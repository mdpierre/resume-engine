"""Telegram front door for the resume pipeline (long polling, one process).

    TELEGRAM_BOT_TOKEN=... ANTHROPIC_API_KEY=... python telegram_bot.py

The bot only moves bytes and messages. The work happens in ``pipeline.run_job`` on a worker
thread, so a 3-minute build never blocks other chats. See DEPLOY.md for settings.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import pipeline

log = logging.getLogger("resume_bot")

HELP = (
    "I turn your resume into a one-page version tailored to a job.\n\n"
    "1. Send /resume\n"
    "2. Upload your resume as a PDF or DOCX\n"
    "3. Paste the job description\n\n"
    "In a few minutes you get a PDF and an editable DOCX back. I only use what's in your "
    "resume; I never make up numbers, titles or tools.\n\n"
    "/cancel starts over. Your files are deleted as soon as your resume is done.\n"
    "Privacy: your resume and the job post are sent to Anthropic's Claude API to write the resume."
)


# --- config ---------------------------------------------------------------------


def _int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


@dataclass(frozen=True)
class Config:
    token: str
    max_upload_mb: int = 5
    daily_limit_per_user: int = 3
    global_daily_limit: int = 40
    max_concurrent_jobs: int = 2
    job_timeout: int = 900
    jd_min_chars: int = 200
    jd_max_chars: int = 15_000
    jd_debounce: float = 4.0
    allowed_user_ids: frozenset[int] = frozenset()
    work_root: Path = Path(tempfile.gettempdir()) / "resume-bot"

    @classmethod
    def from_env(cls) -> "Config":
        missing = [name for name in ("TELEGRAM_BOT_TOKEN", "ANTHROPIC_API_KEY") if not os.environ.get(name)]
        if missing:
            raise SystemExit(f"Missing required environment variable(s): {', '.join(missing)}. See DEPLOY.md.")
        allowed = os.environ.get("ALLOWED_USER_IDS", "")
        return cls(
            token=os.environ["TELEGRAM_BOT_TOKEN"],
            max_upload_mb=_int("MAX_UPLOAD_MB", 5),
            daily_limit_per_user=_int("DAILY_LIMIT_PER_USER", 3),
            global_daily_limit=_int("GLOBAL_DAILY_LIMIT", 40),
            max_concurrent_jobs=_int("MAX_CONCURRENT_JOBS", 2),
            job_timeout=_int("JOB_TIMEOUT_SECONDS", 900),
            jd_min_chars=_int("JD_MIN_CHARS", 200),
            jd_max_chars=_int("JD_MAX_CHARS", 15_000),
            allowed_user_ids=frozenset(int(x) for x in allowed.replace(",", " ").split()),
            work_root=Path(os.environ.get("WORK_ROOT", Path(tempfile.gettempdir()) / "resume-bot")),
        )


# --- limits ---------------------------------------------------------------------


@dataclass
class UsageLimiter:
    """Daily quotas (UTC) and one running job per user. In memory: a restart resets counts."""

    per_user: int
    global_limit: int
    _day: dt.date = field(default_factory=lambda: dt.datetime.now(dt.timezone.utc).date())
    _counts: dict[int, int] = field(default_factory=dict)
    _total: int = 0
    _active: set[int] = field(default_factory=set)

    def _roll(self) -> None:
        today = dt.datetime.now(dt.timezone.utc).date()
        if today != self._day:
            self._day, self._counts, self._total = today, {}, 0

    def try_start(self, user_id: int) -> str | None:
        """Reserve a run, or return the reason the user can't start one."""
        self._roll()
        if user_id in self._active:
            return "You already have a resume in progress. I'll send it when it's done."
        if self._counts.get(user_id, 0) >= self.per_user:
            return f"You've used your {self.per_user} resumes for today. Try again tomorrow (UTC)."
        if self._total >= self.global_limit:
            return "The bot has hit its daily limit. Try again tomorrow."
        self._counts[user_id] = self._counts.get(user_id, 0) + 1
        self._total += 1
        self._active.add(user_id)
        return None

    def finish(self, user_id: int, *, refund: bool = False) -> None:
        self._active.discard(user_id)
        if refund:
            self._roll()
            if self._counts.get(user_id, 0) > 0:
                self._counts[user_id] -= 1
                self._total -= 1

    def remaining(self, user_id: int) -> int:
        self._roll()
        return max(0, self.per_user - self._counts.get(user_id, 0))


# --- helpers --------------------------------------------------------------------


def _cfg(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.bot_data["config"]


def _limiter(context: ContextTypes.DEFAULT_TYPE) -> UsageLimiter:
    return context.bot_data["limiter"]


def _reset(user_data: dict) -> None:
    for key in ("stage", "resume_text", "jd_parts", "touched"):
        user_data.pop(key, None)


async def _allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    allowed = _cfg(context).allowed_user_ids
    user = update.effective_user
    if not allowed or (user and user.id in allowed):
        return True
    log.info("rejected user %s (not in ALLOWED_USER_IDS)", user.id if user else "?")
    await update.effective_message.reply_text(
        f"This bot is invite-only. Send your user ID ({user.id if user else '?'}) to the owner to get access."
    )
    return False


def summarize(result: pipeline.JobResult) -> str:
    lines = [f"Done: one page, {result.fill_ratio * 100:.0f}% full."]
    if result.covered_keywords:
        lines.append("Matched from the posting: " + ", ".join(result.covered_keywords[:12]))
    if result.unsupported_keywords:
        lines.append(
            "Asked for but not in your resume: " + ", ".join(result.unsupported_keywords[:12])
            + ". If any of these are true for you, add them to your resume and run /resume again."
        )
    if result.questions:
        lines.append("Worth checking: " + " ".join(result.questions[:3]))
    return "\n\n".join(lines)


async def _keep_typing(chat, stop: asyncio.Event) -> None:
    while not stop.is_set():
        try:
            await chat.send_action(ChatAction.UPLOAD_DOCUMENT)
        except Exception:  # cosmetic; never let it kill the job
            pass
        try:
            await asyncio.wait_for(stop.wait(), timeout=4.5)
        except asyncio.TimeoutError:
            pass


# --- commands ---------------------------------------------------------------------


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _allowed(update, context):
        await update.effective_message.reply_text("Hi! " + HELP)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if await _allowed(update, context):
        left = _limiter(context).remaining(update.effective_user.id)
        await update.effective_message.reply_text(f"{HELP}\n\nResumes left today: {left}.")


async def resume_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    _reset(context.user_data)
    context.user_data.update(stage="await_resume", touched=time.time())
    await update.effective_message.reply_text(
        f"Upload your current resume as a PDF or DOCX (up to {_cfg(context).max_upload_mb} MB)."
    )


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _reset(context.user_data)
    await update.effective_message.reply_text("Cleared. Send /resume to start again.")


# --- uploads and job descriptions ----------------------------------------------------


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    cfg, message = _cfg(context), update.effective_message
    doc = message.document
    suffix = Path(doc.file_name or "").suffix.lower()
    if suffix not in (".pdf", ".docx"):
        await message.reply_text("Please send a PDF or DOCX file.")
        return
    if (doc.file_size or 0) > cfg.max_upload_mb * 1024 * 1024:
        await message.reply_text(f"That file is over {cfg.max_upload_mb} MB. Send a smaller one.")
        return

    upload_dir = Path(tempfile.mkdtemp(prefix="upload_", dir=cfg.work_root))
    try:
        path = upload_dir / f"resume{suffix}"
        tg_file = await doc.get_file()
        await tg_file.download_to_drive(path)
        text = await asyncio.to_thread(pipeline.extract_text, path)
    except pipeline.PipelineError as exc:
        log.info("upload rejected for user %s: %s", update.effective_user.id, exc)
        await message.reply_text(exc.user_message)
        return
    finally:
        shutil.rmtree(upload_dir, ignore_errors=True)  # keep only the extracted text, in memory

    _reset(context.user_data)
    context.user_data.update(stage="await_jd", resume_text=text, jd_parts=[], touched=time.time())
    log.info("user %s uploaded a %s (%d chars)", update.effective_user.id, suffix, len(text))
    await message.reply_text("Got it. Now paste the full job description (or send it as a .txt file).")


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _allowed(update, context):
        return
    stage = context.user_data.get("stage")
    if stage == "await_resume":
        await update.effective_message.reply_text("Upload your resume as a PDF or DOCX first.")
        return
    if stage != "await_jd":
        await update.effective_message.reply_text("Send /resume to make a tailored resume, or /help.")
        return
    # Telegram splits long pastes into several messages; collect them, then start after a pause.
    context.user_data["jd_parts"].append(update.effective_message.text)
    context.user_data["touched"] = time.time()
    user_id = update.effective_user.id
    name = f"jd-{user_id}"
    for job in context.job_queue.get_jobs_by_name(name):
        job.schedule_removal()
    context.job_queue.run_once(
        jd_ready, _cfg(context).jd_debounce, name=name,
        chat_id=update.effective_chat.id, user_id=user_id,
    )


async def on_jd_file(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """A .txt job description; anything else goes through the resume upload path."""
    if context.user_data.get("stage") != "await_jd":
        await on_document(update, context)
        return
    if not await _allowed(update, context):
        return
    doc = update.effective_message.document
    if (doc.file_size or 0) > 200_000:
        await update.effective_message.reply_text("That text file is too big for a job description.")
        return
    data = await (await doc.get_file()).download_as_bytearray()
    context.user_data["jd_parts"] = [bytes(data).decode("utf-8", errors="replace")]
    await _launch(context, update.effective_chat.id, update.effective_user.id)


async def jd_ready(context: ContextTypes.DEFAULT_TYPE) -> None:
    await _launch(context, context.job.chat_id, context.job.user_id)


async def _launch(context: ContextTypes.DEFAULT_TYPE, chat_id: int, user_id: int) -> None:
    cfg = _cfg(context)
    user_data = context.application.user_data[user_id]
    if user_data.get("stage") != "await_jd":
        return
    jd = "\n".join(user_data.get("jd_parts", [])).strip()
    if len(jd) < cfg.jd_min_chars:
        await context.bot.send_message(chat_id, "That looks short for a job description. Paste the whole posting.")
        user_data["jd_parts"] = []
        return
    if len(jd) > cfg.jd_max_chars:
        await context.bot.send_message(chat_id, f"That's longer than {cfg.jd_max_chars:,} characters. Paste just the role's posting.")
        user_data["jd_parts"] = []
        return
    refusal = _limiter(context).try_start(user_id)
    if refusal:
        await context.bot.send_message(chat_id, refusal)
        return
    resume_text = user_data["resume_text"]
    _reset(user_data)
    context.application.create_task(
        run_resume_job(context.application, chat_id, user_id, resume_text, jd), update=None
    )


# --- the long-running part ------------------------------------------------------------


async def run_resume_job(app: Application, chat_id: int, user_id: int, resume_text: str, jd: str) -> None:
    cfg: Config = app.bot_data["config"]
    limiter: UsageLimiter = app.bot_data["limiter"]
    semaphore: asyncio.Semaphore = app.bot_data["semaphore"]
    bot = app.bot
    job_id = f"{user_id}-{int(time.time())}"
    refund = True

    if semaphore.locked():
        await bot.send_message(chat_id, "Other resumes are being built. Yours is queued and will start shortly.")
    async with semaphore:
        await bot.send_message(chat_id, "Working on it. This usually takes 2 to 5 minutes.")
        stop = asyncio.Event()
        chat = await bot.get_chat(chat_id)
        typing = asyncio.create_task(_keep_typing(chat, stop))
        workdir = Path(tempfile.mkdtemp(prefix=f"job_{user_id}_", dir=cfg.work_root))
        started = time.monotonic()
        log.info("job %s started", job_id)
        work = asyncio.ensure_future(asyncio.to_thread(
            pipeline.run_job, resume_text, jd, workdir, app.bot_data["adapter"],
            deadline=started + cfg.job_timeout,
        ))
        cleanup_now = True
        try:
            done, _ = await asyncio.wait({work}, timeout=cfg.job_timeout + 60)
            if not done:
                # The worker thread can't be killed; delete its files once it gives up on its own.
                cleanup_now = False
                work.add_done_callback(lambda _: shutil.rmtree(workdir, ignore_errors=True))
                log.error("job %s exceeded %ss", job_id, cfg.job_timeout)
                await bot.send_message(chat_id, "This took too long and was stopped. Please try again.")
                return
            result = work.result()
            stop.set()
            for path in (result.pdf, result.docx):
                with path.open("rb") as fh:
                    await bot.send_document(chat_id, fh, filename=path.name, write_timeout=60)
            await bot.send_message(chat_id, summarize(result))
            refund = False
            log.info("job %s done in %.0fs, %d attempt(s), fill %.3f",
                     job_id, time.monotonic() - started, result.attempts, result.fill_ratio)
        except pipeline.PipelineError as exc:
            log.warning("job %s failed: %s", job_id, exc)
            await bot.send_message(chat_id, exc.user_message + "\n\nThis run didn't count against your daily limit.")
        except Exception:
            log.exception("job %s crashed", job_id)
            await bot.send_message(chat_id, "Something went wrong on my side. Please try again later.")
        finally:
            stop.set()
            typing.cancel()
            limiter.finish(user_id, refund=refund)
            if cleanup_now:
                shutil.rmtree(workdir, ignore_errors=True)


# --- housekeeping -----------------------------------------------------------------


async def purge_stale(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Drop resume text that has sat unused for an hour."""
    cutoff = time.time() - 3600
    for user_id, data in list(context.application.user_data.items()):
        if data.get("touched", time.time()) < cutoff:
            _reset(data)
            log.info("purged idle session for user %s", user_id)


async def on_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    log.error("unhandled error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("Something went wrong. Please try again.")
        except Exception:
            pass


def build_app(cfg: Config, adapter) -> Application:
    shutil.rmtree(cfg.work_root, ignore_errors=True)  # nothing from a previous run survives a restart
    cfg.work_root.mkdir(parents=True, exist_ok=True)
    app = (
        ApplicationBuilder()
        .token(cfg.token)
        .connect_timeout(10)
        .read_timeout(30)
        .write_timeout(30)
        .pool_timeout(10)
        .media_write_timeout(60)
        .get_updates_read_timeout(40)
        .build()
    )
    app.bot_data.update(
        config=cfg,
        adapter=adapter,
        limiter=UsageLimiter(cfg.daily_limit_per_user, cfg.global_daily_limit),
        semaphore=asyncio.Semaphore(cfg.max_concurrent_jobs),
    )
    private = filters.ChatType.PRIVATE
    app.add_handler(CommandHandler("start", start, filters=private))
    app.add_handler(CommandHandler("help", help_cmd, filters=private))
    app.add_handler(CommandHandler("resume", resume_cmd, filters=private))
    app.add_handler(CommandHandler("cancel", cancel_cmd, filters=private))
    app.add_handler(MessageHandler(private & filters.Document.TXT, on_jd_file, block=False))
    app.add_handler(MessageHandler(private & filters.Document.ALL, on_document, block=False))
    app.add_handler(MessageHandler(private & filters.TEXT & ~filters.COMMAND, on_text))
    app.add_error_handler(on_error)
    app.job_queue.run_repeating(purge_stale, interval=600, first=600)
    return app


def main() -> None:
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)  # its INFO lines contain the bot token
    cfg = Config.from_env()
    from claude_adapter import AnthropicAdapter

    app = build_app(cfg, AnthropicAdapter())
    log.info("starting: %d/user/day, %d global/day, %d concurrent, allowlist=%s",
             cfg.daily_limit_per_user, cfg.global_daily_limit, cfg.max_concurrent_jobs,
             "on" if cfg.allowed_user_ids else "off")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
