"""Program checks that let writer, editor and checker read the same material."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.calculations import Calculation
from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    ActionText,
    JudgmentRow,
    Phase,
    deadline_findings,
    phase_findings,
    time_terms,
)
from manyselves.capabilities.distribution_reporting.domain.value_slots import slot_lines, slot_table
from manyselves.capabilities.distribution_reporting.runtime.context_packs import (
    Sites,
    project_map,
    recorded_field_findings,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_module import related_rows
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import checkpoints_of
from manyselves.capabilities.distribution_reporting.runtime.mappers.common import _first_record_row
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import EvidenceItem


def _row(row_id: str, evidence: list[str], *, disposition: str = "concern", **extra: Any) -> JudgmentRow:
    module_id = row_id.split("-")[1]
    value: dict[str, Any] = {
        "id": row_id, "submodule_id": f"{module_id}.1", "object": "A房/1#柜", "checkpoint": "检查",
        "disposition": disposition, "evidence_status": "现场确认", "evidence_ids": evidence, "basis": "b",
    }
    if disposition == "concern":
        value |= {"risk_level": "中", "hazard": "h",
                  "action": {"measure": "m", "priority": "一般", "timeframe": "1个月内"}}
    if disposition == "undetermined":
        value |= {"evidence_status": "资料未提供",
                  "gap": {"missing": "未提供接地电阻", "impact": "i", "requested_input": "补测"}}
    value |= extra
    return JudgmentRow.model_validate(value)


def _item(eid: str, sheet: str, fact: str, *, field: str | None = None, value: Any = None,
          summary: bool = False, subject: str = "A房/1#柜", place: str | None = "A房") -> EvidenceItem:
    return EvidenceItem(
        id=eid, subject=subject, place=place, fact=fact, value=value, checkpoint_field=field,
        summary_entry=summary, source={"file_id": "t", "path": "Inputs/t.xlsx", "sheet": sheet, "row": 3},
    )


def test_a_relation_is_seen_from_both_of_its_ends() -> None:
    source = _row("J-2.2-1", ["E-1"], relations=[
        {"row_id": "J-2.4-1", "relation": "印证", "reason": "同柜发热与积灰"}])
    target = _row("J-2.4-1", ["E-2"])
    ledger = {"2.2": [source], "2.4": [target]}
    (entry,) = related_rows([target], ledger)
    assert entry["row_id"] == "J-2.2-1"
    assert entry["for_rows"][0]["row_id"] == "J-2.4-1" and entry["for_rows"][0]["inbound"] is True
    # The recording side keeps its outbound view, not doubled.
    (out,) = related_rows([source], ledger)
    assert out["row_id"] == "J-2.4-1" and len(out["for_rows"]) == 1


def test_a_calculated_slot_shows_its_operands() -> None:
    calc = Calculation(id="K-1", evidence_ids=["E-1"], subject="A房", quantity="ratio",
                       value=0.5, unit="", formula="最低电流 5A ÷ 最高电流 10A")
    slots = slot_table([], [calc], {})
    (slot_id,) = [k for k, v in slots.items() if v.get("formula")]
    assert slot_lines([slot_id], slots)[f"〔{slot_id}〕"].endswith("（最低电流 5A ÷ 最高电流 10A）")
    symbolic = Calculation(id="K-2", evidence_ids=["E-1"], subject="A房", quantity="q",
                           value=1, unit="", formula="a × b")
    assert all("formula" not in v for v in slot_table([], [symbolic], {}).values())


def test_a_header_cell_record_does_not_move_the_header_rows() -> None:
    # One record read from a header cell on row 1; the table's records start on row 4.
    assert _first_record_row({1: 1, 4: 6, 5: 6}) == 4
    assert _first_record_row({3: 2, 4: 2}) == 3
    assert _first_record_row({2: 1}) == 2


def test_only_structured_columns_group_actions() -> None:
    items = [
        _item("E-1", "低配", "SPD=NG", field="SPD"),
        _item("E-2", "低配", "SPD=OK", field="SPD"),
        _item("E-3", "低配", "其他=柜门变形", field="其他"),
        _item("E-4", "低配", "其他=标识缺失", field="其他"),
        _item("E-5", "低配", "温升=12", field="温升", value=12.0),
        _item("E-6", "总表", "x", summary=True, subject="2.4 评估"),
    ]
    assert checkpoints_of(items) == {"E-1": "SPD", "E-2": "SPD", "E-5": "温升", "E-6": "2.4 评估"}


def test_time_terms_read_numbers_ranges_and_units() -> None:
    assert time_terms("1至3个月内完成") == [(1, "个月"), (3, "个月")]
    assert time_terms("建议一个月内完成，两周内复测") == [(1, "个月"), (2, "周")]
    assert time_terms("十五日内") == [(15, "天")]


def test_chief_deadlines_and_phases_follow_the_rows() -> None:
    groups = [{"group_id": "G-01", "timeframes": ["1个月内"], "row_ids": ["J-2.2-1"]}]
    texts = [ActionText(group_id="G-01", problem="p", hazard="h", solution="1至3个月内更换")]
    assert "3个月" in deadline_findings(groups, texts)["3.2"]["problem"]
    ok = [ActionText(group_id="G-01", problem="p", hazard="h", solution="一个月内更换")]
    assert deadline_findings(groups, ok) == {}
    ledger = {"2.2": [_row("J-2.2-1", ["E-1"])]}
    late = Phase(phase="p", time="2个月内", tasks="t", effect="e", row_ids=["J-2.2-1"])
    assert "J-2.2-1" in phase_findings([late], ledger)["1.2"]["problem"]
    same = Phase(phase="p", time="1个月内", tasks="t", effect="e", row_ids=["J-2.2-1"])
    assert phase_findings([same], ledger) == {}


def test_a_field_called_missing_but_recorded_elsewhere_is_flagged() -> None:
    items = [
        _item("E-1", "低配", "SPD=OK", field="SPD"),
        _item("E-2", "接地", "接地电阻=0.8Ω", field="接地电阻"),
        _item("E-3", "接地", "接地电阻=1.1Ω", field="接地电阻", subject="B房/2#柜", place="B房"),
    ]
    sites = Sites(items)
    ledger = {"2.4": [_row("J-2.4-1", ["E-1"], disposition="undetermined")]}
    found = recorded_field_findings(ledger, sites)["2.4"]["J-2.4-1"]
    # The row's own object has the value: a contradiction, not a hint.
    assert found["severity"] == "blocking" and "E-2" in found["problem"] and "E-3" not in found["problem"]
    # A blank cell is not a value; a row whose basis names the column has said how it differs.
    blank = Sites([*items[:1], _item("E-4", "接地", "接地电阻=未填写", field="接地电阻")])
    assert recorded_field_findings(ledger, blank) == {}
    told = {"2.4": [_row("J-2.4-1", ["E-1"], disposition="undetermined").model_copy(
        update={"basis": "接地表的接地电阻是单次读数，缺的是复测记录"})]}
    assert recorded_field_findings(told, sites) == {}
    # A row on the sheet that holds the field speaks of its own cells.
    reading = {"2.4": [_row("J-2.4-2", ["E-2"], disposition="undetermined")]}
    assert recorded_field_findings(reading, sites) == {}
    assert "接地：接地电阻（2）" in project_map(sites)


def _task(name: str):
    import yaml

    from manyselves.kernel.definitions.models import TaskDefinition

    path = Path("manyselves/capabilities/distribution_reporting/tasks") / f"{name}.yaml"
    return TaskDefinition.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))


def test_writer_and_editor_messages_open_with_the_same_pack() -> None:
    from manyselves.runtime.definition_agent import DefinitionAgentInvoker as DefinitionAgent
    from manyselves.runtime.tools.registry import ToolRegistry

    pack = "数字与篇幅\n方法\n事实" * 50
    writer = DefinitionAgent._prompt(_task("write-section"), {"pack": pack, "x": 1}, ToolRegistry())
    editor = DefinitionAgent._prompt(_task("edit-sections"), {"pack": pack, "y": 2}, ToolRegistry())
    block = f'<context name="pack">\n{pack}\n</context>'
    assert writer.startswith(block) and editor.startswith(block)
    checker = _task("check-chief-facts")
    assert list(checker.shared_fields) == ["map", "story_lines"] and checker.static_first
