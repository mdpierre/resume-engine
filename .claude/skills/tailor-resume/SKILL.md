---
name: tailor-resume
description: Tailor the user's resume to a specific job posting using the evidence-gated resume engine. Maps the posting's keywords to registered facts, writes a role spec, builds a one-page DOCX/PDF, and visually checks it. Use when the user pastes a job description or says "tailor my resume", "resume for this job", or /tailor-resume.
---

# Tailor Resume

Every path is relative to the repo root. If `data/resume_facts.json` doesn't exist, run the resume-setup skill first.

## 1. Plan

1. **Read only** the job description, `data/resume_facts.json`, and the closest existing spec in `data/specs/` (start from `base.json` if it's the only one). Open `data/PROFILE.md` only when a fact note doesn't carry enough wording, or to check a claim boundary. If PROFILE.md has a section on how the person's work may be described (for example "say directed, not engineered"), follow it.
2. **Map keywords to facts.** For each important keyword or requirement in the posting, find a supporting fact ID or mark it **unsupported**. Never cover an unsupported keyword with vague wording; list it for the user instead.
3. **Write** `data/specs/<company>-<role>.json` (slugified) by copying the closest spec and changing the company, role, acronym, headline, summary, alignment, order, and which bullets appear.

### Spec rules

- **Real titles only.** Use the title the person actually held. Don't rebrand it to match the posting.
- **No invented outcomes.** Reword and reorder facts, but don't add results, numbers, scope, or actions a fact doesn't support. "Which reduced churn", "ensuring no downtime", or "escalated to the right team" are all additions unless a fact says so.
- **Cite everything.** Summary (`summary_evidence`), every experience/leadership item and each of its bullets, each project, each skill line, each education entry.
- **ASCII hyphens only.** No em or en dashes; the engine rejects them.
- **One-page budget:** about 3 to 5 roles with 1 to 4 bullets each, at most 1 leadership item, 1 to 3 projects, 3 or 4 skill lines, and a summary of 3 lines or fewer. More than that usually won't fit.
- `alignment` (a one-line list of the posting's themes you can back up) is optional; drop it if space is tight.

### New facts

If the user confirms something true that isn't in the profile, add it to `data/PROFILE.md` in the right section first, register an ID in `data/resume_facts.json` with `source: "PROFILE.md#<Section>"`, then cite it.

## 2. Build

```bash
.venv/bin/python resume.py build <company>-<role>
```

Output goes to `output/<date>-<company>-<role>/`. Then:

```bash
cd "output/<dir>" && mkdir -p qa && pdftoppm -png -r 90 "<PDF name>" qa/pt
```

Read `qa/pt-1.png` and check for one page, no clipped or overlapping text, aligned dates, and nothing visibly awkward (a lone word on its own line, a date wrapped under a title).

(Optional: if your harness can spawn a cheaper subagent, it can run this build-and-look step. Tell it not to change any spec wording.)

## 3. Fix loop

- **"No readable one-page layout"** with pages > 1: too much content. Cut the least relevant bullet or role, not the font size.
- Same error with low fill: too little. Add a relevant bullet or project.
- **Evidence or dash errors:** fix the spec and rebuild.
- **Visual problems** like an orphaned word: tighten that bullet's wording and rebuild. Only for real problems.

## 4. Report

Keep it short:

- the PDF and DOCX paths
- what changed from the base resume: headline, ordering, what was added or cut
- keywords covered, and the **unsupported** ones, asking whether any are actually true
- if the user also shared a cover letter, any claim in it that PROFILE.md doesn't support
