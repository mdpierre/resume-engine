"""Resume engine CLI.

  python resume.py init                      copy the example into ./data to start from
  python resume.py validate <spec>           check a spec's evidence without rendering
  python resume.py build <spec> [options]    fit, render, and validate a one-page resume

<spec> is a path, or a bare name resolved against <data>/specs/<name>.json.

The fit loop targets 95-97% printable-height fill (96% ideal). It tunes
vertical spacing first and refuses to shrink body text below 10 pt; if the
content can't fit, it stops and tells you to cut content instead.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import sys
import tempfile
from dataclasses import replace
from pathlib import Path

import resume_checks as rc
from resume_factory import ResumeInputs, build_and_save, load_inputs

ROOT = Path(__file__).resolve().parent
EXAMPLE_DIR = ROOT / "example"


def default_data_dir() -> Path:
    return Path(os.environ.get("RESUME_DATA", ROOT / "data"))


def slugify(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def resolve_spec(spec: str, data_dir: Path) -> Path:
    path = Path(spec)
    if path.exists():
        return path
    named = data_dir / "specs" / (spec if spec.endswith(".json") else f"{spec}.json")
    if named.exists():
        return named
    raise FileNotFoundError(f"spec not found: {spec} (also looked for {named})")


def acronym(inputs: ResumeInputs) -> str:
    if inputs.spec.get("acronym"):
        return inputs.spec["acronym"]
    words = re.findall(r"[A-Za-z0-9]+", inputs.spec["role"])
    return "".join(w[0].upper() for w in words) or "Role"


def output_stem(inputs: ResumeInputs) -> str:
    return f'{inputs.person["name"]} - {acronym(inputs)} Resume'


def _geometry_ok(report: rc.Report) -> bool:
    names = {"page_count", "fill_ratio", "no_overflow", "readable_typography"}
    return all(check.passed for check in report.checks if check.name in names)


def search(inputs, work, reference, *, fill_min, fill_max, ideal, min_body_pt) -> dict:
    """Tune spacing first at readable body sizes and return the closest pass."""
    base = inputs.base_style()
    body_sizes = [s for s in (10.5, 10.25, 10.0, 10.75, 11.0) if s >= min_body_pt]
    best: dict | None = None
    iteration = 0

    for body_pt in body_sizes:
        lo, hi = 0.62, 1.65
        seen: set[float] = set()
        for _ in range(14):
            spacing = round((lo + hi) / 2, 4)
            if spacing in seen:
                break
            seen.add(spacing)
            iteration += 1
            docx = build_and_save(
                inputs, replace(base, body_pt=body_pt, spacing_scale=spacing), work / "candidate.docx"
            )
            pdf = rc.render_pdf(docx, work)
            report = rc.evaluate(
                pdf,
                reference_docx=reference,
                generated_docx=docx,
                fill_min=fill_min,
                fill_max=fill_max,
                min_body_pt=min_body_pt,
            )
            score = rc.overflow_score(rc.parse_bbox(pdf))
            ok = _geometry_ok(report)
            print(
                f"  iter {iteration:2d}: body={body_pt:4.2f}pt spacing={spacing:5.3f} "
                f"pages={report.pages} fill={report.fill * 100:5.1f}% {'PASS' if ok else 'adjust'}"
            )

            if ok and (best is None or abs(report.fill - ideal) < abs(best["fill"] - ideal)):
                shutil.copy(docx, work / "best.docx")
                shutil.copy(pdf, work / "best.pdf")
                best = {
                    "body_pt": body_pt,
                    "spacing": spacing,
                    "docx": work / "best.docx",
                    "pdf": work / "best.pdf",
                    "report": report,
                    "fill": report.fill,
                }
                if abs(report.fill - ideal) <= 0.0015:
                    return best

            if score > ideal or report.pages > 1:
                hi = spacing
            else:
                lo = spacing

        if best is not None:
            return best

    raise RuntimeError(
        "No readable one-page layout reached the requested fill band. If pages > 1, "
        "cut the least relevant bullet or role; if fill stays low, add a bullet or "
        f"project. The engine will not shrink below {min_body_pt:.1f} pt."
    )


def cmd_init(args) -> int:
    data_dir = args.data
    if data_dir.exists() and any(data_dir.iterdir()) and not args.force:
        print(f"{data_dir} already has files; pass --force to overwrite with the example.")
        return 1
    shutil.copytree(EXAMPLE_DIR, data_dir, dirs_exist_ok=True)
    print(f"Copied the example profile into {data_dir}. Replace PROFILE.md with your own,")
    print("then regenerate resume_facts.json and specs/ (the resume-setup skill does this).")
    return 0


def cmd_validate(args) -> int:
    spec = resolve_spec(args.spec, args.data)
    inputs = load_inputs(spec, args.facts or args.data / "resume_facts.json")
    print(f"OK: {spec.name} cites only known facts for {inputs.person['name']}")
    return 0


def cmd_build(args) -> int:
    if not (0 < args.target_min <= args.ideal <= args.target_max < 1):
        print("require 0 < target-min <= ideal <= target-max < 1", file=sys.stderr)
        return 2

    spec = resolve_spec(args.spec, args.data)
    inputs = load_inputs(spec, args.facts or args.data / "resume_facts.json")
    if args.hide_phone:
        inputs.spec["hide_phone"] = True
    reference = args.reference if args.reference and args.reference.exists() else None
    slug = slugify(f'{inputs.spec["company"]}-{inputs.spec["role"]}')
    output_dir = (args.output_dir or ROOT / "output" / f"{dt.date.today():%Y-%m-%d}-{slug}").resolve()

    print(f'Building {inputs.spec["company"]} - {inputs.spec["role"]} for {inputs.person["name"]}')
    print(
        f"Target: {args.target_min * 100:.0f}-{args.target_max * 100:.0f}% fill, "
        f"ideal {args.ideal * 100:.0f}%, body >= {args.min_body_pt:.1f} pt"
    )

    with tempfile.TemporaryDirectory(prefix="resume_engine_") as tmp:
        result = search(
            inputs,
            Path(tmp),
            reference,
            fill_min=args.target_min,
            fill_max=args.target_max,
            ideal=args.ideal,
            min_body_pt=args.min_body_pt,
        )
        output_dir.mkdir(parents=True, exist_ok=True)
        stem = output_stem(inputs)
        out_docx = output_dir / f"{stem}.docx"
        out_pdf = output_dir / f"{stem}.pdf"
        shutil.copy(result["docx"], out_docx)
        shutil.copy(result["pdf"], out_pdf)

    report: rc.Report = result["report"]
    payload = {
        "company": inputs.spec["company"],
        "role": inputs.spec["role"],
        "source_spec": str(inputs.spec_path),
        "source_facts": str(inputs.facts_path),
        "canonical_profile": str(inputs.profile_path),
        "body_pt": result["body_pt"],
        "spacing_scale": result["spacing"],
        "fill_ratio": round(result["fill"], 4),
        "target_fill": [args.target_min, args.target_max],
        "pages": report.pages,
        "passed": report.passed,
        "checks": [{"name": c.name, "passed": c.passed, "detail": c.detail} for c in report.checks],
        "output_docx": str(out_docx),
        "output_pdf": str(out_pdf),
    }
    out_report = output_dir / "resume-validation.json"
    out_report.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    print(f"PASS: {payload['fill_ratio'] * 100:.1f}% fill at {payload['body_pt']:.2f} pt")
    print(out_docx)
    print(out_pdf)
    print(out_report)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--data", type=Path, default=default_data_dir(), help="data dir (default ./data or $RESUME_DATA)")
    sub = parser.add_subparsers(dest="command", required=True)

    p_init = sub.add_parser("init", help="copy the example data dir to start from")
    p_init.add_argument("--force", action="store_true")
    p_init.set_defaults(func=cmd_init)

    p_val = sub.add_parser("validate", help="check a spec's evidence without rendering")
    p_val.add_argument("spec")
    p_val.add_argument("--facts", type=Path)
    p_val.set_defaults(func=cmd_validate)

    p_build = sub.add_parser("build", help="fit, render, and validate a one-page resume")
    p_build.add_argument("spec")
    p_build.add_argument("--facts", type=Path)
    p_build.add_argument("--output-dir", type=Path)
    p_build.add_argument("--reference", type=Path, help="optional .docx whose section order/format must be matched")
    p_build.add_argument("--target-min", type=float, default=0.95)
    p_build.add_argument("--target-max", type=float, default=0.97)
    p_build.add_argument("--ideal", type=float, default=0.96)
    p_build.add_argument("--min-body-pt", type=float, default=10.0)
    p_build.add_argument("--hide-phone", action="store_true", help="omit the phone number (public copies)")
    p_build.set_defaults(func=cmd_build)

    args = parser.parse_args()
    args.data = args.data.resolve()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
