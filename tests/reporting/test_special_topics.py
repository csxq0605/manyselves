from pathlib import Path

import pytest

from manyselves.core.reporting.special_topics import load_special_topic_plan


def test_special_topic_plan_is_parsed_from_one_standalone_inputs_markdown(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs/客户专项问题分析.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# 专项问题分析\n\n"
        "## 4.1 柴油发电机并机条件\n\n"
        "说明适用边界、关键校核、联锁条件和验收方法。\n\n"
        "## 能源管理平台建设路径\n\n"
        "比较分阶段方案、数据前提、组织接口和验证指标。\n",
        encoding="utf-8",
    )

    plan = load_special_topic_plan(tmp_path)

    assert plan.source_ref == Path("Inputs/客户专项问题分析.md")
    assert [section.section_id for section in plan.sections] == ["4.1", "4.2"]
    assert [section.title for section in plan.sections] == [
        "柴油发电机并机条件",
        "能源管理平台建设路径",
    ]
    assert "联锁条件" in plan.sections[0].requirement
    assert len(plan.source_sha256) == 64


def test_special_topic_plan_is_optional_when_file_is_missing_or_empty(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()

    assert load_special_topic_plan(tmp_path) is None

    (inputs / "专项问题分析.md").write_text(
        "\ufeff\n \t\n",
        encoding="utf-8",
    )
    assert load_special_topic_plan(tmp_path) is None


def test_special_topic_plan_rejects_multiple_named_markdown_files(
    tmp_path: Path,
) -> None:
    inputs = tmp_path / "Inputs"
    inputs.mkdir()
    (inputs / "专项问题分析-A.md").write_text(
        "## 问题 A\n\n形成完整分析要求。",
        encoding="utf-8",
    )
    (inputs / "专项问题分析-B.md").write_text(
        "## 问题 B\n\n形成完整分析要求。",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="exactly one"):
        load_special_topic_plan(tmp_path)


def test_special_topic_plan_rejects_missing_requirements_and_bad_numbering(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs/专项问题分析.md"
    source.parent.mkdir(parents=True)
    source.write_text("## 4.2 编号跳跃\n\n简要要求。", encoding="utf-8")
    with pytest.raises(ValueError, match="expected 4.1"):
        load_special_topic_plan(tmp_path)

    source.write_text("## 目标专项\n", encoding="utf-8")
    with pytest.raises(ValueError, match="brief requirement"):
        load_special_topic_plan(tmp_path)


def test_special_topic_plan_validates_chief_headings_against_inputs(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Inputs/专项问题分析.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "## 目标专项\n\n分析边界、建议和验证方法。",
        encoding="utf-8",
    )
    plan = load_special_topic_plan(tmp_path)

    plan.validate_analysis(
        "### 4.1 目标专项\n\n"
        "结合项目事实边界形成判断、建议和可以复核的验证方法。\n\n"
        "#### 4.1.1 验证步骤\n\n"
        "逐项记录责任接口、验证输入、验收结果和剩余风险。"
    )
    with pytest.raises(ValueError, match="exactly match"):
        plan.validate_analysis(
            "### 4.1 擅自改名\n\n"
            "结合项目事实边界形成判断、建议和可以复核的验证方法。"
        )
    with pytest.raises(ValueError, match="outside its planned parent"):
        plan.validate_analysis(
            "### 4.1 目标专项\n\n"
            "结合项目事实边界形成判断、建议和可以复核的验证方法。\n\n"
            "#### 4.2.1 越界小标题\n\n"
            "该小标题不属于计划中的 4.1。"
        )
