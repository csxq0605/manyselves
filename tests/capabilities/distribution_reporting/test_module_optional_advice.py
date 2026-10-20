"""Optional module advice must not create author/reviewer work or disappear on resume."""

import json

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ResolutionVerdict,
    RevisionResponse,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleReviewProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import (
    accept_current_module_recheck,
    prepare_current_module_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    accept_current_module_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (
    prepare_current_module_review,
)
from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
    accept_current_module_revision,
    prepare_current_module_revision,
)
from manyselves.capabilities.distribution_reporting.runtime.review_artifacts import (
    load_current_review_completion,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from tests.capabilities.distribution_reporting.test_module_revision_tools import _module
from tests.capabilities.distribution_reporting.test_module_runtime import _build_runtime


def _start(tmp_path):
    store = ReportingStore(tmp_path)
    context = prepare_current_module_review(
        DeclarativeModuleRuntimeLaneContext(
            module_id="2.1", workflow_id="public-reporting", status="authored",
            reporting_state={"run_id": "optional-advice"}, module=_module(),
        ),
        store=store,
    )
    assert context.review.prepared.mode == "invoke_agent"
    return store, context


def _finding(context, impact, *, round_number=0):
    return ModuleReviewFinding(
        id=f"M-2.1-initial-r{round_number}-{impact}",
        target_submodule_id="2.1.1", category="editorial_quality", impact=impact,
        observation="Synthetic located finding for the current module review routing test.",
        evidence_refs=[context.review.prepared.subject_ref],
        required_change="Consider a clearer presentation of the existing supported statement.",
        reviewer_checks=["Check against current evidence and the task requirements."],
    )


def _accept(store, context, impacts):
    findings = [_finding(context, impact) for impact in impacts]
    return accept_current_module_review(
        {"context": context, "result": {
            "status": "completed",
            "submission": ModuleReviewFindingSubmission(
                coverage={"submodule_ids": context.review.prepared.scope}, findings=findings,
            ),
        }}, store=store,
    )


def _completion(store, context):
    acceptance = context.review.acceptance
    return load_current_review_completion(
        workspace=store.workspace, completion_ref=acceptance.completion_ref,
        run_id="optional-advice", lifecycle="module", reviewer_agent_id="evidence-auditor",
        reviewer_session_key="module-auditor-2.1", subject_refs=[acceptance.subject_ref],
    )


@pytest.mark.parametrize("impacts", [[], ["advisory"], ["blocking"], ["advisory", "blocking"]])
def test_initial_review_defers_only_optional_advice_and_resumes(tmp_path, impacts):
    store, context = _start(tmp_path)
    accepted = _accept(store, context, impacts)
    acceptance = accepted.review.acceptance
    assert [f.impact for f in acceptance.findings] == [i for i in impacts if i == "blocking"]
    assert acceptance.next_action == ("revise" if "blocking" in impacts else "completed")
    artifact = json.loads((tmp_path / acceptance.finding_refs[0]).read_text())
    assert [f["impact"] for f in artifact["findings"]] == impacts

    progress = ModuleReviewProgress.model_validate_json(
        (tmp_path / acceptance.progress_ref).read_text(),
    )
    assert progress.review_protocol_version == 3
    runtime, *_ = _build_runtime(tmp_path)
    restored = accepted.model_copy(update={"status": "review_resumed", "review": accepted.review.model_copy(update={
        "acceptance": None,
        "prepared": context.review.prepared.model_copy(update={
            "mode": "continue_existing", "progress": progress,
        }),
    })})
    restored = DeclarativeModuleRuntimeLaneContext.model_validate_json(restored.model_dump_json())
    resumed = runtime.resume_review_lane(restored)
    assert resumed.review.acceptance.findings == acceptance.findings
    assert resumed.review.acceptance.finding_refs == acceptance.finding_refs
    assert resumed.review.acceptance.next_action == acceptance.next_action
    if "blocking" not in impacts:
        completion, _ = _completion(store, resumed)
        assert completion.resolved_finding_ids == []
        assert completion.verdict_refs == []
        assert completion.deferred_advisory_ids == (
            ["M-2.1-initial-r0-advisory"] if impacts else []
        )


@pytest.mark.parametrize("new_impact", ["advisory", "blocking"])
@pytest.mark.parametrize("protocol", [2, 3])
def test_recheck_retains_advice_across_revision_and_serialized_resume(tmp_path, new_impact, protocol):
    store, context = _start(tmp_path)
    if protocol == 2:
        # An active persisted lifecycle keeps its original required findings.
        context.review.prepared.progress = ModuleReviewProgress(
            run_id="optional-advice", module_id="2.1", current=context.module,
            next_action="review", review_protocol_version=2,
        )
    context = _accept(store, context, ["advisory", "blocking"])
    context = prepare_current_module_revision(context, store=store)
    required_ids = context.revision.prepared.required_finding_ids
    assert required_ids == (
        ["M-2.1-initial-r0-advisory", "M-2.1-initial-r0-blocking"]
        if protocol == 2 else ["M-2.1-initial-r0-blocking"]
    )
    context = accept_current_module_revision({"context": context, "result": {
        "status": "completed",
        "submission": ModuleRevisionSubmission(
            module_id="2.1", base_revision=0, revision=1,
            submodule_narratives={"2.1.1": "### 2.1.1\n\n当前证据边界和修订内容。"},
            source_ids=[], unresolved_questions=[],
            revision_responses=[RevisionResponse(
                finding_id=finding_id, action="implemented",
                summary="已按实际证据明确说明修订内容和可以复核的边界。",
                changed_target_ids=["2.1.1"],
            ) for finding_id in required_ids],
        ),
    }}, store=store)
    context = prepare_current_module_recheck(context, store=store)
    assert context.recheck.prepared.mode == "invoke_agent"
    # This is the exact serializable boundary restored before an Agent result.
    context = DeclarativeModuleRuntimeLaneContext.model_validate_json(context.model_dump_json())
    new_finding = _finding(context, new_impact, round_number=1)
    accepted = accept_current_module_recheck({"context": context, "result": {
        "status": "completed", "submission": ModuleReviewVerdictSubmission(
            coverage={"submodule_ids": ["2.1.1"]},
            verdicts=[ResolutionVerdict(
                finding_id=finding_id, verdict="resolved",
                reason="Checked the current subject against the original required correction.",
                evidence_refs=[context.recheck.prepared.subject_ref],
            ) for finding_id in required_ids], new_findings=[new_finding],
        ),
    }}, store=store)
    progress = ModuleReviewProgress.model_validate_json(
        (tmp_path / accepted.review.acceptance.progress_ref).read_text(),
    )
    assert progress.review_protocol_version == protocol
    runtime, *_ = _build_runtime(tmp_path)
    restored = accepted.model_copy(deep=True, update={"status": "review_resumed"})
    restored.review.acceptance = None
    restored.recheck.acceptance = None
    restored.recheck.prepared = restored.recheck.prepared.model_copy(update={
        "mode": "continue_existing", "progress": progress,
    })
    resumed = runtime.resume_recheck_lane(
        DeclarativeModuleRuntimeLaneContext.model_validate_json(restored.model_dump_json()),
    )
    assert resumed.review.acceptance == accepted.review.acceptance
    if new_impact == "blocking" or protocol == 2:
        assert accepted.review.acceptance.next_action == "continue_existing"
        assert progress.pending == [new_finding]
        assert len(progress.finding_refs) == 2
    else:
        assert accepted.review.acceptance.next_action == "completed"
        completion, artifacts = _completion(store, accepted)
        assert completion.resolved_finding_ids == required_ids
        assert completion.deferred_advisory_ids == [
            "M-2.1-initial-r0-advisory", "M-2.1-initial-r1-advisory",
        ]
        assert len(artifacts) == 3


@pytest.mark.parametrize("tamper", ["defer_blocking", "drop_advice", "resolve_advice", "old_protocol"])
def test_completion_loader_rejects_incorrect_advisory_closure(tmp_path, tamper):
    store, context = _start(tmp_path)
    context = _accept(store, context, ["advisory"])
    acceptance = context.review.acceptance
    assert acceptance.completion_ref is not None
    completion = json.loads((tmp_path / acceptance.completion_ref).read_text())
    if tamper == "defer_blocking":
        ref = acceptance.finding_refs[0]
        artifact = json.loads((tmp_path / ref).read_text())
        artifact["findings"][0]["impact"] = "blocking"
        store.write_json(ref, artifact)
    elif tamper == "drop_advice":
        completion["deferred_advisory_ids"] = []
    elif tamper == "resolve_advice":
        completion["resolved_finding_ids"] = completion["deferred_advisory_ids"]
    else:
        completion["review_protocol_version"] = 2
    store.write_json(acceptance.completion_ref, completion)
    with pytest.raises(ValueError):
        _completion(store, context)


def test_module_rework_stops_at_the_round_limit_with_findings_recorded_open(tmp_path):
    store, context = _start(tmp_path)
    context.reporting_state["max_module_recheck_rounds"] = 1
    context = _accept(store, context, ["blocking"])
    context = prepare_current_module_revision(context, store=store)
    required_ids = context.revision.prepared.required_finding_ids
    context = accept_current_module_revision({"context": context, "result": {
        "status": "completed",
        "submission": ModuleRevisionSubmission(
            module_id="2.1", base_revision=0, revision=1,
            submodule_narratives={"2.1.1": "### 2.1.1\n\n当前证据边界和修订内容。"},
            source_ids=[], unresolved_questions=[],
            revision_responses=[RevisionResponse(
                finding_id=finding_id, action="implemented",
                summary="已按实际证据明确说明修订内容和可以复核的边界。",
                changed_target_ids=["2.1.1"],
            ) for finding_id in required_ids],
        ),
    }}, store=store)
    context = prepare_current_module_recheck(context, store=store)
    accepted = accept_current_module_recheck({"context": context, "result": {
        "status": "completed", "submission": ModuleReviewVerdictSubmission(
            coverage={"submodule_ids": ["2.1.1"]},
            verdicts=[ResolutionVerdict(
                finding_id=finding_id, verdict="open",
                reason="The revised text still does not state the measured boundary.",
            ) for finding_id in required_ids], new_findings=[],
        ),
    }}, store=store)

    assert accepted.review.acceptance.next_action == "completed"
    completion, _ = _completion(store, accepted)
    assert completion.open_at_round_limit_ids == required_ids
    assert completion.resolved_finding_ids == []
