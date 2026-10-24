"""Source projections must not discard late limitations via raw string slicing."""

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    _source_projection as chief_sources,
)
from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
    _source_projection as final_sources,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossSynthesisInput,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SpecialTopicPlan,
    SpecialTopicSectionRequirement,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _source_state() -> dict:
    module = ModuleSubmission(
        module_id="2.1", submodule_narratives={
            key: "合成正文" for key in REPORT_TAXONOMY["2.1"].submodules
        },
        claims=[], source_ids=[], revision=0,
        unresolved_questions=[f"设备{i}：还缺运行工况与测量条件，不能据此判断正常。" for i in range(130)],
    )
    synthesis = CrossSynthesisInput(
        id="S-long", related_module_ids=["2.1", "2.2"], cluster_type="synthetic",
        root_causes=["虚构前提"], propagation_steps=["合成步骤一", "合成步骤二"],
        causal_chain="虚构关联材料，仅供验证输入传递，不代表真实项目结论。" * 260,
        decision_implication="仅用于验证不同条件的保持，不作为实际配电决策依据。",
        action_dependencies=["合成依赖"], joint_actions=["合成行动"],
        verification_method="仅用于验证来源材料尾部是否被完整保留，不实施实际操作。",
        acceptance_criteria=["尾部来源可见"], module_statement_refs=["2.1.1", "2.2.1.1"],
        confidence_and_boundary="最后的必要限定：工况 B 不适用，不得将工况 A 的判断推广过去。",
        target_report_section_ids=["3.2"], evidence_refs=["E-0001"],
    )
    plan = SpecialTopicPlan(
        source_ref="Inputs/synthetic-topic.md", source_sha256="0" * 64,
        sections=[SpecialTopicSectionRequirement(
            section_id="4.1", title="合成专题",
            requirement="需要按对象保留来源、适用条件、数值、结论和例外。" * 560 + "最后：仅限工况 A。",
        )],
    )
    return {"run_id": "source-complete", "module_submissions": {"2.1": module},
            "cross_synthesis_inputs": [synthesis], "special_topic_plan": plan}


@pytest.mark.parametrize("role", ["chief", "final"])
@pytest.mark.parametrize("chapter, key", [("1", "module-2.1"), ("3", "cross_synthesis"), ("4", "special_topic_plan")])
def test_assigned_source_projection_preserves_complete_json_and_late_conditions(
    tmp_path: Path, role: str, chapter: str, key: str,
) -> None:
    state = _source_state()
    store = ReportingStore(tmp_path)
    if role == "chief":
        context, _ = chief_sources(state, chapter, store=store)
    else:
        context, _ = final_sources(state, chapter, state["special_topic_plan"], store=store)
    value = json.loads(context[key])
    if chapter == "1":
        assert value["unresolved_questions"] == state["module_submissions"]["2.1"].unresolved_questions
    elif chapter == "3":
        assert value == [item.model_dump(mode="json") for item in state["cross_synthesis_inputs"]]
    else:
        assert value == state["special_topic_plan"].model_dump(mode="json")
