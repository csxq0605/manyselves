"""Validate Phase 1 parity schema, evidence paths, and release statuses."""

from __future__ import annotations

import argparse
import csv
from dataclasses import dataclass
from pathlib import Path

import yaml

FIELDS = {
    "id", "module", "feature", "legacy_evidence", "api_or_bridge", "react_evidence",
    "browser_test", "electron_test", "status", "reviewer", "rationale",
}


@dataclass(frozen=True)
class ValidationResult:
    errors: tuple[str, ...]
    rows: int


def _evidence_paths(value: str) -> list[str]:
    paths = []
    roots = ("manyselves/", "tests/", "scripts/", "docs/", "frontend/", "desktop/", "deploy/", "frontend-contract/")
    for raw in value.split(";"):
        item = raw.strip()
        if not item:
            continue
        item = item.split("::", 1)[0]
        for suffix in (".py:", ".ts:", ".tsx:", ".md:"):
            if suffix in item:
                item = item.split(suffix, 1)[0] + suffix[:-1]
                break
        normalized = item.replace("\\", "/")
        if normalized.startswith(roots):
            paths.append(normalized)
    return paths


def validate(matrix: Path, rules_path: Path, *, allow_status: str | None = None) -> ValidationResult:
    repo = matrix.resolve().parents[2]
    with matrix.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        columns = set(reader.fieldnames or [])
    rules = yaml.safe_load(rules_path.read_text("utf-8"))
    statuses = list(rules["statuses"])
    allowed_release = {"accepted", "waived"}
    if allow_status:
        minimum = statuses.index(allow_status)
        allowed_release |= set(statuses[minimum:])
    errors: list[str] = []
    if columns != FIELDS:
        errors.append(f"matrix columns differ: {sorted(columns ^ FIELDS)}")
    ids = [row.get("id", "") for row in rows]
    if len(ids) != len(set(ids)):
        errors.append("matrix IDs must be unique")
    family_rules = {}
    for rule in rules["requirements"].values():
        for family in rule["families"]:
            family_rules[family] = rule
    for index, row in enumerate(rows, start=2):
        identifier = row.get("id") or f"line-{index}"
        status = row.get("status", "")
        if status not in statuses:
            errors.append(f"{identifier}: invalid status {status!r}")
        elif status not in allowed_release:
            errors.append(f"{identifier}: status {status!r} is below required release state")
        if status == "waived" and (not row.get("reviewer", "").strip() or not row.get("rationale", "").strip()):
            errors.append(f"{identifier}: waived rows require reviewer and rationale")
        rule = family_rules.get(row.get("module", ""))
        if rule is None:
            errors.append(f"{identifier}: no parity rule for family {row.get('module')!r}")
            continue
        for field in rule.get("all", []):
            if not row.get(field, "").strip():
                errors.append(f"{identifier}: missing {field}")
        any_fields = rule.get("any", [])
        if any_fields and not any(row.get(field, "").strip() for field in any_fields):
            errors.append(f"{identifier}: requires one of {any_fields}")
        if status in {"tested", "accepted", "waived"}:
            evidence_fields = set(rule.get("all", [])) | set(any_fields)
            for field in evidence_fields:
                for evidence in _evidence_paths(row.get(field, "")):
                    if not (repo / evidence).exists():
                        errors.append(f"{identifier}: missing evidence path {evidence}")
    return ValidationResult(tuple(errors), len(rows))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("matrix", type=Path)
    parser.add_argument("--rules", type=Path, default=Path("docs/phase1/parity-rules.yaml"))
    parser.add_argument("--allow-status", choices=["planned", "implemented", "tested", "accepted", "waived"])
    args = parser.parse_args()
    result = validate(args.matrix, args.rules, allow_status=args.allow_status)
    if result.errors:
        print("\n".join(result.errors))
        return 1
    print(f"Phase 1 matrix valid: {result.rows} rows")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
