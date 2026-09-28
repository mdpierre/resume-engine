---
name: resume-setup
description: First-time setup for the resume engine. Turns the user's master profile (Markdown) or an existing resume (PDF, DOCX, Markdown, or pasted text) into data/PROFILE.md, data/resume_facts.json, and a base spec, then builds and checks it. Use when the user says "set me up", shares their profile or resume for the first time, or asks to tailor before data/ exists.
---

# Resume Setup

Goal: a `data/` folder that the engine can build from, where every fact came from the user's own source. You're transcribing and organizing, not writing a better career.

## 1. Environment

- If `.venv/` is missing: `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt`.
- Check `soffice` and `pdftotext` are on PATH (or at `/Applications/LibreOffice.app/Contents/MacOS/soffice`). If not, give the user the install command from README.md and stop until they've run it.
- If `data/` already has files, ask before overwriting anything.

## 2. Read the source

- Markdown profile: read it in full.
- PDF: `pdftotext -layout <file> -` (read the rendered page too if the text comes out jumbled).
- DOCX: `.venv/bin/python -c "import docx,sys;print('\n'.join(p.text for p in docx.Document(sys.argv[1]).paragraphs))" <file>`.
- Pasted text: use as-is.

## 3. Write `data/PROFILE.md`

Use `example/PROFILE.md` as the shape. Required:

- A `## Contact` block with `- Name:` and `- Email:` (plus Location, Phone, LinkedIn, GitHub, Website when the source has them).
- `## Experience` with one `###` per role: `Title - Organization (Location) - Start - End`, then the bullets.
- `## Education`, `## Skills`, and `## Projects` / `## Leadership` if the source has them.

If the user gave a master profile, keep all of it, including sections the engine doesn't use (positioning notes, claim boundaries, stories). Just make sure the Contact block and the sections above exist. If they gave a resume, the profile is the resume's content restructured; note at the top that it was imported from a resume and should grow over time, since a single resume holds less than the person actually did.

Never add a number, outcome, tool, or date the source doesn't state. Where the source is ambiguous (a missing end date, "led a team" with no size), leave it as written and add it to your question list.

## 4. Register facts in `data/resume_facts.json`

Shape: see `example/resume_facts.json`.

- `"canonical_profile": "PROFILE.md"`.
- One fact per distinct claim you might want to cite on its own: usually one per bullet, one per skill line, one per degree, one per project, and one for the positioning summary if there is one.
- IDs: `<section>.<org_or_topic>_<short_claim>`, lowercase, e.g. `experience.acme_dashboard`, `skills.data`, `education.uic_bs`.
- `source`: `PROFILE.md#<Section>`. `note`: a compressed version of the claim with its numbers, so a later agent can pick facts without re-reading the profile.
- Leave out the `person` block (contact info is read straight from PROFILE.md).

## 5. Write `data/specs/base.json`

A general-purpose resume aimed at the kind of role the profile points to. Copy the structure of `example/specs/base.json`. Follow every rule in the tailor-resume skill's "Spec rules" section (real titles, no invented outcomes, cite everything, ASCII hyphens, one-page content budget). `company` is `"Base"`.

## 6. Build and check

```bash
.venv/bin/python resume.py validate base
.venv/bin/python resume.py build base
```

Then render and look at it:

```bash
cd "output/<dir>" && mkdir -p qa && pdftoppm -png -r 90 "<Name> - <ACRONYM> Resume.pdf" qa/pt
```

Read `qa/pt-1.png`. Check: one page, nothing clipped or overlapping, dates right-aligned, no single word wrapped onto its own line, no date wrapped under a title. If the build says there's no readable one-page layout, cut the least useful bullet (or add one if the fill is too low) and rebuild. Don't touch font sizes.

## 7. Report

Short:

- where the files are (`data/…`, and the built PDF/DOCX)
- how many facts you registered
- your question list: ambiguous items, and things resumes usually benefit from that the source doesn't have (numbers on a vague bullet, dates, tools used). Ask whether each is true; don't assume it is.
- next step: paste a job description to tailor
