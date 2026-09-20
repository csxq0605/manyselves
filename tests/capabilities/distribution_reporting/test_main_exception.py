from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
    CrossOwnerRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.main_exception import (
    MainExceptionDecisionError,
    accept_main_exception_decision,
    prepare_main_exception_decision,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    WorkflowDecisionSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
    DeclarativeCrossOwnerRuntimeContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    WorkflowExceptionInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    CrossOwnerRevisionAcceptance,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _author_response(finding_id: str) -> RevisionResponse:
    return RevisionResponse(
        finding_id=finding_id,
        action="disputed",
        summary="作者明确说明该 finding 不应改变当前正文范围。",
    )


def _author_preparation(tmp_path: Path):
    store = ReportingStore(tmp_path)
    return store, prepare_main_exception_decision(
        store=store,
        run_id="run-main-author",
        workflow_id="distribution-cross-owner-2.1-pipeline",
        scope="cross",
        subject_refs=["Work/runs/run-main-author/modules/2.1-r1.json"],
        finding_refs=["Work/runs/run-main-author/reviews/cross-findings-r0-2.1.json"],
        verdicts=[],
        responses=[_author_response("X-2")],
        trigger="author_response",
    )


def test_prepare_main_exception_preserves_typed_input_envelope_and_resume(
    tmp_path: Path,
) -> None:
    store, preparation = _author_preparation(tmp_path)

    assert preparation.mode == "invoke_agent"
    assert preparation.exception_ids == ["X-2"]
    assert preparation.input_ref == (
        "Work/runs/run-main-author/exceptions/cross-author_response-X-2.json"
    )
    assert preparation.decision_ref == (
        "Work/runs/run-main-author/exceptions/"
        "cross-author_response-decision-X-2.json"
    )
    persisted_input = WorkflowExceptionInput.model_validate_json(
        (tmp_path / preparation.input_ref).read_text(encoding="utf-8")
    )
    assert persisted_input.trigger == "author_response"
    assert persisted_input.finding_ids == ["X-2"]
    assert [item.finding_id for item in persisted_input.revision_responses] == ["X-2"]
    assert preparation.envelope.input_refs == [preparation.input_ref, *[
        "Work/runs/run-main-author/modules/2.1-r1.json",
        "Work/runs/run-main-author/reviews/cross-findings-r0-2.1.json",
    ]]
    assert preparation.envelope.input_contract_kind == "workflow_exception_input"
    assert preparation.envelope.input_contract_ref == preparation.input_ref
    assert preparation.envelope.allowed_outputs == ["workflow_decision_submission"]

    persisted = WorkflowDecisionSubmission(
        decision="accept_dispute",
        rationale="Main 接受作者对该 finding 的明确争议。",
        finding_ids=["X-2"],
    )
    store.write_json(preparation.decision_ref, persisted.model_dump(mode="json"))

    resumed = prepare_main_exception_decision(
        store=store,
        run_id="run-main-author",
        workflow_id="distribution-cross-owner-2.1-pipeline",
        scope="cross",
        subject_refs=["Work/runs/run-main-author/modules/2.1-r1.json"],
        finding_refs=["Work/runs/run-main-author/reviews/cross-findings-r0-2.1.json"],
        verdicts=[],
        responses=[_author_response("X-2")],
        trigger="author_response",
    )

    assert resumed.mode == "continue_existing"
    assert resumed.existing_result == persisted


def test_accept_main_exception_writes_decision_and_preserves_state_ref(
    tmp_path: Path,
) -> None:
    store, preparation = _author_preparation(tmp_path)
    state: dict[str, object] = {}
    accepted = accept_main_exception_decision(
        store=store,
        preparation=preparation,
        result=WorkflowDecisionSubmission(
            decision="return_to_author",
            rationale="作者需要在同一 finding 上补充可验证的修订。",
            finding_ids=["X-2"],
        ),
        state=state,
        raise_for_terminal_decisions=False,
    )

    assert accepted.result.decision == "return_to_author"
    assert state == {"review_exception_refs": [preparation.decision_ref]}
    assert WorkflowDecisionSubmission.model_validate_json(
        (tmp_path / preparation.decision_ref).read_text(encoding="utf-8")
    ) == accepted.result


@pytest.mark.parametrize(
    ("decision", "keep_agents_alive"),
    [("request_user", True), ("stop_incomplete", False)],
)
def test_terminal_main_exception_decisions_keep_existing_error_semantics(
    tmp_path: Path,
    decision: str,
    keep_agents_alive: bool,
) -> None:
    store, preparation = _author_preparation(tmp_path)
    submission = WorkflowDecisionSubmission(
        decision=decision,
        rationale="当前例外需要在既有流程边界等待后续决定。",
        finding_ids=["X-2"],
    )

    with pytest.raises(MainExceptionDecisionError) as raised:
        accept_main_exception_decision(
            store=store,
            preparation=preparation,
            result=submission,
            raise_for_terminal_decisions=True,
        )

    assert raised.value.keep_agents_alive is keep_agents_alive
    assert submission.decision in {"request_user", "stop_incomplete"}


def test_prepare_reviewer_escalation_keeps_exact_verdict_and_response_contract(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    preparation = prepare_main_exception_decision(
        store=store,
        run_id="run-main-reviewer",
        workflow_id="distribution-cross-owner-2.1-pipeline",
        scope="cross",
        subject_refs=["Work/runs/run-main-reviewer/lanes/cross-r1/2.1/completion.json"],
        finding_refs=["Work/runs/run-main-reviewer/reviews/cross-verdict-r1-2.1.json"],
        verdicts=[
            ResolutionVerdict(
                finding_id="X-1",
                verdict="escalate",
                reason="审查者与作者对该跨模块结论存在无法在本轮消解的分歧。",
                evidence_refs=["Work/runs/run-main-reviewer/modules/2.1-r1.json"],
            )
        ],
        responses=[
            RevisionResponse(
                finding_id="X-1",
                action="implemented",
                summary="作者已经在目标小节中完成对应关系修订并补充了验证说明。",
                changed_target_ids=["2.1.1"],
            )
        ],
        trigger="reviewer_escalation",
    )

    assert preparation.mode == "invoke_agent"
    assert preparation.exception_ids == ["X-1"]
    assert preparation.input_ref.endswith("cross-reviewer_escalation-X-1.json")
    persisted_input = WorkflowExceptionInput.model_validate_json(
        (tmp_path / preparation.input_ref).read_text(encoding="utf-8")
    )
    assert [item.finding_id for item in persisted_input.verdicts] == ["X-1"]
    assert [item.finding_id for item in persisted_input.revision_responses] == ["X-1"]


def test_accept_main_exception_rejects_mismatched_finding_ids(
    tmp_path: Path,
) -> None:
    store, preparation = _author_preparation(tmp_path)

    with pytest.raises(MainExceptionDecisionError, match="exactly"):
        accept_main_exception_decision(
            store=store,
            preparation=preparation,
            result=WorkflowDecisionSubmission(
                decision="accept_dispute",
                rationale="该决定没有覆盖准备阶段声明的 finding。",
                finding_ids=["different-finding"],
            ),
            raise_for_terminal_decisions=False,
        )


def test_cross_runtime_routes_author_exception_through_generic_waiting_decision(
    tmp_path: Path,
) -> None:
    finding = CrossReviewFinding(
        id="X-2",
        owner_module_id="2.1",
        target_submodule_ids=["2.1.1"],
        related_module_ids=["2.2"],
        category="dependencies",
        impact="blocking",
        observation=(
            "当前模块对跨模块依赖关系、影响路径与责任边界的描述不足，"
            "关联模块无法据此完成联合验证。"
        ),
        evidence_refs=["Work/runs/run-main-runtime/modules/2.1-r0.json"],
        required_change=(
            "补充跨模块依赖关系、影响路径、责任边界和关联模块能够直接复核的"
            "联合验证步骤。"
        ),
        reviewer_checks=["确认关系机制和联合验证步骤已经写入目标小节。"],
    )
    current = ModuleSubmission(
        module_id="2.1",
        submodule_narratives={
            submodule_id: f"原始内容 {submodule_id}。"
            for submodule_id in REPORT_TAXONOMY["2.1"].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    revised = current.model_copy(
        update={
            "revision": 1,
            "revision_responses": [_author_response(finding.id)],
        }
    )
    context = DeclarativeCrossOwnerRuntimeContext(
        owner_module_id="2.1",
        status="revision_accepted",
        revision_acceptance=CrossOwnerRevisionAcceptance(
            run_id="run-main-runtime",
            workflow_id="distribution-cross-owner-2.1-pipeline",
            owner_module_id="2.1",
            review_round=1,
            owner_input_ref=(
                "Work/runs/run-main-runtime/reviews/cross-owner-input-r0-2.1.json"
            ),
            current=current,
            findings=[finding],
            finding_refs=[
                "Work/runs/run-main-runtime/reviews/cross-owner-findings-r0-2.1.json"
            ],
            revised=revised,
            candidate_ref="Work/runs/run-main-runtime/modules/2.1-r1.json",
        ),
    )
    runtime = CrossOwnerRuntime(tmp_path)

    prepared = runtime.prepare_author_exception(context)
    assert prepared.status == "author_exception_ready"
    assert runtime.main_exception_requires_agent(prepared)
    assert prepared.main_preparation is not None
    accepted = runtime.accept_main_exception(
        {
            "context": prepared,
            "result": WorkflowDecisionSubmission(
                decision="request_user",
                rationale="该争议需要用户在同一个 Run 中明确是否退回作者。",
                finding_ids=[finding.id],
            ),
        }
    )
    assert runtime.main_exception_requests_user(accepted)
    resumed = runtime.apply_main_exception_user_input(
        {
            "context": accepted,
            "input": {
                "decision": "return_to_author",
                "rationale": "用户要求作者在同一 finding 上补充修订。",
            },
        }
    )

    assert runtime.author_exception_returns_to_author(resumed)
    assert resumed.review_exception_refs == [
        prepared.main_preparation.decision_ref
    ]
