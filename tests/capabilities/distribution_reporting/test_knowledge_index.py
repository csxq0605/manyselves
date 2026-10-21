"""The knowledge base's own field index decides default sections; authors may depart."""

from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import (
    JudgmentRow,
    RowRelation,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_module import (
    checked_relations,
    unlisted_relations,
)
from manyselves.capabilities.distribution_reporting.runtime.knowledge_index import (
    build_index,
    route_evidence,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import (
    placement_findings,
)
from manyselves.capabilities.distribution_reporting.runtime.mappers.common import make_evidence
from manyselves.capabilities.distribution_reporting.runtime.mappers.s4_6 import _route_path
from manyselves.capabilities.distribution_reporting.runtime.rule_standards import (
    knowledge_units,
)

KNOWLEDGE = """
### KU-2.3-02 零序/漏电的防范

所属类别：2.3.2 零序/漏电的防范
限值/阈值：原10A候选尚不能充当统一安全线。
结论标准：绝对值、比例和原因分别表达。
相关标准：GB 13955-2017

### KU-2.4-11 低压回路剩余电流过大

所属类别：2.4.3 带病运行问题汇总
相关标准：GB 50054-2011

检索关联主题：2.4.3.1 低压回路剩余电流过大。仅辅助检索。

## 八、附录

### 附录D：诊断工作用表对应检测项索引

条目：剩余电流
S4-4表字段：剩余电流
对应知识单元编号：KU-2.3-02 / KU-2.4-11
对应知识单元名称：零序保护与剩余电流保护评估

条目：变压器噪声震动
S4-4表字段：变压器噪声震动
对应知识单元编号：待按实际检查内容匹配
对应知识单元名称：保留原观测并另核适用方法
"""


def _record(field: str, cell: str = "J3") -> object:
    return make_evidence(
        file_id="F1", path=Path("Inputs/S4-4.xlsx"), sheet="低配评估详情", cell=cell,
        row=3, column=cell[0], subject="配电房/1#柜", fact=f"{field}=1A",
        checkpoint_field=field,
    )


def _row(row_id: str, submodule: str, evidence: list[str], **extra) -> JudgmentRow:
    return JudgmentRow(
        id=row_id, submodule_id=submodule, object="1#柜", checkpoint="剩余电流",
        disposition="no_concern", evidence_status="现场确认", evidence_ids=evidence,
        basis="依据记录", **extra,
    )


def test_index_reads_unit_sections_and_fields():
    index = build_index([KNOWLEDGE])
    assert index.unit_sections["KU-2.3-02"] == ("2.3.2",)
    # The topic names the leaf subsection; the category is only a heading.
    assert index.unit_sections["KU-2.4-11"] == ("2.4.3.1",)
    assert index.fields["剩余电流"].units == ("KU-2.3-02", "KU-2.4-11")
    assert index.fields["变压器噪声震动"].units == ()


def test_field_with_two_units_goes_to_both_sections_in_knowledge_order():
    routed = route_evidence([_record("剩余电流(A)")], build_index([KNOWLEDGE]))
    assert [item.submodule_id for item in routed] == ["2.3.2", "2.4.3.1"]
    assert [item.knowledge_units for item in routed] == [["KU-2.3-02"], ["KU-2.4-11"]]
    assert len({item.id for item in routed}) == 2


def test_field_the_knowledge_base_leaves_open_stays_unassigned_with_its_note():
    (item,) = route_evidence([_record("变压器噪声震动", "U3")], build_index([KNOWLEDGE]))
    assert item.module_id is None and item.submodule_id is None
    assert item.knowledge_note == "保留原观测并另核适用方法"


def test_without_a_field_index_nothing_is_routed():
    (item,) = route_evidence([_record("剩余电流")], build_index([]))
    assert item.module_id is None


def test_standards_carry_limits_and_criteria():
    unit = knowledge_units([KNOWLEDGE])["KU-2.3-02"]
    assert unit["limits"].startswith("原10A候选")
    assert unit["criteria"] == "绝对值、比例和原因分别表达。"


def test_path_matches_titles_exactly():
    assert _route_path("配电设备/元件内在风险＞安装规范性问题＞标牌标识") == ("2.4", "2.4.2.4", None)
    # A path that stops at a heading stays with its module; the author chooses.
    assert _route_path("配电设备/元件内在风险＞配置与选型问题") == ("2.4", None, None)
    module, section, gap = _route_path("配电设备/元件内在风险＞配置与选型问题＞电压事件（过压）的防范")
    assert (module, section) == ("2.3", "2.3.3") and gap
    module, section, gap = _route_path("配电设备/元件内在风险＞没有这一段")
    assert (module, section) == ("2.4", None) and gap
    assert _route_path("没有这个模块＞x") == (None, None, None)


def test_departure_needs_a_unit_serving_the_chosen_section():
    ledger = {"2.4": [
        _row("J-2.4-01", "2.4.2.2", ["E-1"], placement_reason="接地问题", rule_refs=["KU-2.4-11"]),
        _row("J-2.4-02", "2.4.3.1", ["E-2"], placement_reason="同一回路", rule_refs=["KU-2.4-11"]),
    ]}
    sections = {"KU-2.4-11": ["2.4.3.1"]}
    found, departures = placement_findings(
        ledger, [], {"E-1": "2.3.2", "E-2": "2.3.2"}, sections,
    )
    assert "J-2.4-01" in found["2.4"] and "J-2.4-02" not in found["2.4"]
    assert [d["row_id"] for d in departures] == ["J-2.4-01", "J-2.4-02"]


def test_citing_another_section_without_a_reason_is_not_a_departure():
    ledger = {"2.4": [_row("J-2.4-01", "2.4.3.1", ["E-1"])]}
    found, departures = placement_findings(ledger, [], {"E-1": "2.3.2"}, {})
    assert found == {"2.4": {}} and departures == []


def test_unclaimed_unassigned_record_goes_back_to_every_module():
    ledger = {"2.2": [], "2.4": [_row("J-2.4-01", "2.4.4", ["E-1"], rule_refs=["KU-2.4-14"])]}
    found, _ = placement_findings(ledger, ["E-1", "E-2"], {}, {"KU-2.4-14": ["2.4.4"]})
    assert set(found["2.2"]) == {"E-2"} and set(found["2.4"]) == {"E-2"}
    assert found["2.2"]["E-2"]["severity"] == "blocking"


def test_unlisted_relation_kept_only_with_mechanism_and_condition():
    other = _row("J-2.2-01", "2.2.2.1", ["E-9"])
    grounded = RowRelation(
        row_id="J-2.2-01", relation="积灰—发热", reason="同柜",
        mechanism="积灰阻碍散热", condition="同一柜体，出风口积灰且温升偏高",
    )
    bare = RowRelation(row_id="J-2.2-01", relation="随便", reason="同柜")
    listed = RowRelation(row_id="J-2.2-01", relation="负荷—发热", reason="负荷高")
    ledger = {
        "2.2": [other],
        "2.4": [_row("J-2.4-01", "2.4.4", ["E-1"], relations=[grounded, bare, listed])],
    }
    kept, dropped = checked_relations(ledger, ["负荷—发热"])
    assert [r.relation for r in kept["2.4"][0].relations] == ["积灰—发热", "负荷—发热"]
    assert dropped["J-2.4-01"][0]["relation"] == "随便"
    (candidate,) = unlisted_relations(kept, ["负荷—发热"])
    assert candidate["relation"] == "积灰—发热" and candidate["to"]["row_id"] == "J-2.2-01"
