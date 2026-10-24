"""An undetermined row's gap is read from the records it cites; equal gaps are one entry."""

from __future__ import annotations

from typing import Any

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
from manyselves.capabilities.distribution_reporting.runtime.context_packs import Sites
from manyselves.capabilities.distribution_reporting.runtime.gap_entries import (
    BLANK,
    LISTED,
    NONE,
    OPINION,
    READING,
    STATE,
    gap_entries,
    gap_kind,
    gap_sections,
    section_gap_entries,
    settle_gap_kinds,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem


def _item(eid: str, sheet: str, fact: str, *, subject: str = "A房/1#柜", place: str | None = "A房",
          summary: bool = False, reading: float | None = None) -> EvidenceItem:
    measurements = (
        [{"quantity": "current", "value": reading, "unit": "A", "label": "运行电流"}]
        if reading is not None else []
    )
    return EvidenceItem(
        id=eid, subject=subject, place=place, fact=fact, summary_entry=summary,
        measurements=measurements,
        source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": sheet, "row": 3},
    )


def _row(row_id: str, evidence: list[str], missing: str, *, section: str = "2.4.4",
         obj: str = "A房/1#柜", status: str = "资料未提供", **gap: Any) -> JudgmentRow:
    return JudgmentRow.model_validate({
        "id": row_id, "submodule_id": section, "object": obj, "checkpoint": "检查",
        "disposition": "undetermined", "evidence_status": status, "evidence_ids": evidence,
        "basis": "b", "gap": {"missing": missing, "impact": "不能判定", "requested_input": "补上", **gap},
    })


ITEMS = [
    _item("E-1", "收资表", "具备情况=NG；有效性=NG", subject="运行记录/应急预案", place=None),
    _item("E-2", "红外", "检测部位=未填写；测点温度=31℃", subject="A房/1#柜", reading=31.0),
    _item("E-3", "红外", "检测部位=未填写；测点温度=29℃", subject="B房/2#柜", place="B房"),
    _item("E-4", "低配", "运行电流=123A", reading=123.0),
    _item("E-5", "汇总", "评估结论=多数电容柜未投入", place=None, summary=True),
    _item("E-6", "合规", "防鼠板=OK"),
]
SITES = Sites(ITEMS)


def test_the_kind_of_gap_is_read_from_the_cited_records() -> None:
    assert gap_kind(_row("J-2.5-1", ["E-1"], "应急预案"), SITES) == LISTED
    assert gap_kind(_row("J-2.4-1", ["E-2"], "检测部位"), SITES) == BLANK
    # A blank column the row does not ask for is not its gap.
    assert gap_kind(_row("J-2.4-2", ["E-2"], "红外历史记录"), SITES) == READING
    assert gap_kind(_row("J-2.1-1", ["E-4"], "额定容量"), SITES) == READING
    assert gap_kind(_row("J-2.1-2", ["E-5"], "电容柜台账"), SITES) == OPINION
    assert gap_kind(_row("J-2.4-3", ["E-6"], "防鼠板尺寸"), SITES) == STATE
    assert gap_kind(_row("J-2.4-4", [], "图纸"), SITES) == NONE


def test_the_program_sets_an_undetermined_rows_evidence_status() -> None:
    rows = [
        _row("J-2.4-1", ["E-2"], "检测部位", status="资料未提供"),
        _row("J-2.1-1", ["E-4"], "额定容量", status="资料未提供"),
        _row("J-2.5-1", ["E-1"], "应急预案", status="现场确认"),
        _row("J-2.1-2", ["E-5"], "电容柜台账", status="资料未提供"),
    ]
    statuses = [row.evidence_status for row in settle_gap_kinds(rows, SITES)]
    assert statuses == ["现场确认", "现场确认", "资料未提供", "来源作者意见"]


def test_rows_lacking_the_same_thing_are_one_entry_naming_every_object() -> None:
    ledger = {
        "2.2": [_row("J-2.2-1", ["E-2"], "该测点的检测部位", section="2.2.2.1")],
        "2.4": [
            _row("J-2.4-1", ["E-2"], "检测部位与负荷率"),
            _row("J-2.4-2", ["E-3"], "检测部位", obj="B房/2#柜"),
            _row("J-2.4-3", ["E-1"], "应急预案文本", fill=["问委托方"]),
        ],
    }
    entries = gap_entries(ledger, SITES)
    blank, listed = entries
    assert blank["kind"] == BLANK and blank["row_ids"] == ["J-2.2-1", "J-2.4-1", "J-2.4-2"]
    assert blank["objects"] == ["A房/1#柜", "B房/2#柜"]
    assert blank["wording"] == "原表红外的检测部位未填写"
    # Written where most of its rows are; the other section points there.
    assert blank["section"] == "2.4.4" and blank["fill"] == ["问委托方"]
    assert listed["wording"] == "应急预案未提供（收资表记为不具备）" and listed["fill"] == ["问委托方"]
    (pointer,) = section_gap_entries(entries, ledger["2.2"], "2.2.2.1")
    assert pointer["write_here"] is False and pointer["see_section"] == "2.4.4"
    here = section_gap_entries(entries, ledger["2.4"], "2.4.4")
    assert [e["write_here"] for e in here] == [True, True] and here[0]["row_ids"] == ["J-2.4-1", "J-2.4-2"]
    assert gap_sections(entries)["J-2.2-1"] == "2.4.4"


def test_a_different_way_to_close_the_gap_is_a_different_entry() -> None:
    ledger = {"2.5": [
        _row("J-2.5-1", ["E-1"], "应急预案", section="2.5.1"),
        _row("J-2.5-2", ["E-1"], "应急预案", section="2.5.1", fill=["问委托方"]),
    ]}
    assert len(gap_entries(ledger, SITES)) == 2


def test_chapters_one_and_three_read_each_gap_once_as_its_entry() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.context_packs import brief_row, project_map
    from manyselves.capabilities.distribution_reporting.runtime.gap_entries import (
        chief_gap_entries,
        gap_ids,
        gap_labels,
    )

    ledger = {"2.4": [
        _row("J-2.4-1", ["E-2"], "该测点的检测部位"),
        _row("J-2.4-2", ["E-3"], "检测部位", obj="B房/2#柜"),
    ]}
    entries = gap_entries(ledger, SITES)
    (entry,) = chief_gap_entries(entries)
    assert entry["row_ids"] == ["J-2.4-1", "J-2.4-2"] and entry["objects"] == "A房/1#柜、B房/2#柜"
    row = ledger["2.4"][1]
    # The row names its entry instead of repeating the gap in its own words.
    assert brief_row(row, gap_id=gap_ids(entries)[row.id])["gap"] == {"gap_id": "G01"}
    assert brief_row(row)["gap"]["missing"] == "检测部位"
    page = project_map(SITES, ledger, gaps=gap_labels(entries))
    assert "〔G01〕原表红外的检测部位未填写" in page and "：检测部位" not in page
