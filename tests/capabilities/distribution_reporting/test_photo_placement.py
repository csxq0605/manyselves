"""Photos of records the knowledge base left unassigned go where the authors placed them."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    SourceLocation,
)


def _item(number: int, submodule: str | None, photo: str) -> EvidenceItem:
    return EvidenceItem(
        id=f"E-{number:04d}",
        subject=f"设备{number}",
        fact="其他检查发现：柜内有积尘。",
        source=SourceLocation(file_id="table", path=Path("Inputs/table.xlsx")),
        module_id=submodule[:3] if submodule else None,
        submodule_id=submodule,
        photo_refs=[photo],
    )


def _photo(photo_id: str) -> PhotoAsset:
    return PhotoAsset(
        id=photo_id,
        path=Path(f"Work/photos/{photo_id}.jpg"),
        sha256="0" * 64,
        media_type="image/jpeg",
        source_member=f"xl/media/{photo_id}.jpg",
    )


def test_unassigned_records_do_not_reject_their_photos() -> None:
    evidence = [_item(1, None, "P-0001"), _item(2, "2.1.1", "P-0002")]
    photos = [_photo("P-0001"), _photo("P-0002")]

    # Not placed yet: left out, not an error.
    assert runtime_photo_ids(evidence, photos) == ["P-0002"]
    # A photo with no source-table record at all is still an error.
    with pytest.raises(ValueError, match="source-table evidence"):
        runtime_photo_ids(evidence, [*photos, _photo("P-0009")])


def test_authors_placement_gives_the_photo_its_section(tmp_path: Path) -> None:
    evidence = [
        _item(1, None, "P-0001"),
        _item(2, None, "P-0002"),
        _item(3, "2.1.1", "P-0003"),
    ]
    photos = [_photo(p) for p in ("P-0001", "P-0002", "P-0003")]
    row = JudgmentRow.model_validate(
        {
            "id": "J-1",
            "submodule_id": "2.3.1",
            "object": "设备1",
            "checkpoint": "清洁",
            "disposition": "no_concern",
            "evidence_status": "现场确认",
            "evidence_ids": ["E-0001"],
            "basis": "柜内有积尘，不影响运行。",
            "rule_refs": ["KU-1"],
        }
    )

    placed = LedgerTools(tmp_path)._placed_evidence(
        "run-1",
        [item.model_dump(mode="json") for item in evidence],
        [photo.model_dump(mode="json") for photo in photos],
        {"2.3": [row]},
    )

    by_id = {item["id"]: item for item in placed}
    assert (by_id["E-0001"]["module_id"], by_id["E-0001"]["submodule_id"]) == ("2.3", "2.3.1")
    assert by_id["E-0002"]["submodule_id"] is None
    assert by_id["E-0003"]["submodule_id"] == "2.1.1"
    assert runtime_photo_ids(
        [EvidenceItem.model_validate(item) for item in placed], photos
    ) == ["P-0001", "P-0003"]
    unplaced = json.loads(
        (tmp_path / "Work/runs/run-1/ledger/unplaced-photos.json").read_text(encoding="utf-8")
    )
    assert unplaced["photos"] == [{"photo_id": "P-0002", "evidence_ids": ["E-0002"]}]
