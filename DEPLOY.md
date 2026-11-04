# Deploying the Telegram bot on Railway

Friends message a Telegram bot, upload a resume, paste a job post, and get back a one-page PDF and DOCX. The engine is unchanged; the bot is a thin layer on top of it.

## How it fits together

```
telegram_bot.py   transport: commands, uploads, limits, queueing, sending files
      |
pipeline.py       one job: extract text -> Claude -> data/ files -> resume.py -> repair loop
      |
claude_adapter.py Claude API calls, with prompts built from the two SKILL.md files
      |
resume.py         the engine, run as a subprocess exactly as you run it by hand
```

- **Engine** (`resume*.py`) is untouched. `python resume.py build ...` still works the same way locally.
- **Adapter** stands in for Claude Code. Locally, Claude Code follows `.claude/skills/resume-setup` and `tailor-resume`. In production, `AnthropicAdapter` sends those same rule sections (read from the SKILL.md files at startup) to the Messages API and gets back structured JSON: `PROFILE.md` plus facts, then a spec. If you edit a skill, the bot follows the change on its next restart.
- **Pipeline** writes a throwaway `data/` folder, then runs `validate` and `build`. Any engine error, such as an unknown evidence ID or content that runs past one page, goes back to Claude to fix, up to `MAX_SPEC_REPAIRS` times. The engine's checks still have the final say: nothing gets sent unless `resume.py build` passes.
- **Bot** keeps each job off the event loop (`asyncio.to_thread`), caps how many run at once with a semaphore, and deletes every file once the job ends.

### Run the whole flow locally, without Telegram

```bash
.venv/bin/pip install -r requirements-bot.txt
export ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python pipeline.py ~/Downloads/resume.pdf job.txt -o output/pipeline --keep-workdir
```

`--keep-workdir` keeps the generated `PROFILE.md`, facts, and spec so you can inspect them.

### Run the bot locally

```bash
export TELEGRAM_BOT_TOKEN=123456:ABC... ANTHROPIC_API_KEY=sk-ant-...
.venv/bin/python telegram_bot.py
```

Stop the local bot before deploying. Telegram allows only one long-polling client per token.

## Railway setup

1. In Telegram, message **@BotFather**, send `/newbot`, and copy the token. Optionally use `/setcommands` to register:
   ```
   resume - Make a tailored resume
   help - How it works
   cancel - Start over
   ```
2. In Railway, create a **New Project**, choose **Deploy from GitHub repo**, and pick this repo. Railway finds `railway.json` and builds the `Dockerfile`, which includes LibreOffice, Poppler, and Liberation fonts.
3. Under **Variables**, add `TELEGRAM_BOT_TOKEN` and `ANTHROPIC_API_KEY`. Add any optional variables from the table below.
4. Deploy, then open **Deploy Logs**. You should see `starting: 3/user/day, ...`. Message the bot `/start`.
5. Leave the service at **1 replica**. Two replicas polling the same token make Telegram return `Conflict` errors. The bot doesn't need a public domain or port, because long polling only makes outbound connections.

During a redeploy, the old and new containers can overlap for a few seconds, so one `Conflict` line in the logs is normal.

## Environment variables

| Variable | Default | What it does |
|---|---|---|
| `TELEGRAM_BOT_TOKEN` | required | From @BotFather |
| `ANTHROPIC_API_KEY` | required | From console.anthropic.com |
| `ALLOWED_USER_IDS` | empty (anyone) | Comma or space separated Telegram user IDs. **Set this before sharing the link.** A blocked user is told their ID so they can send it to you. |
| `DAILY_LIMIT_PER_USER` | 3 | Successful resumes per user per UTC day. Failed runs are refunded. |
| `GLOBAL_DAILY_LIMIT` | 40 | Cap on runs across all users per day. This is your spend ceiling. |
| `MAX_CONCURRENT_JOBS` | 2 | Jobs that run at the same time; extra jobs wait in a queue. Each LibreOffice render uses about 300 MB of RAM. |
| `MAX_UPLOAD_MB` | 5 | Largest resume file accepted |
| `JOB_TIMEOUT_SECONDS` | 900 | Total time limit for one job |
| `CLAUDE_TIMEOUT_SECONDS` | 300 | Time limit for each Claude call |
| `BUILD_TIMEOUT_SECONDS` | 420 | Time limit for each engine build. The process group is killed when it runs out, so leftover `soffice` processes die too. |
| `MAX_SPEC_REPAIRS` | 2 | How many times Claude can retry after the engine rejects a spec |
| `CLAUDE_MODEL` | `claude-sonnet-5-5` | Model for both steps. `claude-opus-5-5` costs twice as much and may tailor better |
| `CLAUDE_EFFORT` | `medium` | `low` is cheaper and faster; `high` gives better tailoring |
| `CLAUDE_FALLBACKS` | `1` | Server-side fallback to another model when the main one is overloaded. Set `0` to always use `CLAUDE_MODEL`. |
| `JD_MIN_CHARS` / `JD_MAX_CHARS` | 200 / 15000 | Accepted job description length |
| `WORK_ROOT` | `/tmp/resume-bot` | Scratch space. It is wiped on startup. |
| `LOG_LEVEL` | `INFO` | |

## Behavior worth knowing

- **Latency.** A job makes two Claude calls (profile, then spec) plus a build of about 30 to 90 seconds, so it usually takes 2 to 5 minutes. The bot shows "sending a file..." the whole time.
- **Cost.** Each run is a few Claude calls with the skill rules as a cached system prompt. Check your usage in the Anthropic console after a few runs and set `GLOBAL_DAILY_LIMIT` to match what you're willing to spend.
- **Privacy.** Resume text and job posts are sent to the Anthropic API. Uploaded files are deleted as soon as text is extracted. The extracted text stays in memory until the job starts and is dropped after an hour of inactivity. Each job's work folder is deleted when the job finishes. Logs record user IDs and timings, never resume content. `/help` tells users all of this.
- **State is in memory.** Counters and half-finished conversations reset on restart. That's fine for a friends-scale bot. If you outgrow it, move `UsageLimiter` to Railway Redis or Postgres; nothing else depends on how it's stored.
- **Long pastes.** Telegram splits messages longer than 4096 characters. The bot collects consecutive messages and starts about 4 seconds after the last one. Users can also send a `.txt` file.
- **Honesty rules still apply.** The adapter gets the same AGENTS.md rules: no invented facts, real titles only, and every line must cite evidence. The engine enforces citations itself. Keywords from the posting that aren't in the resume are sent back to the user, not added.

## Reusing this pattern for another automation product

The parts that don't change from product to product:

1. **A deterministic core you can run from a CLI.** Here that's `resume.py`. Keep it free of transport and LLM code.
2. **An adapter with a small Protocol** (`ResumeAdapter`). The production version calls the API with structured output. Tests use a fake that returns fixtures, so the whole flow runs offline.
3. **A pipeline function**, `run_job(inputs, workdir, adapter, deadline) -> result`. It's synchronous, keeps all its files inside `workdir`, raises one error type whose message is safe to show users, and can be run from the command line.
4. **A transport** that handles access, limits, queueing, cleanup, and sending results. The bot only depends on `run_job` and `JobResult`.
5. **Deploy files**: a Dockerfile with system dependencies, `railway.json` with a single replica, and secrets passed in as environment variables.

To build a new product, replace step 1 and the adapter prompts, then keep or copy the transport layer.

## Tests

```bash
.venv/bin/python -m unittest -v tests/test_bot_pipeline.py
```

These tests don't touch the network. They cover:

- an end-to-end build with a fake adapter;
- the repair loop;
- timeouts and process-group kills;
- text extraction;
- usage limits;
- config errors;
- the async job path with a mocked Telegram bot, checking that files are sent, the workdir is deleted, and failed runs are refunded.

Testing against the real API and a real bot needs both keys set; see "Run the bot locally".
