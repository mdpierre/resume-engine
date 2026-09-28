"""Structured, evidence-gated DOCX resume renderer.

A data directory holds three things:

  PROFILE.md          canonical facts about the person (the only source of truth)
  resume_facts.json   registry of fact IDs, each pointing back into PROFILE.md
  specs/*.json        one spec per target role, citing fact IDs for every claim

The renderer owns layout only. ``resume.py build`` owns page-fit tuning and
validation.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.text import WD_ALIGN_PARAGRAPH, WD_TAB_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Inches, Pt, RGBColor

INK = RGBColor(0x1A, 0x1A, 0x1A)
MUTED = RGBColor(0x55, 0x55, 0x55)
FAINT = RGBColor(0x77, 0x77, 0x77)

# Contact labels accepted in the PROFILE.md "## Contact" block, mapped to keys.
CONTACT_LABELS = {
    "name": "name",
    "location": "location",
    "phone": "phone",
    "email": "email",
    "email (career)": "email",
    "linkedin": "linkedin",
    "github": "github",
    "website": "website",
    "portfolio": "website",
}
CONTACT_ORDER = ["location", "phone", "email", "linkedin", "github", "website"]


@dataclass(frozen=True)
class Style:
    """Readable typography plus an independently tunable spacing scale."""

    body_pt: float = 10.5
    spacing_scale: float = 1.0
    margin_in: float = 0.5
    line_spacing: float = 1.0
    font_name: str = "Arial"
    accent_hex: str = "1A56DB"

    @property
    def accent(self) -> RGBColor:
        return RGBColor.from_string(self.accent_hex.upper())

    def font(self, offset: float = 0.0) -> Pt:
        return Pt(self.body_pt + offset)

    def gap(self, points: float) -> Pt:
        return Pt(points * self.spacing_scale)


@dataclass(frozen=True)
class ResumeInputs:
    spec: dict[str, Any]
    facts: dict[str, Any]
    person: dict[str, str]
    spec_path: Path
    facts_path: Path
    profile_path: Path

    def base_style(self) -> Style:
        theme = {**self.facts.get("style", {}), **self.spec.get("style", {})}
        return Style(
            font_name=theme.get("font", Style.font_name),
            accent_hex=theme.get("accent", Style.accent_hex).lstrip("#"),
        )


def load_inputs(spec_path: Path, facts_path: Path) -> ResumeInputs:
    spec_path = Path(spec_path).resolve()
    facts_path = Path(facts_path).resolve()
    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    facts = json.loads(facts_path.read_text(encoding="utf-8"))
    profile_path = _profile_path(facts, facts_path)
    person = _resolve_person(facts, profile_path)
    validate_spec(spec, facts, spec_path)
    return ResumeInputs(
        spec=spec,
        facts=facts,
        person=person,
        spec_path=spec_path,
        facts_path=facts_path,
        profile_path=profile_path,
    )


def _profile_path(facts: dict[str, Any], facts_path: Path) -> Path:
    canonical = Path(facts.get("canonical_profile", "PROFILE.md"))
    if not canonical.is_absolute():
        canonical = (facts_path.parent / canonical).resolve()
    if not canonical.exists():
        raise FileNotFoundError(f"canonical profile not found: {canonical}")
    return canonical


def _normalize_url(value: str) -> str:
    return value.strip().removeprefix("https://").removeprefix("http://").removeprefix("www.").rstrip("/")


def profile_contact(profile_path: Path) -> dict[str, str]:
    """Read identity fields from the ``## Contact`` block of PROFILE.md."""
    parsed: dict[str, str] = {}
    in_contact = False
    for line in profile_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("## "):
            in_contact = line[3:].strip().lower() == "contact"
            continue
        if not in_contact or not line.startswith("- ") or ":" not in line:
            continue
        label, value = line[2:].split(":", 1)
        key = CONTACT_LABELS.get(label.strip().lower())
        if key and value.strip() and key not in parsed:
            parsed[key] = _normalize_url(value)
    return parsed


def _resolve_person(facts: dict[str, Any], profile_path: Path) -> dict[str, str]:
    """PROFILE.md is authoritative; an optional facts ``person`` block must agree."""
    person = profile_contact(profile_path)
    for key in ("name", "email"):
        if not person.get(key):
            raise ValueError(
                f"PROFILE.md ## Contact block is missing '- {key.title()}: ...' ({profile_path})"
            )
    declared = {k: _normalize_url(str(v)) for k, v in facts.get("person", {}).items()}
    drift = {k: (person.get(k), v) for k, v in declared.items() if person.get(k) != v}
    if drift:
        raise ValueError(
            "resume_facts.json person block disagrees with PROFILE.md "
            f"(profile, facts): {drift}. Fix one, or delete the person block."
        )
    return person


def _education_entries(spec: dict[str, Any]) -> list[dict[str, Any]]:
    edu = spec.get("education", [])
    return [edu] if isinstance(edu, dict) else list(edu)


def _all_evidence_ids(spec: dict[str, Any]) -> set[str]:
    ids: set[str] = set(spec.get("summary_evidence", []))
    for section in ("experience", "leadership", "projects"):
        for item in spec.get(section, []):
            ids.update(item.get("evidence", []))
            for bullet in item.get("bullets", []):
                ids.update(bullet.get("evidence", []))
    for group in spec.get("skills", []):
        ids.update(group.get("evidence", []))
    for edu in _education_entries(spec):
        ids.update(edu.get("evidence", []))
    return ids


def _iter_text(value: Any):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for nested in value.values():
            yield from _iter_text(nested)
    elif isinstance(value, list):
        for nested in value:
            yield from _iter_text(nested)


def validate_spec(spec: dict[str, Any], facts: dict[str, Any], spec_path: Path) -> None:
    required = {"company", "role", "summary", "experience", "skills", "education"}
    missing = sorted(required - set(spec))
    if missing:
        raise ValueError(f"spec missing required keys: {missing}")

    known = set(facts.get("facts", {}))
    unknown = sorted(_all_evidence_ids(spec) - known)
    if unknown:
        raise ValueError(f"spec references unknown evidence ids: {unknown}")
    if not spec.get("summary_evidence"):
        raise ValueError("summary_evidence must cite at least one supported fact")

    for section in ("experience", "leadership", "projects", "skills"):
        for index, item in enumerate(spec.get(section, [])):
            if not item.get("evidence"):
                raise ValueError(f"{section}[{index}] must cite at least one evidence id")
            for bullet_index, bullet in enumerate(item.get("bullets", [])):
                if not bullet.get("evidence"):
                    raise ValueError(f"{section}[{index}].bullets[{bullet_index}] must cite evidence")
    entries = _education_entries(spec)
    if not entries:
        raise ValueError("education must contain at least one entry")
    for index, edu in enumerate(entries):
        if not edu.get("evidence"):
            raise ValueError(f"education[{index}] must cite at least one evidence id")

    for text in _iter_text(spec):
        if "—" in text or "–" in text:
            raise ValueError(f"non-ASCII dash found in {spec_path.name}: {text!r}")


def _set_font(run, st: Style, size: Pt | None = None, *, color=INK, bold=False, italic=False):
    run.font.name = st.font_name
    rfonts = run._element.get_or_add_rPr().get_or_add_rFonts()
    rfonts.set(qn("w:ascii"), st.font_name)
    rfonts.set(qn("w:hAnsi"), st.font_name)
    run.font.size = size
    run.font.color.rgb = color
    run.bold = bold
    run.italic = italic
    return run


def _run(p, st: Style, text: str, *, offset=0.0, color=INK, bold=False, italic=False):
    return _set_font(p.add_run(text), st, st.font(offset), color=color, bold=bold, italic=italic)


def _paragraph(doc: Document, st: Style, *, align=None, before=0.0, after=0.0):
    p = doc.add_paragraph()
    p.alignment = align
    p.paragraph_format.space_before = st.gap(before)
    p.paragraph_format.space_after = st.gap(after)
    p.paragraph_format.line_spacing = st.line_spacing
    return p


def _bottom_border(p, st: Style) -> None:
    ppr = p._p.get_or_add_pPr()
    pbdr = OxmlElement("w:pBdr")
    bottom = OxmlElement("w:bottom")
    bottom.set(qn("w:val"), "single")
    bottom.set(qn("w:sz"), "6")
    bottom.set(qn("w:space"), "1")
    bottom.set(qn("w:color"), st.accent_hex.upper())
    pbdr.append(bottom)
    ppr.append(pbdr)


def _section(doc: Document, st: Style, label: str) -> None:
    p = _paragraph(doc, st, before=6.2, after=2.0)
    _run(p, st, label, offset=-0.15, color=st.accent, bold=True)
    _bottom_border(p, st)


def _right_tab(p, st: Style) -> None:
    p.paragraph_format.tab_stops.add_tab_stop(Inches(8.5 - 2 * st.margin_in), WD_TAB_ALIGNMENT.RIGHT)


def _item_header(doc: Document, st: Style, item: dict[str, Any]) -> None:
    p = _paragraph(doc, st, before=4.2, after=0.8)
    _right_tab(p, st)
    _run(p, st, item["title"], offset=0.65, bold=True)
    if item.get("organization"):
        _run(p, st, "  |  ", color=FAINT)
        _run(p, st, item["organization"], italic=True, color=MUTED)
    _run(p, st, "\t")
    right = " | ".join(part for part in (item.get("location"), item.get("dates")) if part)
    _run(p, st, right, offset=-0.9, color=FAINT)


def _bullet(doc: Document, st: Style, text: str) -> None:
    p = doc.add_paragraph(style="List Bullet")
    p.paragraph_format.left_indent = Inches(0.24)
    p.paragraph_format.first_line_indent = Inches(-0.14)
    p.paragraph_format.space_before = st.gap(0.55)
    p.paragraph_format.space_after = st.gap(0.55)
    p.paragraph_format.line_spacing = st.line_spacing
    _run(p, st, text)


def _render_items(doc: Document, st: Style, items: list[dict[str, Any]]) -> None:
    for item in items:
        _item_header(doc, st, item)
        for bullet in item.get("bullets", []):
            _bullet(doc, st, bullet["text"])


def build_document(inputs: ResumeInputs, style: Style) -> Document:
    spec = inputs.spec
    person = inputs.person

    doc = Document()
    section = doc.sections[0]
    section.orientation = WD_ORIENT.PORTRAIT
    section.page_width = Inches(8.5)
    section.page_height = Inches(11)
    for side in ("top_margin", "bottom_margin", "left_margin", "right_margin"):
        setattr(section, side, Inches(style.margin_in))

    normal = doc.styles["Normal"]
    normal.font.name = style.font_name
    normal.font.size = style.font()
    normal.font.color.rgb = INK
    normal.paragraph_format.line_spacing = style.line_spacing

    p = _paragraph(doc, style, align=WD_ALIGN_PARAGRAPH.CENTER, after=0.8)
    _run(p, style, person["name"], offset=13.0, bold=True)

    if spec.get("headline"):
        p = _paragraph(doc, style, align=WD_ALIGN_PARAGRAPH.CENTER, after=1.5)
        _run(p, style, spec["headline"], offset=0.1, color=style.accent, bold=True)

    hidden = set(spec.get("hide_contact", []))
    if spec.get("hide_phone"):
        hidden.add("phone")
    contact = " | ".join(person[k] for k in CONTACT_ORDER if person.get(k) and k not in hidden)
    p = _paragraph(doc, style, align=WD_ALIGN_PARAGRAPH.CENTER, after=2.8)
    _run(p, style, contact, offset=-1.1, color=MUTED)

    _section(doc, style, "PROFILE")
    p = _paragraph(doc, style, before=1.4, after=1.4)
    _run(p, style, spec["summary"])

    if spec.get("alignment"):
        _section(doc, style, "ROLE ALIGNMENT")
        p = _paragraph(doc, style, before=1.4, after=1.4)
        _run(p, style, " | ".join(spec["alignment"]))

    _section(doc, style, "EXPERIENCE")
    _render_items(doc, style, spec["experience"])

    if spec.get("leadership"):
        _section(doc, style, "LEADERSHIP")
        _render_items(doc, style, spec["leadership"])

    if spec.get("projects"):
        _section(doc, style, "PROJECTS")
        for item in spec["projects"]:
            p = _paragraph(doc, style, before=2.6, after=1.5)
            _run(p, style, f'{item["title"]}: ', bold=True)
            _run(p, style, item["description"])
            if item.get("tools"):
                _run(p, style, f'  {item["tools"]}', italic=True, color=MUTED)

    _section(doc, style, "SKILLS & TOOLS")
    for i, group in enumerate(spec["skills"]):
        p = _paragraph(doc, style, before=1.5 if i == 0 else 0.65, after=0.65)
        _run(p, style, f'{group["label"]}: ', bold=True)
        _run(p, style, group["items"])

    _section(doc, style, "EDUCATION")
    for i, edu in enumerate(_education_entries(spec)):
        p = _paragraph(doc, style, before=3.2 if i == 0 else 2.2, after=0.7)
        _right_tab(p, style)
        _run(p, style, edu["degree"], offset=0.55, bold=True)
        if edu.get("school"):
            _run(p, style, "  |  ", color=FAINT)
            _run(p, style, edu["school"], italic=True, color=MUTED)
        right = " | ".join(part for part in (edu.get("location"), edu.get("date")) if part)
        if right:
            _run(p, style, "\t")
            _run(p, style, right, offset=-0.9, color=FAINT)
        if edu.get("detail"):
            p = _paragraph(doc, style, before=0.5, after=0.5)
            _run(p, style, edu["detail"], offset=-0.15, color=MUTED)

    return doc


def build_and_save(inputs: ResumeInputs, style: Style, output_path: Path) -> Path:
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    build_document(inputs, style).save(output_path)
    return output_path
