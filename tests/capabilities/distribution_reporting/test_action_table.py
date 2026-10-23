"""3.2 follows the expert report's grouped action table, with the group number (G-xx) first."""

from manyselves.capabilities.distribution_reporting.domain.action_table import (
    action_table_problems,
)

HEADER = "| 编号 | 评估内容 | 发现问题 | 风险与危害 | 解决方案 | 备注 |\n| --- | --- | --- | --- | --- | --- |"
ROW = "| G-01 | 设备＞安装 | 工艺配电房电缆导体裸露 | 触电 | 建议立即做绝缘处理 |  |"


def test_grouped_action_table_passes() -> None:
    body = "\n".join([
        "根据评估结果，给出以下行动。",
        "",
        "**重要，紧急 · 芜湖区域**",
        HEADER,
        ROW,
        "",
        "**一般**",
        HEADER,
        ROW,
    ])
    assert action_table_problems(body) == []


def test_free_text_or_other_columns_are_reported() -> None:
    assert action_table_problems("按依赖顺序列出责任接口和验收。") == ["no action table found"]
    other = "**重要，紧急**\n| 对象 | 措施 | 期限 |\n| --- | --- | --- |\n| A | B | C |"
    assert any("header" in problem for problem in action_table_problems(other))


def test_table_needs_a_priority_label_and_object_and_action() -> None:
    unlabeled = "\n".join([HEADER, ROW])
    assert any("priority label" in problem for problem in action_table_problems(unlabeled))
    empty = "\n".join(["**重要，不紧急**", HEADER, "| G-02 | 设备 |  | 风险 |  |  |"])
    problems = action_table_problems(empty)
    assert any("发现问题" in problem for problem in problems)
    assert any("解决方案" in problem for problem in problems)
