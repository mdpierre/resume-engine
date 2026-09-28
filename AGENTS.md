# AGENTS.md - resume-engine

This repo builds one-page, evidence-gated resumes. The person using it is the resume owner; their data lives in `data/` (gitignored), or wherever `RESUME_DATA` points.

## Workflows

- **First run, or "set me up" / "here's my resume/profile"** -> follow `.claude/skills/resume-setup/SKILL.md`.
- **A job description, or "tailor my resume"** -> follow `.claude/skills/tailor-resume/SKILL.md`.

If `data/` doesn't exist yet and the user asks to tailor, run setup first.

## Commands

```bash
.venv/bin/python resume.py validate <spec>
.venv/bin/python resume.py build <spec> [--output-dir DIR] [--hide-phone]
.venv/bin/python -m unittest -v tests/test_engine.py
```

If `.venv` is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`. The build also needs LibreOffice (`soffice`) and Poppler (`pdftotext`, `pdftoppm`); if either is missing, tell the user the install command from README.md rather than working around it.

## Hard rules

- `data/PROFILE.md` is the only source of truth. Never put a claim in a spec that isn't backed by a registered fact that traces to PROFILE.md.
- Don't invent numbers, outcomes, titles, dates, or tools. Reframing is fine; adding is not. If the user tells you something new and true, add it to PROFILE.md first, then register it, then cite it.
- Use the person's real job titles. Don't rebrand a title to match a posting.
- Plain ASCII hyphens only in specs.
- Fix overflow by cutting content, never by forcing smaller type.
- Don't edit the engine code (`resume*.py`) to get a resume to pass. If the engine seems wrong, say so.
- Don't commit anything in `data/` or `output/` unless the user asks.
