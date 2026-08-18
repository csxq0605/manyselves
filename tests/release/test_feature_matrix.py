import csv
from pathlib import Path

from scripts.verify_phase1_matrix import validate


MATRIX = Path("docs/phase1/feature-parity.csv")
RULES = Path("docs/phase1/parity-rules.yaml")


def test_every_matrix_row_has_unique_id_and_legacy_evidence() -> None:
    rows = list(csv.DictReader(MATRIX.open(encoding="utf-8")))
    assert len({row["id"] for row in rows}) == len(rows)
    assert all(row["legacy_evidence"].strip() for row in rows)


def test_tested_matrix_has_complete_existing_evidence() -> None:
    result = validate(MATRIX, RULES, allow_status="tested")
    assert result.errors == (), "\n".join(result.errors)
    assert result.rows >= 100


def test_release_mode_rejects_rows_without_reviewer_acceptance() -> None:
    result = validate(MATRIX, RULES)
    assert any("below required release state" in error for error in result.errors)
