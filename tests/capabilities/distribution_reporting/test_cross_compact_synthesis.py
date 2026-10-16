"""Cross supplies one supported relation; Chief expands it without losing constraints."""

import json

import pytest

from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    _source_projection as chief_sources,
)
from manyselves.capabilities.distribution_reporting.runtime.contracts.submissions import (
    submission_schema,
)
from manyselves.capabilities.distribution_reporting.runtime.final_review_tools import (
    _source_projection as final_sources,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossSynthesisInput,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from tests.capabilities.distribution_reporting.test_chief_source_completeness import _source_state

MERGED_FIELDS = {
    "root_causes": "causal_chain",
    "propagation_steps": "causal_chain",
    "action_dependencies": "decision_implication",
    "joint_actions": "decision_implication",
    "acceptance_criteria": "verification_method",
}


def _expanded_relation():
    return {
        "id": "S-conditions", "related_module_ids": ["2.1", "2.2"],
        "cluster_type": "same-condition dependency",
        "causal_chain": "Supported dependency applies only to the stated observation and operating condition.",
        "root_causes": ["COMMON-PRECONDITION"],
        "propagation_steps": ["FIRST-LINK", "SECOND-LINK"],
        "decision_implication": "The second action depends on completion of the first action, within the same condition.",
        "action_dependencies": ["DEPENDENCY-NO-REORDER"],
        "joint_actions": ["COORDINATED-ACTION"],
        "verification_method": "Recheck both current observations under the same stated operating condition.",
        "acceptance_criteria": ["ACCEPTANCE-EXACT-CONDITION"],
        "module_statement_refs": ["2.1.1", "2.2.1.1"],
        "confidence_and_boundary": "Only applies to condition A; condition B has not been observed or verified.",
        "target_report_section_ids": ["3.2"], "evidence_refs": ["E-0001"],
    }


@pytest.mark.parametrize("role", ["chief", "final"])
def test_compact_relation_reaches_chief_and_final_with_trace_and_boundaries(tmp_path, role):
    payload = {key: value for key, value in _expanded_relation().items() if key not in MERGED_FIELDS}
    relation = CrossSynthesisInput.model_validate(payload)
    state = _source_state()
    state["cross_synthesis_inputs"] = [relation]
    store = ReportingStore(tmp_path)
    if role == "chief":
        context, _ = chief_sources(state, "3", store=store)
    else:
        context, _ = final_sources(state, "3", state["special_topic_plan"], store=store)
    assert json.loads(context["cross_synthesis"]) == [payload]


def test_restored_expanded_relation_preserves_detail_without_repeating_fields():
    payload = _expanded_relation()
    relation = CrossSynthesisInput.model_validate(payload)
    compact = relation.model_dump(mode="json")
    assert len(compact) == 10
    assert not (set(compact) & MERGED_FIELDS.keys())
    for old, destination in MERGED_FIELDS.items():
        assert all(value in compact[destination] for value in payload[old])
        assert payload[destination] in compact[destination]
    # Serializing and restoring again must not append the same details twice.
    assert CrossSynthesisInput.model_validate_json(relation.model_dump_json()) == relation


def test_actual_cross_submit_contract_requests_one_expression_per_relation_dimension():
    schema = submission_schema("cross_review_finding_submission")
    relation = schema["$defs"]["CrossSynthesisInput"]
    assert len(relation["required"]) == 10
    assert not (set(relation["properties"]) & MERGED_FIELDS.keys())
    assert set(relation["required"]) == set(_expanded_relation()) - MERGED_FIELDS.keys()
