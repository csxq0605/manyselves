"""Pairs that are gaps or findings side by side in one room are not relations."""

from __future__ import annotations

from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.context_packs import Sites
from manyselves.capabilities.distribution_reporting.runtime.gap_entries import gap_entries
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import _adopted_by_both
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem
from manyselves.capabilities.distribution_reporting.runtime.relation_routes import (
    GAP_AFFECTS,
    GAP_BOTH,
    SAME_ROOM,
    gap_affects,
    routed_relations,
    same_room_issues,
)


def _item(eid: str, subject: str, place: str | None, *, reading: float | None = None) -> EvidenceItem:
    return EvidenceItem(
        id=eid, subject=subject, place=place, fact="状态=NG",
        measurements=[{"quantity": "current", "value": reading, "unit": "A", "label": "运行电流"}]
        if reading is not None else [],
        source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": "表", "row": 3},
    )


ITEMS = [
    _item("E-1", "A房/1#柜", "A房", reading=26.0),
    _item("E-2", "A房/1#柜", "A房"),
    _item("E-3", "A房", "A房"),
    _item("E-4", "收资表/操作程序", None),
    _item("E-5", "合规表/用具", None),
    _item("E-6", "B房/2#柜", "B房", reading=15.0),
]
SITES = Sites(ITEMS)


def _row(row_id: str, section: str, obj: str, disposition: str, evidence: list[str],
         relations: list[str] = ()) -> JudgmentRow:
    gap: dict[str, Any] = (
        {"gap": {"missing": "资料", "impact": "不能判定", "requested_input": "补"}}
        if disposition == "undetermined" else {}
    )
    if disposition == "concern":
        gap = {"risk_level": "中", "hazard": "h",
               "action": {"measure": "核查", "priority": "一般", "timeframe": "一月"}}
    return JudgmentRow.model_validate({
        "id": row_id, "submodule_id": section, "object": obj, "checkpoint": "检查",
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": evidence,
        "basis": "b", **gap,
        "relations": [
            {"row_id": other, "relation": "关系", "reason": "r", "mechanism": "m", "condition": "c"}
            for other in relations
        ],
    })


def test_gaps_and_same_room_findings_leave_the_relations_and_the_rest_stay() -> None:
    ledger = {
        "2.1": [
            # A reading on the same cabinet bears on the other module's finding: a relation.
            _row("J-2.1-1", "2.1.1", "A房/1#柜", "undetermined", ["E-1"], ["J-2.4-1"]),
            _row("J-2.1-2", "2.1.1", "B房/2#柜", "undetermined", ["E-6"], ["J-2.2-2"]),
        ],
        "2.2": [
            _row("J-2.2-1", "2.2.2.3", "A房", "concern", ["E-3"], ["J-2.5-2"]),
            _row("J-2.2-2", "2.2.2.1", "B房/2#柜", "undetermined", ["E-6"]),
        ],
        "2.4": [_row("J-2.4-1", "2.4.4", "A房/1#柜", "concern", ["E-2"], ["J-2.5-1"])],
        "2.5": [
            _row("J-2.5-1", "2.5.1", "操作程序", "undetermined", ["E-4"]),
            # Named under its room, though its record has no place.
            _row("J-2.5-2", "2.5.5", "A房（安全用具）", "concern", ["E-5"]),
        ],
    }
    kept, routes = routed_relations(ledger, SITES)
    assert [(r["rows"], r["route"]) for r in routes] == [
        (["J-2.1-2", "J-2.2-2"], GAP_BOTH),
        (["J-2.2-1", "J-2.5-2"], SAME_ROOM),
        (["J-2.4-1", "J-2.5-1"], GAP_AFFECTS),
    ]
    assert [r.row_id for r in kept["2.1"][0].relations] == ["J-2.4-1"]
    assert all(not row.relations for rows in kept.values() for row in rows if row.id != "J-2.1-1")
    # The missing document names the row it bears on; each room is listed once.
    entries = gap_entries(kept, SITES, affects=gap_affects(routes, kept))
    procedure = next(e for e in entries if e["row_ids"] == ["J-2.5-1"])
    assert [a["row_id"] for a in procedure["affects"]] == ["J-2.4-1"]
    (room,) = same_room_issues(routes, kept)
    assert room["place"] == "A房" and [r["row_id"] for r in room["rows"]] == ["J-2.2-1", "J-2.5-2"]


def test_a_proposed_relation_stands_only_when_both_ends_took_it() -> None:
    adopted = {
        "2.4": {"J-2.4-1": [{"row_id": "J-2.5-1", "relation": "甲"}, {"row_id": "J-2.5-2", "relation": "乙"}]},
        "2.5": {"J-2.5-1": [{"row_id": "J-2.4-1", "relation": "甲"}]},
    }
    kept, one_side = _adopted_by_both(adopted)
    assert kept == {
        "2.4": {"J-2.4-1": [{"row_id": "J-2.5-1", "relation": "甲"}]},
        "2.5": {"J-2.5-1": [{"row_id": "J-2.4-1", "relation": "甲"}]},
    }
    assert one_side == [{"row_id": "J-2.4-1", "other_row_id": "J-2.5-2", "relation": "乙"}]
