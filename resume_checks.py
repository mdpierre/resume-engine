"""Validation checks for a rendered resume PDF.

Pure, dependency-light checks the test-driven loop and the pytest gate both
call. Rendering goes DOCX -> PDF through LibreOffice (`soffice --headless`),
which is a true render of the .docx you submit. Measurement uses poppler
(`pdfinfo`, `pdftotext -bbox`), so nothing here needs pip packages beyond
`python-docx` (only used by the structure diff).

Checks implemented:
  * exactly one page
  * fill ratio within a caller-provided target band
  * no text overflows the page margins
  * required sections (contact, experience, skills) present, in order
  * structure diff against a reference .docx (used for company-tailored builds)
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

# --- Spec constants --------------------------------------------------------
MARGIN_IN = 0.5
MARGIN_PT = MARGIN_IN * 72.0
FILL_MIN = 0.95
FILL_MAX = 0.97
MIN_BODY_PT = 10.0
MAX_PAGES = 1
OVERFLOW_TOL_PT = 2.0  # words within 2pt of the margin are not "overflow"

# Core sections of the reference format, in canonical order.
REFERENCE_SECTIONS = ["PROFILE", "EXPERIENCE", "PROJECTS", "SKILLS & TOOLS", "EDUCATION"]
# Every heading the tailored builder may emit (uppercase, own line in the PDF).
KNOWN_HEADINGS = [
    "PROFILE",
    "ROLE ALIGNMENT",
    "EXPERIENCE",
    "LEADERSHIP",
    "PROJECTS",
    "SKILLS & TOOLS",
    "EDUCATION",
]

_SOFFICE_CANDIDATES = [
    os.environ.get("SOFFICE", ""),
    "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    "/usr/local/bin/soffice",
    "/opt/homebrew/bin/soffice",
    "/usr/bin/soffice",
    "soffice",
    "libreoffice",
]
_PDFTOTEXT_CANDIDATES = [
    os.environ.get("PDFTOTEXT", ""),
    "/opt/homebrew/bin/pdftotext",
    "/usr/local/bin/pdftotext",
    "/usr/bin/pdftotext",
    "pdftotext",
]


# --- Rendering -------------------------------------------------------------
def find_soffice() -> str:
    """Locate the LibreOffice binary or raise a clear error."""
    from shutil import which

    for cand in filter(None, _SOFFICE_CANDIDATES):
        if Path(cand).exists() or which(cand):
            return cand
    raise FileNotFoundError(
        "LibreOffice (soffice) not found. Install it (macOS: brew install --cask libreoffice) or set SOFFICE=/path/to/soffice"
    )


def find_pdftotext() -> str:
    """Locate Poppler's pdftotext binary or raise a clear error."""
    from shutil import which

    for candidate in filter(None, _PDFTOTEXT_CANDIDATES):
        if Path(candidate).exists() or which(candidate):
            return candidate
    raise FileNotFoundError(
        "Poppler (pdftotext) not found. Install it (macOS: brew install poppler) or set PDFTOTEXT=/path/to/pdftotext"
    )


def render_pdf(docx: Path, out_dir: Path | None = None) -> Path:
    """Render ``docx`` to PDF via LibreOffice and return the PDF path.

    Uses a throwaway profile dir so a running LibreOffice instance never blocks
    the headless convert.
    """
    docx = Path(docx)
    out_dir = Path(out_dir) if out_dir else docx.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    soffice = find_soffice()
    with tempfile.TemporaryDirectory(prefix="lo_profile_") as profile:
        proc = subprocess.run(
            [
                soffice,
                "--headless",
                f"-env:UserInstallation=file://{profile}",
                "--convert-to",
                "pdf",
                "--outdir",
                str(out_dir),
                str(docx),
            ],
            capture_output=True,
            text=True,
            timeout=120,
        )
    pdf = out_dir / (docx.stem + ".pdf")
    if not pdf.exists():
        raise RuntimeError(
            f"soffice did not produce {pdf}.\nstdout: {proc.stdout}\nstderr: {proc.stderr}"
        )
    return pdf


# --- PDF geometry ----------------------------------------------------------
@dataclass
class Word:
    x0: float
    y0: float
    x1: float
    y1: float


@dataclass
class Page:
    width: float
    height: float
    words: list[Word]


def parse_bbox(pdf: Path) -> list[Page]:
    """Parse word bounding boxes per page from ``pdftotext -bbox``."""
    xml = subprocess.run(
        [find_pdftotext(), "-bbox", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout
    pages: list[Page] = []
    for w, h, body in re.findall(
        r'<page width="([\d.]+)" height="([\d.]+)">(.*?)</page>', xml, re.S
    ):
        words = [
            Word(float(a), float(b), float(c), float(d))
            for a, b, c, d in re.findall(
                r'<word xMin="([\d.]+)" yMin="([\d.]+)" '
                r'xMax="([\d.]+)" yMax="([\d.]+)"',
                body,
            )
        ]
        pages.append(Page(float(w), float(h), words))
    return pages


def pdf_text(pdf: Path) -> str:
    """Plain text of the PDF in reading order (for section-order checks)."""
    return subprocess.run(
        [find_pdftotext(), "-layout", str(pdf), "-"],
        capture_output=True,
        text=True,
        timeout=60,
    ).stdout


# --- Individual checks -----------------------------------------------------
@dataclass
class CheckResult:
    name: str
    passed: bool
    detail: str


def fill_ratio(pages: list[Page]) -> float:
    """How far content reaches down page 1, as a fraction of printable height.

    1.0 means content touches the bottom margin. Computed on page 1 only; the
    page-count check handles multi-page overflow separately.
    """
    if not pages or not pages[0].words:
        return 0.0
    page = pages[0]
    bottom = max(w.y1 for w in page.words)
    available = page.height - 2 * MARGIN_PT
    return (bottom - MARGIN_PT) / available


def overflow_score(pages: list[Page]) -> float:
    """Monotonic density signal for the loop's search.

    On one page it equals the fill ratio (0..~1). When content spills onto
    later pages it returns >1, scaled by how much spilled, so a bisection on
    the build ``scale`` can bracket the one-page-90-98% band.
    """
    if not pages:
        return 0.0
    if len(pages) == 1:
        return fill_ratio(pages)
    total = sum(len(p.words) for p in pages) or 1
    spilled = sum(len(p.words) for p in pages[1:])
    return 1.0 + spilled / total


def check_page_count(pages: list[Page]) -> CheckResult:
    n = len(pages)
    return CheckResult(
        "page_count",
        n == MAX_PAGES,
        f"{n} page(s); expected exactly {MAX_PAGES}",
    )


def check_fill(
    pages: list[Page], fill_min: float = FILL_MIN, fill_max: float = FILL_MAX
) -> CheckResult:
    ratio = fill_ratio(pages)
    ok = len(pages) == 1 and fill_min <= ratio <= fill_max
    return CheckResult(
        "fill_ratio",
        ok,
        f"{ratio * 100:.1f}% (target {fill_min * 100:.0f}-{fill_max * 100:.0f}%)",
    )


def check_typography(docx: Path, min_body_pt: float = MIN_BODY_PT) -> CheckResult:
    """Require a readable Normal style instead of solving density by tiny type."""
    from docx import Document

    doc = Document(str(docx))
    size = doc.styles["Normal"].font.size
    body_pt = size.pt if size else 0.0
    return CheckResult(
        "readable_typography",
        body_pt >= min_body_pt,
        f"Normal style {body_pt:.2f} pt; minimum {min_body_pt:.2f} pt",
    )


def check_overflow(pages: list[Page]) -> CheckResult:
    offenders: list[str] = []
    for i, page in enumerate(pages, 1):
        left = MARGIN_PT - OVERFLOW_TOL_PT
        right = page.width - MARGIN_PT + OVERFLOW_TOL_PT
        top = MARGIN_PT - OVERFLOW_TOL_PT
        bottom = page.height - MARGIN_PT + OVERFLOW_TOL_PT
        for w in page.words:
            if w.x0 < left or w.x1 > right or w.y0 < top or w.y1 > bottom:
                offenders.append(
                    f"p{i} box=({w.x0:.0f},{w.y0:.0f},{w.x1:.0f},{w.y1:.0f})"
                )
    return CheckResult(
        "no_overflow",
        not offenders,
        "no text outside margins"
        if not offenders
        else f"{len(offenders)} word(s) past margin: {offenders[:3]}",
    )


def _heading_line_index(lines: list[str], heading: str) -> int:
    """Line index of a heading rendered on its own line, else -1.

    Matches whole standalone lines so body words (e.g. 'experience' inside the
    profile paragraph) never masquerade as the EXPERIENCE section heading.
    """
    for i, line in enumerate(lines):
        if line.strip().upper() == heading:
            return i
    return -1


def check_section_order(pdf: Path) -> CheckResult:
    """Required sections present and in order: contact, experience, skills.

    'contact' is located by the email address near the top; section headings
    are matched as standalone uppercase lines in reading order.
    """
    text = pdf_text(pdf)
    lines = text.splitlines()

    email_line = next(
        (i for i, ln in enumerate(lines) if re.search(r"[\w.+-]+@[\w.-]+\.\w+", ln)),
        -1,
    )
    exp_line = _heading_line_index(lines, "EXPERIENCE")
    skills_line = _heading_line_index(lines, "SKILLS & TOOLS")

    missing = [
        name
        for name, p in [
            ("contact", email_line),
            ("experience", exp_line),
            ("skills", skills_line),
        ]
        if p < 0
    ]
    if missing:
        return CheckResult(
            "section_order", False, f"missing required section(s): {missing}"
        )

    ordered = email_line < exp_line < skills_line
    detected = detected_heading_order(text)
    return CheckResult(
        "section_order",
        ordered,
        f"contact<experience<skills = {ordered}; detected order: {detected}",
    )


def detected_heading_order(text: str) -> list[str]:
    """Headings that appear as standalone lines, in reading order."""
    lines = text.splitlines()
    found = [
        (idx, h) for h in KNOWN_HEADINGS if (idx := _heading_line_index(lines, h)) >= 0
    ]
    return [h for _, h in sorted(found)]


# --- Structure diff against a reference .docx ------------------------------
def docx_headings(docx: Path) -> list[str]:
    """Section headings of a .docx (paragraphs carrying a bottom border rule)."""
    from docx import Document

    doc = Document(str(docx))
    headings = []
    for p in doc.paragraphs:
        text = p.text.strip()
        if text and "pBdr" in p._p.xml:
            headings.append(text.upper())
    return headings


def docx_format_profile(docx: Path) -> dict:
    """Formatting fingerprint used for reference parity."""
    from docx import Document

    doc = Document(str(docx))
    normal = doc.styles["Normal"]
    sec = doc.sections[0]
    name_size = None
    for p in doc.paragraphs:
        sizes = [r.font.size.pt for r in p.runs if r.font.size]
        if sizes and max(sizes) >= 20:  # the name line
            name_size = max(sizes)
            break
    return {
        "font": normal.font.name,
        "base_pt": normal.font.size.pt if normal.font.size else None,
        "margins_in": (
            round(sec.top_margin.inches, 3),
            round(sec.bottom_margin.inches, 3),
            round(sec.left_margin.inches, 3),
            round(sec.right_margin.inches, 3),
        ),
        "name_pt": name_size,
    }


@dataclass
class StructureDiff:
    passed: bool
    reference_order_preserved: bool
    missing_reference_sections: list[str] = field(default_factory=list)
    tailored_insertions: list[str] = field(default_factory=list)
    format_mismatches: list[str] = field(default_factory=list)
    generated_headings: list[str] = field(default_factory=list)
    reference_headings: list[str] = field(default_factory=list)

    def as_result(self) -> CheckResult:
        bits = []
        if self.missing_reference_sections:
            bits.append(f"missing {self.missing_reference_sections}")
        if self.format_mismatches:
            bits.append(f"format: {self.format_mismatches}")
        if self.tailored_insertions:
            bits.append(f"tailored insertions (ok): {self.tailored_insertions}")
        detail = "; ".join(bits) if bits else "matches reference format"
        return CheckResult("reference_structure", self.passed, detail)


def reference_structure_diff(generated_docx: Path, reference_docx: Path) -> StructureDiff:
    """Compare a generated .docx to the reference format.

    Passes when every reference section appears in the generated resume in the
    same relative order, and the formatting fingerprint (font, base size,
    margins, name size) matches. Extra sections in the generated resume are
    reported as intentional tailored insertions, not failures.
    """
    gen = docx_headings(generated_docx)
    ref = docx_headings(reference_docx)
    ref_core = [h for h in ref if h in REFERENCE_SECTIONS]

    # Every reference section present, in the same relative order?
    missing = [h for h in ref_core if h not in gen]
    gen_subseq = [h for h in gen if h in ref_core]
    order_ok = gen_subseq == ref_core and not missing

    insertions = [h for h in gen if h not in ref_core]

    # Formatting parity.
    gf, rf = docx_format_profile(generated_docx), docx_format_profile(reference_docx)
    mism = []
    if gf["font"] != rf["font"]:
        mism.append(f"font {gf['font']}!={rf['font']}")
    if gf["margins_in"] != rf["margins_in"]:
        mism.append(f"margins {gf['margins_in']}!={rf['margins_in']}")
    # base_pt scales with the loop, so compare the shape not the exact value:
    if rf["base_pt"] and gf["base_pt"] and abs(gf["base_pt"] - rf["base_pt"]) > 2.0:
        mism.append(f"base_pt {gf['base_pt']}!={rf['base_pt']}")

    passed = order_ok and not mism
    return StructureDiff(
        passed=passed,
        reference_order_preserved=order_ok,
        missing_reference_sections=missing,
        tailored_insertions=insertions,
        format_mismatches=mism,
        generated_headings=gen,
        reference_headings=ref,
    )


# --- Aggregate -------------------------------------------------------------
@dataclass
class Report:
    pages: int
    fill: float
    checks: list[CheckResult]

    @property
    def passed(self) -> bool:
        return all(c.passed for c in self.checks)


def evaluate(
    pdf: Path,
    reference_docx: Path | None = None,
    generated_docx: Path | None = None,
    *,
    fill_min: float = FILL_MIN,
    fill_max: float = FILL_MAX,
    min_body_pt: float = MIN_BODY_PT,
) -> Report:
    """Run every check against a rendered PDF (+ optional reference diff)."""
    pages = parse_bbox(pdf)
    checks = [
        check_page_count(pages),
        check_fill(pages, fill_min, fill_max),
        check_overflow(pages),
        check_section_order(pdf),
    ]
    if generated_docx:
        checks.append(check_typography(generated_docx, min_body_pt))
    if reference_docx and generated_docx:
        checks.append(
            reference_structure_diff(generated_docx, reference_docx).as_result()
        )
    return Report(pages=len(pages), fill=fill_ratio(pages), checks=checks)
