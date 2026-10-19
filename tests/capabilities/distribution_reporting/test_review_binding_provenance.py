"""Whole-part citation bindings must not masquerade as per-observation judgments."""

from types import SimpleNamespace

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    SubmitResultTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ClaimRecord
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ReviewEvidenceExcerpt,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_delta import (
    build_module_recheck_delta,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    _claim_statements,
    _review_evidence,
    _review_evidence_by_ids,
)
from tests.capabilities.distribution_reporting.test_module_review_delta import _module
from tests.capabilities.distribution_reporting.test_project_fact_packet import _facts, _write_facts


def test_runtime_part_claim_is_explicitly_a_binding_not_a_semantic_judgment():
    assembler = object.__new__(SubmitResultTool)
    assembler._runtime_claim_boundary = lambda evidence_ids: (0.95, False)
    prose = "Observation, a conditional interpretation and an action in one whole section."
    raw = assembler._runtime_claim_for_part(
        module_id="2.4", part_id="2.4.3.1", prose=prose, evidence_ids=["E-0001"]
    )
    assert raw["origin"] == "runtime_part_binding"
    claim = ClaimRecord.model_validate(raw)
    restored = ClaimRecord.model_validate_json(claim.model_dump_json())
    statement = _claim_statements([restored], {"2.4.3.1"})[0]
    assert statement.origin == "runtime_part_binding"
    assert statement.confidence is None  # Parsing confidence is not judgment confidence.
    assert statement.text == prose and statement.evidence_ids == ["E-0001"]


def test_recheck_delta_retains_binding_provenance_without_repeating_prose():
    baseline = _module(claim_text="whole section", revision=0)
    baseline.claims[0] = baseline.claims[0].model_copy(update={"origin": "runtime_part_binding"})
    current = baseline.model_copy(deep=True, update={"revision": 1})
    delta = build_module_recheck_delta(baseline, current, {"2.4.1.2"})
    statement = delta["claim_statements"][0]
    assert statement.origin == "runtime_part_binding" and statement.confidence is None
    assert statement.text == "whole section"


def test_evidence_packet_distinguishes_bound_and_unbound_objects_in_current_scope(tmp_path):
    _write_facts(tmp_path, _facts(2))
    claim = ClaimRecord(
        id="C-part",
        module_id="2.4",
        submodule_id="2.4.3.1",
        text="Section body",
        claim_type="technical_interpretation",
        source_ids=["E-0001"],
    )
    subject = SimpleNamespace(module_id="2.4", claims=[claim])
    packet = _review_evidence(tmp_path, subject, "facts", {"2.4.3.1"})
    assert packet[0].bound_submodule_ids == ["2.4.3.1"]
    assert packet[1].bound_submodule_ids == []
    # A historical finding reference with a removed current binding stays visible.
    extra = _review_evidence_by_ids(tmp_path, "facts", {"E-0001"}, current_claims=[])
    assert extra[0].bound_submodule_ids == [] and extra[0].content == packet[0].content
    assert "judgment_covered" not in packet[0].model_dump()


def test_legacy_provenance_is_unknown_and_not_reinterpreted():
    claim = _module(claim_text="old statement", revision=0).claims[0]
    assert claim.origin == "unspecified"
    excerpt = ReviewEvidenceExcerpt(
        evidence_id="E-0001", title="source", locator="J1", content="old record"
    )
    assert excerpt.bound_submodule_ids is None  # Unknown, not a computed empty mapping.
    assert _claim_statements([claim], {"2.4.1.2"})[0].origin == "unspecified"


def test_prepared_review_persists_provenance_and_unbound_observation_map(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        ModuleReviewInput,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRuntimeLaneContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
        prepare_current_module_review,
    )
    from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
        register_fact_sources,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
    from tests.capabilities.distribution_reporting.test_public_reporting_runtime import (
        _module_submission,
    )

    state = _write_facts(tmp_path, _facts(2))
    register_fact_sources(tmp_path, "facts", _facts(2))
    subject = _module_submission()
    claim = ClaimRecord(
        id="C-binding",
        module_id="2.4",
        submodule_id="2.4.3.1",
        text="Whole section with a bound source.",
        origin="runtime_part_binding",
        claim_type="technical_interpretation",
        source_ids=["E-0001"],
    )
    subject.claims = [claim]
    subject.source_ids = ["E-0001"]
    subject.submodule_narratives["2.4.3.1"] = claim.text + "\n\n[[CLAIM:C-binding]]"
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="module-report",
        reporting_state=state,
        status="authored",
        module=subject,
    )
    prepared = prepare_current_module_review(
        context, store=ReportingStore(tmp_path)
    ).review.prepared
    restored = ModuleReviewInput.model_validate_json(
        (tmp_path / prepared.envelope.input_refs[0]).read_text()
    )
    assert restored.claim_statements[0].origin == "runtime_part_binding"
    assert restored.claim_statements[0].confidence is None
    assert restored.evidence[0].bound_submodule_ids == ["2.4.3.1"]
    assert restored.evidence[1].bound_submodule_ids == []
    assert restored.validation_report.passed  # Unbound evidence is not a new gate.
