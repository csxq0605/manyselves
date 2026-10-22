"""A checker's or editor's blocking finding stands only on material it was given."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.runtime.chief_fact_packets import (
    apply_fact_findings,
    chief_units,
)
from manyselves.capabilities.distribution_reporting.runtime.grounds import (
    UNCHECKABLE,
    ground_ids,
    grounded,
)


def _finding(severity: str = "blocking", grounds=()):
    return {"problem": "数值配错。", "severity": severity, "suggestion": "改正。", "grounds": list(grounds)}


def test_ground_ids_read_ids_out_of_free_text() -> None:
    assert ground_ids(["J-2.2-45（同一房间）", "〔K-0013〕", "E-0075、E-0075", "KU-2.4-10"]) == [
        "J-2.2-45", "K-0013", "E-0075", "KU-2.4-10",
    ]


def test_a_blocking_finding_without_known_grounds_becomes_minor() -> None:
    material = {"J-2.2-45": "老配电房（低压）｜同一房间", "E-0075": "IP等级 OK"}
    checked = grounded(
        {
            "a": _finding(grounds=["J-2.2-45"]),
            "b": _finding(),
            "c": _finding(grounds=["J-9.9-1"]),
            "d": _finding("minor"),
        },
        material,
    )
    assert checked["a"]["severity"] == "blocking"
    assert checked["a"]["grounds"] == {"J-2.2-45": "老配电房（低压）｜同一房间"}
    for key in ("b", "c"):
        assert checked[key]["severity"] == "minor"
        assert checked[key]["problem"].startswith(UNCHECKABLE)
    assert checked["d"] == {"problem": "数值配错。", "severity": "minor", "suggestion": "改正。"}


def test_fact_findings_are_held_to_their_own_packet() -> None:
    packets = [
        {
            "packet_id": "F-01",
            "units": [{"unit_id": "1.2-段1", "where": "1.2 第1段"}],
            "rows": [{"id": "J-2.4-1", "object": "14#柜-3", "risk_level": "中", "basis": "温升高"}],
            "facts": {"E-0001": "14#柜-3 电流 11.3A"},
            "values": {"〔V-0001〕": "14#柜-3｜电流=11.3A"},
            "counts": {},
        },
        {
            "packet_id": "F-02",
            "units": [{"unit_id": "3.1-汇总-1", "where": "3.1 风险汇总"}],
            "rows": [], "facts": {}, "values": {}, "counts": {},
        },
    ]
    results = {
        "F-01": {"findings": {"1.2-段1": _finding(grounds=["〔V-0001〕"])}},
        # J-2.4-1 is in another packet: this checker never saw it.
        "F-02": {"findings": {"3.1-汇总-1": _finding(grounds=["J-2.4-1"])}},
    }
    checked = apply_fact_findings(packets, results)
    findings = checked["check"]["findings"]
    assert checked["blocking"] is True
    assert findings["1.2-段1"]["severity"] == "blocking"
    assert findings["1.2-段1"]["problem"].startswith("1.2 第1段：")
    assert findings["1.2-段1"]["grounds"] == {"V-0001": "14#柜-3｜电流=11.3A"}
    assert findings["3.1-汇总-1"]["severity"] == "minor"


def test_an_edited_overview_is_checked_as_written() -> None:
    chapter3 = {
        "risk_summary": ["总体风险可控。"],
        "risk_details": [{"module_id": "2.4", "points": ["温度偏高。"]}],
        "cross_findings": [],
        "action_lead": "如下。",
        "actions": [],
    }
    drawn = [u["unit_id"] for u in chief_units({}, chapter3, [])]
    assert drawn == ["3.1-汇总-1", "3.1-详述-2.4-1"]
    edited = chief_units({}, chapter3, [], {"3.1": "风险汇总如下。\n\n温度偏高需复测。"})
    assert [u["unit_id"] for u in edited] == ["3.1-段1", "3.1-段2"]
    assert edited[1]["text"] == "温度偏高需复测。"
