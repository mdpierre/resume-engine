"""Tests for the deployment layer: adapter helpers, pipeline, and bot limits.

No network: a fake adapter stands in for Claude and returns the example profile and spec.
The end-to-end tests run the real engine and are skipped without LibreOffice/Poppler.
"""

from __future__ import annotations

import copy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import claude_adapter  # noqa: E402
import pipeline  # noqa: E402
from claude_adapter import ProfileDraft, SpecDraft  # noqa: E402

EXAMPLE = ROOT / "example"
HAVE_TOOLS = bool(shutil.which("soffice") or os.environ.get("SOFFICE")) and bool(shutil.which("pdftotext"))


def example_spec() -> dict:
    return json.loads((EXAMPLE / "specs" / "base.json").read_text(encoding="utf-8"))


class FakeAdapter:
    """Returns the example data; ``specs`` lets a test script a sequence of attempts."""

    def __init__(self, specs: list[dict] | None = None):
        self.specs = specs or [example_spec()]
        self.calls: list[dict] = []

    def import_profile(self, resume_text, *, timeout):
        return ProfileDraft(
            profile_md=(EXAMPLE / "PROFILE.md").read_text(encoding="utf-8"),
            facts=json.loads((EXAMPLE / "resume_facts.json").read_text(encoding="utf-8")),
            questions=["Is your phone number current?"],
        )

    def write_spec(self, profile_md, facts, job_description, *, timeout, previous_spec=None, feedback=None):
        self.calls.append({"previous_spec": previous_spec, "feedback": feedback})
        spec = self.specs[min(len(self.calls) - 1, len(self.specs) - 1)]
        return SpecDraft(spec=copy.deepcopy(spec), covered_keywords=["SQL"], unsupported_keywords=["Kubernetes"])


def bad_evidence_spec() -> dict:
    spec = example_spec()
    spec["experience"][0]["bullets"][0]["evidence"] = ["made.up_fact"]
    return spec


class WorkdirTest(unittest.TestCase):
    def setUp(self):
        self.workdir = Path(tempfile.mkdtemp(prefix="test_job_"))
        self.addCleanup(shutil.rmtree, self.workdir, ignore_errors=True)


@unittest.skipUnless(HAVE_TOOLS, "needs LibreOffice and Poppler")
class PipelineEndToEnd(WorkdirTest):
    def test_builds_example_profile(self):
        adapter = FakeAdapter()
        result = pipeline.run_job("resume text", "job text", self.workdir, adapter)
        self.assertTrue(result.pdf.is_file() and result.docx.is_file())
        self.assertTrue(result.pdf.resolve().is_relative_to(self.workdir.resolve()), "outputs must stay inside the job workdir")
        self.assertGreaterEqual(result.fill_ratio, 0.95)
        self.assertEqual(result.attempts, 1)
        self.assertEqual(result.unsupported_keywords, ["Kubernetes"])

    def test_repairs_spec_from_validator_feedback(self):
        adapter = FakeAdapter([bad_evidence_spec(), example_spec()])
        result = pipeline.run_job("resume text", "job text", self.workdir, adapter)
        self.assertEqual(result.attempts, 2)
        self.assertIn("made.up_fact", adapter.calls[1]["feedback"])
        self.assertIsNotNone(adapter.calls[1]["previous_spec"])

    def test_gives_up_after_max_repairs(self):
        adapter = FakeAdapter([bad_evidence_spec()])
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_job("resume text", "job text", self.workdir, adapter, max_repairs=1)
        self.assertEqual(len(adapter.calls), 2)
        self.assertIn("one clean page", ctx.exception.user_message)

    def test_missing_contact_is_a_friendly_error(self):
        class NoContact(FakeAdapter):
            def import_profile(self, resume_text, *, timeout):
                draft = super().import_profile(resume_text, timeout=timeout)
                draft.profile_md = draft.profile_md.replace("## Contact", "## Nothing here")
                return draft

        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.run_job("resume text", "job text", self.workdir, NoContact())
        self.assertIn("name and an email", ctx.exception.user_message)

    def test_extracts_pdf_and_docx_text(self):
        out = self.workdir / "out"
        code, output = pipeline.run_engine(
            ["--data", str(EXAMPLE), "build", "base", "--output-dir", str(out)], timeout=300
        )
        self.assertEqual(code, 0, output)
        report = json.loads((out / "resume-validation.json").read_text(encoding="utf-8"))
        for key in ("output_pdf", "output_docx"):
            text = pipeline.extract_text(Path(report[key]))
            self.assertIn("Jordan Rivera", text)


class PipelineUnits(WorkdirTest):
    def test_rejects_unknown_file_type(self):
        path = self.workdir / "resume.txt"
        path.write_text("x" * 500)
        with self.assertRaises(pipeline.PipelineError):
            pipeline.extract_text(path)

    def test_rejects_corrupt_docx(self):
        path = self.workdir / "resume.docx"
        path.write_bytes(b"not a zip file")
        with self.assertRaises(pipeline.PipelineError) as ctx:
            pipeline.extract_text(path)
        self.assertIn("couldn't read", ctx.exception.user_message)

    def test_engine_timeout_kills_process_group(self):
        with mock.patch.object(pipeline, "ENGINE", self.workdir / "slow.py"):
            (self.workdir / "slow.py").write_text("import time; time.sleep(30)\n")
            with self.assertRaises(pipeline.PipelineError) as ctx:
                pipeline.run_engine([], timeout=1)
        self.assertIn("too long", ctx.exception.user_message)

    def test_deadline_stops_before_calling_claude(self):
        import time

        with self.assertRaises(pipeline.PipelineError):
            pipeline.run_job("r", "j", self.workdir, FakeAdapter(), deadline=time.monotonic() + 1)

    def test_feedback_names_overflow(self):
        output = "iter 1: body=10.0 spacing=0.80 pages=2 fill=100.0% adjust\nRuntimeError: No readable one-page layout"
        feedback = pipeline._engine_feedback(output)
        self.assertIn("RuntimeError", feedback)
        self.assertIn("cut", feedback)


class AdapterHelpers(unittest.TestCase):
    def test_skill_sections_exist(self):
        self.assertIn("evidence", claude_adapter.spec_rules().lower())
        self.assertIn("PROFILE.md", claude_adapter.setup_rules())

    def test_clean_spec_normalizes(self):
        spec = example_spec()
        spec["headline"] = "Data — Analyst "
        spec["layout"] = "technical"
        spec["projects"] = []
        spec["experience"][0]["location"] = ""
        cleaned = claude_adapter.clean_spec(spec)
        self.assertEqual(cleaned["headline"], "Data - Analyst")
        self.assertNotIn("layout", cleaned)
        self.assertNotIn("projects", cleaned)
        self.assertNotIn("location", cleaned["experience"][0])

    def test_facts_registry_drops_bad_ids(self):
        reg = claude_adapter.facts_registry([
            {"id": "work.acme_role", "source": "PROFILE.md#acme", "note": "ok"},
            {"id": "Bad ID", "source": "x", "note": "x"},
        ])
        self.assertEqual(list(reg["facts"]), ["work.acme_role"])
        with self.assertRaises(claude_adapter.AdapterError):
            claude_adapter.facts_registry([{"id": "nope", "source": "", "note": ""}])

    def test_schemas_are_strict(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertFalse(node["additionalProperties"])
                self.assertEqual(set(node["required"]), set(node["properties"]))
                for child in node["properties"].values():
                    walk(child)
            elif node.get("type") == "array":
                walk(node["items"])

        walk(claude_adapter.SPEC_SCHEMA)
        walk(claude_adapter.PROFILE_SCHEMA)


try:
    import telegram  # noqa: F401

    HAVE_PTB = True
except ImportError:
    HAVE_PTB = False


@unittest.skipUnless(HAVE_PTB, "needs requirements-bot.txt")
class BotUnits(unittest.TestCase):
    def setUp(self):
        import telegram_bot

        self.bot = telegram_bot

    def test_limiter_quotas_and_refunds(self):
        limiter = self.bot.UsageLimiter(per_user=2, global_limit=3)
        self.assertIsNone(limiter.try_start(1))
        self.assertIn("in progress", limiter.try_start(1))
        limiter.finish(1)
        self.assertIsNone(limiter.try_start(1))
        limiter.finish(1)
        self.assertIn("used your 2", limiter.try_start(1))
        self.assertIsNone(limiter.try_start(2))
        limiter.finish(2, refund=True)  # failed runs don't count
        self.assertEqual(limiter.remaining(2), 2)
        self.assertIsNone(limiter.try_start(2))
        self.assertIn("daily limit", limiter.try_start(3))

    def test_limiter_resets_each_day(self):
        import datetime as dt

        limiter = self.bot.UsageLimiter(per_user=1, global_limit=1)
        limiter.try_start(1)
        limiter.finish(1)
        limiter._day -= dt.timedelta(days=1)
        self.assertIsNone(limiter.try_start(1))

    def test_config_requires_both_secrets(self):
        with mock.patch.dict(os.environ, {"TELEGRAM_BOT_TOKEN": "x"}, clear=True):
            with self.assertRaises(SystemExit) as ctx:
                self.bot.Config.from_env()
        self.assertIn("ANTHROPIC_API_KEY", str(ctx.exception))

    def test_config_parses_allowlist(self):
        env = {"TELEGRAM_BOT_TOKEN": "x", "ANTHROPIC_API_KEY": "y", "ALLOWED_USER_IDS": "12, 34 56"}
        with mock.patch.dict(os.environ, env, clear=True):
            cfg = self.bot.Config.from_env()
        self.assertEqual(cfg.allowed_user_ids, {12, 34, 56})

    def test_app_builds_without_network(self):
        cfg = self.bot.Config(token="123456:TEST", work_root=Path(tempfile.mkdtemp()) / "bot")
        app = self.bot.build_app(cfg, FakeAdapter())
        self.assertTrue(cfg.work_root.is_dir())
        self.assertIsNotNone(app.job_queue)
        shutil.rmtree(cfg.work_root.parent, ignore_errors=True)

    def test_summary_mentions_gaps(self):
        result = pipeline.JobResult(Path("a.pdf"), Path("a.docx"), 0.96, 1, ["SQL"], ["Kubernetes"], [])
        text = self.bot.summarize(result)
        self.assertIn("96%", text)
        self.assertIn("Kubernetes", text)


@unittest.skipUnless(HAVE_PTB and HAVE_TOOLS, "needs bot deps, LibreOffice and Poppler")
class BotJobFlow(unittest.IsolatedAsyncioTestCase):
    """Drives the async job path with a mocked Telegram bot and the real engine."""

    async def _run(self, adapter):
        import asyncio
        from types import SimpleNamespace

        import telegram_bot

        root = Path(tempfile.mkdtemp(prefix="bot_root_"))
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        cfg = telegram_bot.Config(token="x", work_root=root, job_timeout=600)
        sent = []
        bot = mock.AsyncMock()
        bot.send_document.side_effect = lambda chat_id, fh, filename, **kw: sent.append((filename, len(fh.read())))
        bot.get_chat.return_value = mock.Mock(send_action=mock.AsyncMock())
        limiter = telegram_bot.UsageLimiter(3, 10)
        limiter.try_start(7)
        app = SimpleNamespace(bot=bot, bot_data={
            "config": cfg, "adapter": adapter, "limiter": limiter, "semaphore": asyncio.Semaphore(1),
        })
        await telegram_bot.run_resume_job(app, 1, 7, "resume text", "job text")
        messages = [c.args[1] for c in bot.send_message.call_args_list]
        return root, sent, messages, limiter

    async def test_success_sends_both_files_and_cleans_up(self):
        root, sent, messages, limiter = await self._run(FakeAdapter())
        self.assertEqual([name.rsplit(".", 1)[1] for name, _ in sent], ["pdf", "docx"])
        self.assertTrue(all(size > 1000 for _, size in sent))
        self.assertIn("Kubernetes", messages[-1])
        self.assertEqual(list(root.iterdir()), [], "job workdir must be deleted")
        self.assertEqual(limiter.remaining(7), 2)
        self.assertIsNone(limiter.try_start(7), "active slot must be released")

    async def test_failure_refunds_and_cleans_up(self):
        root, sent, messages, limiter = await self._run(FakeAdapter([bad_evidence_spec()]))
        self.assertEqual(sent, [])
        self.assertIn("didn't count", messages[-1])
        self.assertEqual(list(root.iterdir()), [])
        self.assertEqual(limiter.remaining(7), 3)


class LocalCliStillWorks(unittest.TestCase):
    def test_engine_validate_runs_standalone(self):
        proc = subprocess.run(
            [sys.executable, str(ROOT / "resume.py"), "--data", str(EXAMPLE), "validate", "base"],
            capture_output=True, text=True, cwd=ROOT, timeout=60,
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)


if __name__ == "__main__":
    unittest.main()
