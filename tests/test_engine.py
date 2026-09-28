"""Standard-library tests for the engine, run against the bundled example.

    python -m unittest -v tests/test_engine.py

Set RESUME_ARTIFACT_DIR=<output dir> to also gate a produced resume.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import resume_checks as rc  # noqa: E402
from resume_factory import Style, build_and_save, load_inputs, profile_contact  # noqa: E402

EXAMPLE = ROOT / "example"
FACTS = EXAMPLE / "resume_facts.json"
SPEC = EXAMPLE / "specs" / "base.json"


def _write(tmp: str, name: str, data) -> Path:
    path = Path(tmp) / name
    path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
    return path


class FactoryTests(unittest.TestCase):
    def test_example_is_evidence_gated(self):
        inputs = load_inputs(SPEC, FACTS)
        self.assertEqual(inputs.person["name"], "Jordan Rivera")
        self.assertTrue(inputs.spec["summary_evidence"])

    def test_contact_parsed_from_profile(self):
        person = profile_contact(EXAMPLE / "PROFILE.md")
        self.assertEqual(person["email"], "jordan.rivera@example.com")
        self.assertEqual(person["linkedin"], "linkedin.com/in/jrivera")

    def test_rejects_unknown_evidence(self):
        bad = json.loads(SPEC.read_text(encoding="utf-8"))
        bad["summary_evidence"].append("unsupported.fact")
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "unknown evidence"):
                load_inputs(_write(tmp, "bad.json", bad), FACTS)

    def test_rejects_uncited_bullet(self):
        bad = json.loads(SPEC.read_text(encoding="utf-8"))
        bad["experience"][0]["bullets"][0]["evidence"] = []
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "must cite evidence"):
                load_inputs(_write(tmp, "bad.json", bad), FACTS)

    def test_rejects_em_dash(self):
        bad = json.loads(SPEC.read_text(encoding="utf-8"))
        bad["summary"] += " — oops"
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "non-ASCII dash"):
                load_inputs(_write(tmp, "bad.json", bad), FACTS)

    def test_rejects_person_drift(self):
        facts = json.loads(FACTS.read_text(encoding="utf-8"))
        facts["canonical_profile"] = str(EXAMPLE / "PROFILE.md")
        facts["person"] = {"name": "Someone Else"}
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaisesRegex(ValueError, "disagrees with PROFILE.md"):
                load_inputs(SPEC, _write(tmp, "facts.json", facts))

    def test_default_body_is_readable(self):
        inputs = load_inputs(SPEC, FACTS)
        with tempfile.TemporaryDirectory() as tmp:
            docx = build_and_save(inputs, inputs.base_style(), Path(tmp) / "resume.docx")
            check = rc.check_typography(docx, 10.0)
            self.assertTrue(check.passed, check.detail)


class ProducedArtifactTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        artifact_dir = os.environ.get("RESUME_ARTIFACT_DIR")
        if not artifact_dir:
            raise unittest.SkipTest("set RESUME_ARTIFACT_DIR to test a produced resume")
        root = Path(artifact_dir)
        cls.docx = next(iter(sorted(root.glob("*.docx"))), None)
        cls.pdf = next(iter(sorted(root.glob("*.pdf"))), None)
        if not cls.docx or not cls.pdf:
            raise unittest.SkipTest(f"resume DOCX/PDF not found in {root}")
        cls.report = rc.evaluate(cls.pdf, generated_docx=cls.docx)

    def test_all_checks_pass(self):
        failed = [f"{c.name}: {c.detail}" for c in self.report.checks if not c.passed]
        self.assertFalse(failed, "\n".join(failed))


if __name__ == "__main__":
    unittest.main(verbosity=2)
