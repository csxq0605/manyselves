"""Publish candidate role Skills from an extract-template-skills run into Inputs.

Read ``Work/runs/<run>/template-skills/diff.md`` first.  The replaced files are
kept under ``Work/runs/<run>/template-skills/previous/``; nothing is deleted.

    uv run python scripts/maintenance/publish_template_skills.py \\
        --project /path/to/project --run-id extract-template-skills-<id> [--role author-2.1 ...]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from manyselves.capabilities.distribution_reporting.runtime.template_skills import (  # noqa: E402
    publish_template_skills,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--role", action="append", help="publish only these roles")
    args = parser.parse_args(argv)
    result = publish_template_skills(args.project, args.run_id, roles=args.role)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
