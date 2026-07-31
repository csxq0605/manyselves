"""Prevent Phase 1 changes to the frozen core and template paths."""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

PROTECTED_PREFIXES = ("manyselves/core/", "manyselves/templates/")


def _git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        text=True,
        capture_output=True,
        check=False,
    )


def _current_commit() -> str | None:
    result = _git("rev-parse", "--verify", "HEAD^{commit}")
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _read_baseline(path: Path) -> str | None:
    try:
        baseline = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not baseline or "\n" in baseline:
        return None
    result = _git("rev-parse", "--verify", f"{baseline}^{{commit}}")
    return baseline if result.returncode == 0 else None


def initialize(path: Path) -> int:
    if path.exists():
        print(f"Baseline already exists: {path}", file=sys.stderr)
        return 2
    commit = _current_commit()
    if commit is None:
        print("Could not determine the current Git commit", file=sys.stderr)
        return 2
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x", encoding="utf-8") as baseline_file:
            baseline_file.write(f"{commit}\n")
    except OSError as error:
        print(f"Could not initialize baseline {path}: {error}", file=sys.stderr)
        return 2
    return 0


def check(base_file: Path) -> int:
    baseline = _read_baseline(base_file)
    if baseline is None:
        print(f"Missing or invalid baseline: {base_file}", file=sys.stderr)
        return 2
    result = _git("diff", "--name-only", baseline, "--")
    if result.returncode != 0:
        print(result.stderr.strip() or "Could not read Git changes", file=sys.stderr)
        return 2
    protected_changes = [
        path.replace("\\", "/")
        for path in result.stdout.splitlines()
        if path.replace("\\", "/").startswith(PROTECTED_PREFIXES)
    ]
    if protected_changes:
        print("\n".join(protected_changes))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    command = parser.add_mutually_exclusive_group(required=True)
    command.add_argument("--initialize", type=Path, metavar="FILE")
    command.add_argument("--base-file", type=Path, metavar="FILE")
    args = parser.parse_args()
    if args.initialize is not None:
        return initialize(args.initialize)
    return check(args.base_file)


if __name__ == "__main__":
    raise SystemExit(main())
