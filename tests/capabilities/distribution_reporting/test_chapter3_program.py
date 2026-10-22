"""Chapter 3's groups, order and two layers come from the program; the chief words them."""

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    ActionText,
    ChiefChapter3,
    CrossFinding,
    JudgmentRow,
    action_group_findings,
    action_groups,
    action_table_markdown,
    grouped_actions,
    problem_table,
    risk_overview_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    SAME_NAME,
    SAME_RECORD_ROW,
    object_level,
)


def _row(row_id, obj, evidence, priority="一般", level="中", status="现场确认", measure="m"):
    return JudgmentRow.model_validate(
        {"id": row_id, "submodule_id": "2.4.1", "object": obj, "checkpoint": "自拟检查点",
         "disposition": "concern", "evidence_status": status, "evidence_ids": [evidence],
         "basis": "b", "risk_level": level, "hazard": "h", "hazard_conditions": ["c"],
         "action": {"measure": measure, "priority": priority, "timeframe": "一月"}}
    )


def test_actions_group_by_priority_and_declared_checkpoint_in_risk_order() -> None:
    ledger = {
        "2.4": [
            _row("J-1", "1#", "E-1", level="低"),
            _row("J-2", "2#", "E-2", level="高"),
            _row("J-3", "3#", "E-3", priority="重要，紧急"),
        ],
        "2.2": [_row("J-4", "4#", "E-4", level="高")],
    }
    checkpoint_of = {"E-1": "SPD", "E-2": "SPD", "E-3": "SPD", "E-4": "温升"}
    groups = action_groups(ledger, checkpoint_of)
    assert [(g["priority"], g["checkpoint"], g["row_ids"]) for g in groups] == [
        ("重要，紧急", "SPD", ["J-3"]),
        ("一般", "SPD", ["J-1", "J-2"]),
        ("一般", "温升", ["J-4"]),
    ]
    texts = [ActionText(group_id="G-01", problem="p", hazard="h", solution="s")]
    assert "G-02" in action_group_findings(groups, texts)["3.2"]["problem"]
    # A group the chief left out keeps its rows' own words; no action row drops out.
    actions = grouped_actions(groups, texts)
    assert [a.row_ids for a in actions] == [["J-3"], ["J-1", "J-2"], ["J-4"]]
    assert actions[1].problem.startswith("1#、2#")


def test_a_note_every_action_of_a_priority_shares_is_said_once() -> None:
    rows = {"J-1": "来源作者意见", "J-2": "来源作者意见"}
    groups = [
        {"group_id": "G-01", "priority": "一般", "checkpoint": "a", "row_ids": ["J-1"],
         "objects": ["1#"], "measures": ["m"], "timeframes": ["一月"]},
        {"group_id": "G-02", "priority": "一般", "checkpoint": "b", "row_ids": ["J-2"],
         "objects": ["2#"], "measures": ["m"], "timeframes": ["一月"]},
    ]
    table = action_table_markdown("措施如下。", grouped_actions(groups, []), rows)
    assert table.count("来源作者意见") == 1
    assert "**一般 · 以下各项依据：来源作者意见**" in table


def test_risk_overview_has_two_layers_one_line_each_with_cross_findings_kept() -> None:
    chapter3 = ChiefChapter3(
        risk_summary=["散热条件差，温升偏高。"],
        risk_details=[{"module_id": "2.2", "points": ["积灰。", "缺空调。"]}],
        cross_findings=[CrossFinding(subject="1#", conclusion="温升与积灰相互印证。",
                                     row_ids=["J-2.2-1", "J-2.4-1"])],
        action_lead="措施如下。",
        actions=[],
    )
    text = risk_overview_markdown(chapter3, {"2.2": "环境"})
    assert text.startswith("风险汇总如下：\n\n1. 散热条件差，温升偏高。\n2. 温升与积灰相互印证。")
    assert "详述见下：\n\n**环境**\n\n- 积灰。\n- 缺空调。" in text


def test_a_status_under_the_problem_table_is_said_once() -> None:
    table = problem_table([
        _row("J-1", "1#", "E-1", status="来源作者意见"),
        _row("J-2", "2#", "E-2", status="来源作者意见"),
    ])
    assert table.count("来源作者意见") == 1
    assert "1#、2#的证据状态为来源作者意见" in table


def test_object_level_follows_the_tables_record_rows() -> None:
    assert object_level([{"record_row": "f:低配!3"}, {"record_row": "f:低配!3"}]) == SAME_RECORD_ROW
    assert object_level([{"record_row": "f:低配!3"}, {"record_row": "f:红外!7"}]) == SAME_NAME
    assert object_level([{"record_row": None}]) == SAME_NAME
