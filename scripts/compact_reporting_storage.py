#!/usr/bin/env python3
"""Dry-run, apply, or roll back completed-report DOCX CAS compaction."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from manyselves.core.reporting.storage_compaction import (  # noqa: E402
    CompletedRunOutputCompactor,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Validate a completed report and optionally replace its visible "
            "final DOCX with a verified project-local CAS view."
        )
    )
    parser.add_argument(
        "--workspace",
        type=Path,
        required=True,
        help="Manyselves project workspace containing Work and Outputs.",
    )
    parser.add_argument("--run-id", required=True)
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument(
        "--dry-run",
        dest="action",
        action="store_const",
        const="dry_run",
        help="Validate and report estimated savings without writing (default).",
    )
    actions.add_argument(
        "--apply",
        dest="action",
        action="store_const",
        const="apply",
        help="Atomically replace the matching final DOCX with a CAS symlink.",
    )
    actions.add_argument(
        "--rollback",
        dest="action",
        action="store_const",
        const="rollback",
        help="Atomically materialize the final DOCX as a regular file again.",
    )
    parser.set_defaults(action="dry_run")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        record = CompletedRunOutputCompactor(args.workspace).execute(
            args.run_id,
            action=args.action,
        )
    except (OSError, ValueError) as exc:
        print(
            json.dumps(
                {"status": "refused", "error": str(exc)},
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 2
    print(record.model_dump_json(indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
