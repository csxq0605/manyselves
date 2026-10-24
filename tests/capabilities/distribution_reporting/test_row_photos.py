"""A concern row that names no photo shows its own records' photos."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.context_packs import Sites
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem
from manyselves.capabilities.distribution_reporting.runtime.row_photos import filled_photos, photos_not_shown


def _item(eid: str, subject: str, place: str | None, photos: list[str], section: str | None) -> EvidenceItem:
    return EvidenceItem(
        id=eid, subject=subject, place=place, fact="状态=NG", photo_refs=photos, submodule_id=section,
        module_id=section[:3] if section else None,
        source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": "表", "row": 3},
    )


def _row(row_id: str, obj: str, evidence: list[str], photos: list[str] = (), section: str = "2.2.2.3") -> JudgmentRow:
    return JudgmentRow.model_validate({
        "id": row_id, "submodule_id": section, "object": obj, "checkpoint": "胶垫",
        "disposition": "concern", "evidence_status": "现场确认", "evidence_ids": evidence,
        "basis": "b", "risk_level": "中", "hazard": "h", "photo_ids": list(photos),
        "action": {"measure": "补", "priority": "一般", "timeframe": "一月"},
    })


def _sites() -> Sites:
    return Sites([
        _item("E-1", "A房", "A房", ["P-1"], "2.2.2.3"),
        _item("E-2", "评估总表 2.2.2.3", None, ["P-2"], "2.2.2.3"),  # cited by two rows: whose?
        _item("E-3", "评估总表 2.2.2.3", None, ["P-3"], "2.2.2.3"),  # cited by one row only
        _item("E-4", "B房", "B房", ["P-4"], "2.2.2.3"),  # another object
        _item("E-5", "A房", "A房", ["P-5"], "2.4.2.2"),  # another section
    ])


def test_only_the_rows_own_records_give_it_photos() -> None:
    ledger = {"2.2": [
        _row("J-2.2-1", "A房", ["E-1", "E-2", "E-3", "E-4", "E-5"]),
        _row("J-2.2-2", "C房", ["E-2"]),
        _row("J-2.2-3", "A房", ["E-1"], photos=["P-9"]),
    ]}
    sites = _sites()
    filled = filled_photos(ledger, sites)["2.2"]
    assert [row.photo_ids for row in filled] == [["P-1", "P-3"], [], ["P-9"]]
    # The shared summary photo reaches no row and is listed for a person to place.
    assert [p["photo_id"] for p in photos_not_shown({"2.2": filled}, sites)] == ["P-2", "P-4"]
