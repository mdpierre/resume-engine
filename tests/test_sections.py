"""Section layout tests: section_order, section_titles, and custom sections.

    python -m unittest -v tests/test_sections.py
"""

from __future__ import annotations

import copy
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import resume_checks as rc  # noqa: E402
from resume_factory import (  # noqa: E402
    DEFAULT_SECTION_ORDER,
    build_and_save,
    load_inputs,
    section_headings,
)

EXAMPLE = ROOT / "example"
FACTS = EXAMPLE / "resume_facts.json"
SPEC = EXAMPLE / "specs" / "base.json"
DEFAULT_HEADINGS = ["PROFILE", "ROLE ALIGNMENT", "EXPERIENCE", "LEADERSHIP", "PROJECTS", "SKILLS & TOOLS", "EDUCATION"]


def _has_soffice() -> bool:
    try:
        rc.find_soffice()
        rc.find_pdftotext()
    except FileNotFoundError:
        return False
    return True


class SectionLayoutTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.spec = json.loads(SPEC.read_text(encoding="utf-8"))
        self.evidence = ["experience.lakeshore_dashboard"]

    def inputs(self, spec=None):
        path = self.root / "spec.json"
        path.write_text(json.dumps(self.spec if spec is None else spec), encoding="utf-8")
        return load_inputs(path, FACTS)

    def render(self, spec=None, name="resume"):
        inputs = self.inputs(spec)
        return build_and_save(inputs, inputs.base_style(), self.root / f"{name}.docx")

    def add_custom(self):
        self.spec["sections"] = [
            {"id": "specialties", "title": "Specialties", "type": "skills-list", "items": [
                {"label": "Reporting", "items": "Looker dashboards", "evidence": ["skills.data"]}]},
            {"id": "volunteering", "title": "Volunteering", "type": "entries", "items": [
                {"title": "Inventory Volunteer", "organization": "Neighborhood Food Pantry", "location": "Chicago, IL",
                 "dates": "2022", "evidence": ["project.food_pantry"],
                 "bullets": [{"text": "Built a Google Sheets inventory used by 8 volunteers.",
                              "evidence": ["project.food_pantry"]}]}]},
            {"id": "highlights", "title": "Highlights", "type": "bullets", "items": [
                {"text": "Cut weekly report prep from about 6 hours to under 1.", "evidence": self.evidence}]},
        ]

    # --- Rendering ---------------------------------------------------------

    def test_default_order_unchanged(self):
        self.assertEqual(section_headings(self.spec), DEFAULT_HEADINGS)
        implicit = self.render(name="implicit")
        self.spec["section_order"] = list(DEFAULT_SECTION_ORDER)
        explicit = self.render(name="explicit")
        self.assertEqual(rc.docx_headings(implicit), DEFAULT_HEADINGS)
        with zipfile.ZipFile(implicit) as a, zipfile.ZipFile(explicit) as b:
            self.assertEqual(a.read("word/document.xml"), b.read("word/document.xml"))

    def test_reorder(self):
        order = list(DEFAULT_SECTION_ORDER)
        order.remove("skills")
        order.insert(1, "skills")
        self.spec["section_order"] = order
        headings = rc.docx_headings(self.render())
        self.assertEqual(headings, section_headings(self.spec))
        self.assertLess(headings.index("SKILLS & TOOLS"), headings.index("EXPERIENCE"))

    def test_rename(self):
        self.spec["section_titles"] = {"experience": "Relevant Experience", "summary": "Summary"}
        headings = rc.docx_headings(self.render())
        self.assertIn("RELEVANT EXPERIENCE", headings)
        self.assertEqual(headings[0], "SUMMARY")
        self.assertNotIn("EXPERIENCE", headings)

    def test_custom_sections_all_types(self):
        from docx import Document

        self.add_custom()
        self.spec["section_titles"] = {"highlights": "Selected Highlights"}
        docx = self.render()
        self.assertEqual(
            rc.docx_headings(docx)[-5:],
            ["SKILLS & TOOLS", "SPECIALTIES", "VOLUNTEERING", "SELECTED HIGHLIGHTS", "EDUCATION"],
        )
        text = "\n".join(p.text for p in Document(str(docx)).paragraphs)
        for value in ("Reporting: Looker dashboards", "Inventory Volunteer", "Neighborhood Food Pantry",
                      "Built a Google Sheets inventory used by 8 volunteers.",
                      "Cut weekly report prep from about 6 hours to under 1."):
            self.assertIn(value, text)

    def test_omitted_summary(self):
        for key in ("summary", "summary_evidence"):
            self.spec.pop(key)
        self.spec["section_order"] = ["experience", "skills", "education"]
        self.inputs()
        headings = rc.docx_headings(self.render())
        self.assertEqual(headings, ["EXPERIENCE", "SKILLS & TOOLS", "EDUCATION"])
        self.assertNotIn("PROFILE", headings)

    def test_summary_still_required_when_in_order(self):
        self.spec.pop("summary")
        with self.assertRaisesRegex(ValueError, r"missing required keys: \['summary'\]"):
            self.inputs()
        self.spec["summary"] = "Operations analyst."
        self.spec.pop("summary_evidence")
        with self.assertRaisesRegex(ValueError, "summary_evidence"):
            self.inputs()

    def test_skills_block_optional(self):
        self.spec.pop("skills")
        self.assertNotIn("SKILLS & TOOLS", section_headings(self.spec))
        self.inputs()
        self.spec["section_order"] = ["summary", "experience", "education"]
        self.inputs()

    def test_empty_optional_sections_skipped(self):
        for key in ("alignment", "leadership", "projects"):
            self.spec[key] = []
        self.assertEqual(section_headings(self.spec), ["PROFILE", "EXPERIENCE", "SKILLS & TOOLS", "EDUCATION"])

    # --- Rejection ---------------------------------------------------------

    def test_invalid_layout(self):
        cases = [
            ({"section_order": DEFAULT_SECTION_ORDER + ["unknown"]}, "unknown section ids"),
            ({"section_order": DEFAULT_SECTION_ORDER + ["skills"]}, "duplicate ids"),
            ({"section_order": ["summary", "skills", "education"]}, r"missing required sections: \['experience'\]"),
            ({"section_order": ["summary", "experience", "education"]}, r"missing required sections: \['skills'\]"),
            ({"section_order": "summary,experience"}, "must be a list"),
            ({"section_titles": {"unknown": "Unknown"}}, "unknown section_titles keys"),
            ({"section_titles": {"experience": "  "}}, "nonempty strings"),
            ({"section_titles": {"experience": 3}}, "nonempty strings"),
            ({"section_titles": ["experience"]}, "section_titles must be an object"),
        ]
        for change, message in cases:
            with self.subTest(change=change), self.assertRaisesRegex(ValueError, message):
                self.inputs({**self.spec, **change})

    def test_custom_missing_from_explicit_order(self):
        self.add_custom()
        self.spec["section_order"] = list(DEFAULT_SECTION_ORDER)
        with self.assertRaisesRegex(ValueError, "missing required sections"):
            self.inputs()

    def test_invalid_custom_schema(self):
        self.add_custom()
        cases = [
            ("id", "Bad id", "invalid custom section id"),
            ("id", "summary", "invalid custom section id"),
            ("type", "table", "unknown section type"),
            ("items", [], "items must be a nonempty list"),
            ("title", " ", "title must be a nonempty string"),
        ]
        for field, value, message in cases:
            bad = copy.deepcopy(self.spec)
            bad["sections"][0][field] = value
            with self.subTest(field=field, value=value), self.assertRaisesRegex(ValueError, message):
                self.inputs(bad)
        self.spec["sections"].append(copy.deepcopy(self.spec["sections"][0]))
        with self.assertRaisesRegex(ValueError, "duplicate section id: specialties"):
            self.inputs()

    def test_uncited_custom_items(self):
        self.add_custom()
        for index, section in enumerate(self.spec["sections"]):
            bad = copy.deepcopy(self.spec)
            bad["sections"][index]["items"][0]["evidence"] = []
            pattern = rf"sections\[{section['id']}\]\.items\[0\] must cite at least one evidence id"
            with self.subTest(section=section["id"]), self.assertRaisesRegex(ValueError, pattern):
                self.inputs(bad)
        self.spec["sections"][1]["items"][0]["bullets"][0].pop("evidence")
        with self.assertRaisesRegex(
            ValueError, r"sections\[volunteering\]\.items\[0\]\.bullets\[0\] must cite at least one evidence id"
        ):
            self.inputs()

    def test_unknown_custom_evidence(self):
        self.add_custom()
        for nested in (False, True):
            bad = copy.deepcopy(self.spec)
            item = bad["sections"][1]["items"][0]
            if nested:
                item = item["bullets"][0]
            item["evidence"] = ["unsupported.fact"]
            with self.subTest(nested=nested), self.assertRaisesRegex(ValueError, "unknown evidence ids"):
                self.inputs(bad)

    def test_custom_text_rejects_em_dash(self):
        self.add_custom()
        self.spec["sections"][2]["items"][0]["text"] += " — oops"
        with self.assertRaisesRegex(ValueError, "non-ASCII dash"):
            self.inputs()

    # --- Checks ------------------------------------------------------------

    def test_check_section_order_with_expected_headings(self):
        headings = ["EDUCATION", "SKILLS & TOOLS", "RELEVANT EXPERIENCE"]
        text = "Jordan Rivera\njordan.rivera@example.com\n" + "\n".join(headings)
        self.assertEqual(rc.detected_heading_order(text, headings), headings)
        with patch.object(rc, "pdf_text", return_value=text):
            self.assertTrue(rc.check_section_order(Path("unused.pdf"), headings).passed)
            self.assertFalse(rc.check_section_order(Path("unused.pdf"), headings[::-1]).passed)
            self.assertFalse(rc.check_section_order(Path("unused.pdf"), headings + ["MISSING"]).passed)
            # The default check still wants EXPERIENCE before SKILLS & TOOLS.
            self.assertFalse(rc.check_section_order(Path("unused.pdf")).passed)
        for bad in (
            "\n".join(headings) + "\njordan.rivera@example.com",
            text.replace("\nEDUCATION", "\nBody mentions EDUCATION here"),
        ):
            with self.subTest(text=bad), patch.object(rc, "pdf_text", return_value=bad):
                self.assertFalse(rc.check_section_order(Path("unused.pdf"), headings).passed)

    @unittest.skipUnless(_has_soffice(), "LibreOffice/Poppler not installed")
    def test_reordered_pdf_passes_section_check(self):
        self.spec["section_order"] = ["education", "skills", "summary", "experience", "leadership", "projects"]
        self.spec["section_titles"] = {"experience": "Relevant Experience"}
        pdf = rc.render_pdf(self.render(), self.root)
        expected = section_headings(self.spec)
        check = rc.check_section_order(pdf, expected)
        self.assertTrue(check.passed, check.detail)
        self.assertFalse(rc.check_section_order(pdf).passed)
        report = rc.evaluate(pdf, expected_headings=expected)
        self.assertTrue(next(c for c in report.checks if c.name == "section_order").passed)

    def test_every_example_spec_validates(self):
        specs = sorted((EXAMPLE / "specs").glob("*.json"))
        self.assertGreaterEqual(len(specs), 2)
        for spec_path in specs:
            with self.subTest(spec=spec_path.name):
                headings = section_headings(load_inputs(spec_path, FACTS).spec)
                self.assertTrue(any("EXPERIENCE" in heading for heading in headings), headings)
                self.assertIn("EDUCATION", headings)


if __name__ == "__main__":
    unittest.main(verbosity=2)
