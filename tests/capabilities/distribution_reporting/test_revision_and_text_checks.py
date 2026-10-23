"""Row patches, slot repairs, copied instructions, same-observation grading and judge groups."""

from typing import Any

from manyselves.capabilities.distribution_reporting.domain.copied_text import CopiedText, skill_parts
from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    ActionText,
    JudgmentRow,
    RowRelation,
    action_group_findings,
    apply_row_patches,
)
from manyselves.capabilities.distribution_reporting.domain.value_slots import (
    UNRESOLVED,
    fill_slots,
    slot_repairs,
    variant_kind,
)
from manyselves.capabilities.distribution_reporting.runtime.context_packs import section_groups
from manyselves.capabilities.distribution_reporting.runtime.cross_module import (
    checked_relations,
    same_observation_findings,
    shared_observation_groups,
)


def _row(row_id: str, submodule: str, *, evidence: list[str], risk: str = "中", **extra: Any) -> JudgmentRow:
    return JudgmentRow.model_validate({
        "id": row_id, "submodule_id": submodule, "object": "1#柜", "checkpoint": "c",
        "disposition": "concern", "evidence_status": "现场确认", "evidence_ids": evidence,
        "basis": "b", "finding": "f", "risk_level": risk, "hazard": "可能过热，待核实。",
        "hazard_conditions": ["温度持续偏高"],
        "action": {"measure": "复测", "priority": "一般", "timeframe": "一月"},
        **extra,
    })


SLOTS = {"V-0089": {"text": "186A", "subject": "文体中心配电房/1D1"}}


def test_a_slot_suffix_naming_its_own_object_is_filled_as_object() -> None:
    assert variant_kind(SLOTS["V-0089"], "文体中心配电房/1D1") == "对象"
    assert variant_kind(SLOTS["V-0089"], "1D1") == "对象"
    assert variant_kind(SLOTS["V-0089"], "别的柜") is None
    assert fill_slots("〔V-0089:1D1〕", SLOTS) == "文体中心配电房/1D1 186A"


def test_composition_fills_every_slot_and_lists_the_repairs() -> None:
    text = "电流〔V-0089:别的柜〕，另见〔V-9999〕。"
    assert fill_slots(text, SLOTS, unresolved=UNRESOLVED) == f"电流186A，另见{UNRESOLVED}。"
    repairs = slot_repairs(text, SLOTS)
    assert len(repairs) == 2 and "186A" in repairs[0] and UNRESOLVED in repairs[1]


def test_copied_instruction_text_is_found_and_project_text_passes() -> None:
    skill = "## 2.1\n判断方法：\n先看负荷率是否超过限值并说明理由\n写法：\n先写现状，再以条件句提出可能的后果和建议\n"
    instructions = skill_parts(skill, ["写法"])
    assert "条件句" in instructions and "负荷率" not in instructions
    copied = CopiedText([instructions], allowed=["1#柜负荷率偏高需要复测的情况"])
    assert copied.spans("1#柜现状偏高，再以条件句提出可能的后果和建议。")
    assert copied.spans("先看负荷率是否超过限值并说明理由") == []
    assert copied.spans("〔V-0001〕1#柜负荷率偏高需要复测的情况") == []


def test_a_patch_changes_only_its_fields_and_bad_patches_are_problems() -> None:
    rows = [_row("J-2.4-1", "2.4.1", evidence=["E-1"])]
    changed, removed, problems = apply_row_patches(rows, {
        "patches": [
            {"row_id": "J-2.4-1", "fields": {"basis": "新依据"}},
            {"row_id": "J-2.4-9", "fields": {"basis": "x"}},
        ],
        "removed_row_ids": ["J-2.4-2"],
    })
    assert [(row.id, row.basis, row.finding) for row in changed] == [("J-2.4-1", "新依据", "f")]
    assert removed == ["J-2.4-2"] and set(problems) == {"J-2.4-9"}
    _changed, _removed, problems = apply_row_patches(
        rows, {"patches": [{"row_id": "J-2.4-1", "fields": {"id": "J-2.4-5", "nope": 1}}]}
    )
    assert "nope" in problems["J-2.4-1"] and "id" in problems["J-2.4-1"]


def test_rows_sharing_any_observation_are_graded_once() -> None:
    # The cabinet's reading (O-1) is one cell routed to 2.4.1 (its owner) and
    # 2.2.1; the 2.2 row also cites another record.
    observation_of = {"E-1": "O-1", "E-2": "O-1"}
    ledger = {
        "2.4": [_row("J-2.4-1", "2.4.1", evidence=["E-1"], risk="高")],
        "2.2": [_row("J-2.2-1", "2.2.1", evidence=["E-2", "E-7"], risk="中")],
    }
    (group,) = shared_observation_groups(ledger, observation_of)
    assert {row.id for row in group} == {"J-2.4-1", "J-2.2-1"}
    found = same_observation_findings(ledger, observation_of, {"O-1": "2.4.1"})
    assert list(found) == ["2.2"] and found["2.2"]["J-2.2-1"]["severity"] == "blocking"


def test_a_pending_relation_stays_only_with_what_is_missing() -> None:
    other = _row("J-2.5-1", "2.5.1", evidence=["E-5"])
    pending = RowRelation(row_id="J-2.5-1", relation="r", reason="x", status="待核实", missing="缺测温记录")
    vague = RowRelation(row_id="J-2.5-1", relation="r2", reason="x", status="待核实")
    ledger = {
        "2.4": [_row("J-2.4-1", "2.4.1", evidence=["E-1"], relations=[pending, vague])],
        "2.5": [other],
    }
    kept, _dropped = checked_relations(ledger, {"r", "r2"}, {})
    assert [(r.relation, r.pending) for r in kept["2.4"][0].relations] == [("r", True)]


def test_an_action_group_with_several_deadlines_says_what_happens_meanwhile() -> None:
    groups = [{"group_id": "G-01", "timeframes": ["立即", "三个月内"]}]
    text = ActionText(group_id="G-01", problem="p", hazard="若a，可能b", solution="s")
    assert "interim" in str(action_group_findings(groups, [text]))
    done = text.model_copy(update={"interim": "整改完成前，作业前先验电并挂牌"})
    assert action_group_findings(groups, [done]) == {}


def test_a_large_module_is_judged_in_consecutive_section_groups() -> None:
    records = {"2.4.1": 30, "2.4.2": 27, "2.4.3": 40, "2.4.4": 18}
    groups = section_groups(list(records), records, budget_records=60)
    assert [s for group in groups for s in group] == list(records)
    assert len(groups) == 2
    assert section_groups(["2.1.1"], {"2.1.1": 5}, budget_records=60) == [["2.1.1"]]
