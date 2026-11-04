"""One job, start to finish: a resume file + a job description -> a tailored one-page PDF/DOCX.

This is the seam between transports (Telegram, a CLI, a future web form) and the product.
Everything a job touches lives in its own work directory, which the caller deletes.

    python pipeline.py my-resume.pdf job.txt -o ./out      # run the full flow locally, no Telegram

The engine itself is untouched: this module writes data/ files and runs `resume.py` as a
subprocess, exactly like a person would. `resume.py` still works on its own.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from claude_adapter import AdapterError, ResumeAdapter

ROOT = Path(__file__).resolve().parent
ENGINE = ROOT / "resume.py"
log = logging.getLogger(__name__)

BUILD_TIMEOUT = float(os.environ.get("BUILD_TIMEOUT_SECONDS", "420"))
CLAUDE_TIMEOUT = float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "300"))
MAX_REPAIRS = int(os.environ.get("MAX_SPEC_REPAIRS", "2"))
MIN_RESUME_CHARS = 200
MAX_RESUME_CHARS = 30_000


class PipelineError(Exception):
    """A job failed; ``user_message`` is safe to show the person who submitted it."""

    def __init__(self, message: str, user_message: str):
        super().__init__(message)
        self.user_message = user_message


@dataclass
class JobResult:
    pdf: Path
    docx: Path
    fill_ratio: float
    attempts: int
    covered_keywords: list[str] = field(default_factory=list)
    unsupported_keywords: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)


# --- input ------------------------------------------------------------------------


def extract_text(path: Path, timeout: float = 60) -> str:
    """Plain text of an uploaded PDF or DOCX, using the tools the engine already needs."""
    suffix = path.suffix.lower()
    try:
        if suffix == ".pdf":
            import resume_checks

            text = subprocess.run(
                [resume_checks.find_pdftotext(), "-layout", str(path), "-"],
                capture_output=True, text=True, timeout=timeout, check=True,
            ).stdout
        elif suffix == ".docx":
            import docx

            document = docx.Document(str(path))
            lines = [p.text for p in document.paragraphs]
            for table in document.tables:
                for row in table.rows:
                    lines.append(" | ".join(cell.text for cell in row.cells))
            text = "\n".join(lines)
        else:
            raise PipelineError(f"unsupported file type {suffix}", "Please send a PDF or DOCX file.")
    except PipelineError:
        raise
    except Exception as exc:  # corrupt or encrypted files land here
        raise PipelineError(f"text extraction failed: {exc}", "I couldn't read that file. Is it a normal, unlocked PDF or DOCX?") from exc

    text = re.sub(r"[ \t]+\n", "\n", text).strip()
    if len(text) < MIN_RESUME_CHARS:
        raise PipelineError(
            f"only {len(text)} chars extracted",
            "I couldn't find much text in that file. If it's a scanned image, export a text PDF or DOCX instead.",
        )
    if len(text) > MAX_RESUME_CHARS:
        raise PipelineError(f"{len(text)} chars extracted", "That file is too long for a resume. Send a shorter one.")
    return text


# --- engine -------------------------------------------------------------------------


def run_engine(args: list[str], timeout: float) -> tuple[int, str]:
    """Run ``resume.py`` with a hard timeout that also kills its LibreOffice children."""
    proc = subprocess.Popen(
        [sys.executable, str(ENGINE), *args],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
        start_new_session=True, cwd=ROOT,
    )
    try:
        output, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
        proc.communicate()
        raise PipelineError(f"engine timed out after {timeout:.0f}s", "Rendering took too long. Please try again.")
    return proc.returncode, output


def _engine_feedback(output: str) -> str:
    """The part of a failed engine run worth showing Claude: the error and the last fit iterations."""
    lines = [line for line in output.strip().splitlines() if line.strip()]
    iters = [line.strip() for line in lines if line.strip().startswith("iter")]
    errors = [line.strip() for line in lines if re.match(r"\s*(\w+Error|FileNotFoundError):", line)]
    hint = ""
    if iters:
        last = iters[-1]
        pages = re.search(r"pages=(\d+)", last)
        if pages and int(pages.group(1)) > 1:
            hint = "The content runs past one page even at the tightest spacing: cut the least relevant bullets or a role."
        else:
            hint = "The page is underfilled even at the loosest spacing: add a relevant, cited bullet or project."
    return "\n".join([*errors[-3:], *iters[-4:], hint]).strip() or "\n".join(lines[-8:])


# --- the job ------------------------------------------------------------------------


def _remaining(deadline: float, step: str) -> float:
    left = deadline - time.monotonic()
    if left < 15:
        raise PipelineError(f"deadline reached before {step}", "This took too long and was stopped. Please try again.")
    return left


def run_job(
    resume_text: str,
    job_description: str,
    workdir: Path,
    adapter: ResumeAdapter,
    *,
    deadline: float | None = None,
    max_repairs: int = MAX_REPAIRS,
) -> JobResult:
    """Import the profile, tailor a spec, and build it, repairing the spec from engine feedback."""
    deadline = deadline or time.monotonic() + 900
    data = workdir / "data"
    out = workdir / "output"
    (data / "specs").mkdir(parents=True, exist_ok=True)

    try:
        draft = adapter.import_profile(resume_text, timeout=min(CLAUDE_TIMEOUT, _remaining(deadline, "import")))
    except AdapterError as exc:
        raise PipelineError(str(exc), exc.user_message) from exc
    (data / "PROFILE.md").write_text(draft.profile_md, encoding="utf-8")
    (data / "resume_facts.json").write_text(json.dumps(draft.facts, indent=2) + "\n", encoding="utf-8")
    log.info("profile imported: %d facts", len(draft.facts["facts"]))

    spec_path = data / "specs" / "job.json"
    previous = feedback = None
    for attempt in range(1, max_repairs + 2):
        try:
            result = adapter.write_spec(
                draft.profile_md, draft.facts, job_description,
                timeout=min(CLAUDE_TIMEOUT, _remaining(deadline, "spec")),
                previous_spec=previous, feedback=feedback,
            )
        except AdapterError as exc:
            raise PipelineError(str(exc), exc.user_message) from exc
        spec_path.write_text(json.dumps(result.spec, indent=2) + "\n", encoding="utf-8")

        code, output = run_engine(["--data", str(data), "validate", str(spec_path)], timeout=60)
        if code == 0:
            if out.exists():
                shutil.rmtree(out)
            timeout = min(BUILD_TIMEOUT, _remaining(deadline, "build"))
            started = time.monotonic()
            code, output = run_engine(
                ["--data", str(data), "build", str(spec_path), "--output-dir", str(out)], timeout=timeout
            )
            log.info("build attempt %d: exit=%d in %.0fs", attempt, code, time.monotonic() - started)
            if code == 0:
                report = json.loads((out / "resume-validation.json").read_text(encoding="utf-8"))
                return JobResult(
                    pdf=Path(report["output_pdf"]),
                    docx=Path(report["output_docx"]),
                    fill_ratio=report["fill_ratio"],
                    attempts=attempt,
                    covered_keywords=result.covered_keywords,
                    unsupported_keywords=result.unsupported_keywords,
                    questions=draft.questions,
                )
        if "Contact block is missing" in output:
            raise PipelineError(
                "profile has no name/email",
                "Your resume needs your name and an email address at the top. Add them and send it again.",
            )
        feedback = _engine_feedback(output)
        previous = result.spec
        log.info("spec attempt %d rejected: %s", attempt, feedback.splitlines()[0] if feedback else "?")

    raise PipelineError(
        f"no passing spec after {max_repairs + 1} attempts: {feedback}",
        "I couldn't fit this resume onto one clean page. Try a shorter resume or a more focused job description.",
    )


# --- local CLI -------------------------------------------------------------------------


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the full resume job locally (needs ANTHROPIC_API_KEY).")
    parser.add_argument("resume", type=Path, help="resume as .pdf or .docx")
    parser.add_argument("job", type=Path, help="job description as a .txt file")
    parser.add_argument("-o", "--out", type=Path, default=Path("output") / "pipeline")
    parser.add_argument("--keep-workdir", action="store_true", help="keep data/ and the spec for inspection")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    from claude_adapter import AnthropicAdapter

    workdir = Path(tempfile.mkdtemp(prefix="resume_job_"))
    try:
        result = run_job(extract_text(args.resume), args.job.read_text(encoding="utf-8"), workdir, AnthropicAdapter())
        args.out.mkdir(parents=True, exist_ok=True)
        for path in (result.pdf, result.docx):
            shutil.copy(path, args.out / path.name)
        print(f"PASS in {result.attempts} attempt(s): {result.fill_ratio * 100:.1f}% fill -> {args.out.resolve()}")
        if result.unsupported_keywords:
            print("Unsupported keywords:", ", ".join(result.unsupported_keywords))
        return 0
    except PipelineError as exc:
        print(f"FAILED: {exc}", file=sys.stderr)
        return 1
    finally:
        if args.keep_workdir:
            print(f"workdir kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
