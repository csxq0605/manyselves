"""Check writing lessons before adopting them: content rules and variant triggers.

Every active lesson in the file must name no object or value of the run it
came from, use no mandatory word, name known roles, and have a trigger that
fires on at least one fixed-structure variant (``scripts/benchmarks/
fixed_structure_variants.py``).  No Provider is called.

    uv run python scripts/maintenance/check_writing_lessons.py --synthetic
    uv run python scripts/maintenance/check_writing_lessons.py \\
        --lessons /path/to/Work/runs/<triage-run>/lessons/candidates.yaml \\
        --s4-4 /path/to/Inputs/S4-4.xlsx --project /path/to/project --source-run <report-run>
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from manyselves.capabilities.distribution_reporting.runtime.lesson_triage import (  # noqa: E402
    lesson_admission,
    reporting_run_id,
    run_terms,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skills import (  # noqa: E402
    LESSONS_FILE,
)
from manyselves.runtime.exemplar.lessons import load_lessons  # noqa: E402
from scripts.benchmarks.fixed_structure_variants import (  # noqa: E402
    mapped_variants,
    synthetic_workbook,
)


def variant_data(source: Path, tmp: Path) -> dict:
    return {
        name: (items, [gap.model_dump(mode="json") for gap in gaps])
        for name, _info, items, gaps, *_rest in mapped_variants(source, tmp)
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--lessons", type=Path, default=LESSONS_FILE)
    parser.add_argument("--s4-4", dest="s4_4", type=Path)
    parser.add_argument("--synthetic", action="store_true")
    parser.add_argument("--project", type=Path, help="project of the run the lessons came from")
    parser.add_argument("--source-run", help="report run the lessons came from")
    args = parser.parse_args(argv)
    if not args.synthetic and not args.s4_4:
        parser.error("give --s4-4 PATH or --synthetic")
    book = load_lessons(args.lessons)
    terms: list[str] = []
    if args.project and args.source_run:
        workspace = args.project.resolve()
        terms = run_terms(workspace, reporting_run_id(workspace, args.source_run))
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        source = args.s4_4 or synthetic_workbook(tmp_path / "S4-4.xlsx")
        variants_dir = tmp_path / "variants"
        variants_dir.mkdir()
        report = lesson_admission(book, variant_data(source, variants_dir), run_terms=terms)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(entry["admissible"] for entry in report.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
