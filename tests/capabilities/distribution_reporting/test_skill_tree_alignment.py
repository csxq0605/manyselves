"""Skills follow this run's section tree by meaning, not by number."""

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    bound_report_taxonomy,
    example_report_taxonomy,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skills import (
    align_skill_sections,
    module_skill_roles,
    skills_for_run,
)

SKILL = """---
name: author-2.4
---
# 2.4 配电设备/元件风险的方法

## 2.4 配电设备/元件风险
### 判断方法
- 模块总体判断

## 2.4.3.1 低压回路剩余电流过大
### 判断方法
- 剩余电流的判断

## 2.4.3.2 部分高压柜照明功能缺失
### 写法
- 柜内照明的写法
"""


def _tree(edit) -> dict:
    tree = example_report_taxonomy()
    edit(tree)
    return tree


def _drop(section_id):
    def edit(tree):
        for module in tree["modules"]:
            module["sections"] = [s for s in module["sections"] if s["id"] != section_id]
    return edit


def _renumber(old, new):
    def edit(tree):
        for module in tree["modules"]:
            for section in module["sections"]:
                if section["id"] == old:
                    section["id"] = new
    return edit


def test_same_tree_keeps_the_skill_as_written():
    text, notes = align_skill_sections(SKILL, "2.4")
    assert "## 2.4.3.1 低压回路剩余电流过大" in text and notes == []


def test_a_section_numbered_differently_is_found_by_its_title():
    with bound_report_taxonomy(_tree(_renumber("2.4.3.1", "2.4.3.9"))):
        text, notes = align_skill_sections(SKILL, "2.4")
    assert "## 2.4.3.9 低压回路剩余电流过大" in text
    assert "- 剩余电流的判断" in text
    assert any("→ 本次 2.4.3.9" in note for note in notes)


def test_a_section_the_table_leaves_out_is_left_out_of_the_skill():
    with bound_report_taxonomy(_tree(_drop("2.4.3.2"))):
        text, notes = align_skill_sections(SKILL, "2.4")
    assert "柜内照明" not in text
    assert "- 剩余电流的判断" in text
    assert any("没有这一节" in note for note in notes)


def test_module_skill_is_chosen_by_the_module_title():
    def swap(tree):
        # This table numbers its modules differently: 2.4's content is 2.3.
        by_id = {m["id"]: m for m in tree["modules"]}
        by_id["2.3"]["title"], by_id["2.4"]["title"] = by_id["2.4"]["title"], by_id["2.3"]["title"]

    assert module_skill_roles()["2.4"] == "author-2.4"
    with bound_report_taxonomy(_tree(swap)):
        roles = module_skill_roles()
    assert roles["2.3"] == "author-2.4" and roles["2.4"] == "author-2.3"


def test_a_module_the_table_leaves_out_drops_its_skill():
    def drop_module(tree):
        tree["modules"] = [m for m in tree["modules"] if m["id"] != "2.5"]

    with bound_report_taxonomy(_tree(drop_module)):
        skills, by_module, notes = skills_for_run({"author-2.4": SKILL, "author-2.5": "x"})
    assert "author-2.5" not in skills and "2.5" not in by_module
    assert by_module["2.4"].startswith("---")
    assert any("author-2.5" in note for note in notes)
