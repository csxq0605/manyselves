#!/usr/bin/env python3
"""Print a read-only Manyselves reporting storage preview.

The command intentionally calls ``ReportingRetentionPlanner.preview`` rather
than ``generate``: no summary file, lifecycle manifest, CAS inode, or output
view is changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Running a source checkout script does not necessarily put the repository
# root on ``sys.path`` (Python starts with ``scripts/``).  Keep this tiny
# bootstrap local and deterministic; no package/provider initialization occurs.
try:
    from manyselves.core.reporting.retention import ReportingRetentionPlanner
except ModuleNotFoundError:  # pragma: no cover - depends on invocation cwd
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from manyselves.core.reporting.retention import ReportingRetentionPlanner


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Preview reporting CAS retention without mutating storage"
    )
    parser.add_argument(
        "workspace",
        nargs="?",
        type=Path,
        default=Path.cwd(),
        help="Manyselves workspace (default: current directory)",
    )
    parser.add_argument(
        "--grace-days",
        type=int,
        default=7,
        help="minimum canonical-blob age for a reclaim preview (default: 7)",
    )
    args = parser.parse_args()
    result = ReportingRetentionPlanner(args.workspace).preview(
        grace_days=args.grace_days
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":  # pragma: no cover - exercised by CLI smoke tests
    raise SystemExit(main())
