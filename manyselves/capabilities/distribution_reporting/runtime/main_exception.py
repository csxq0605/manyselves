"""Capability-owned Main exception preparation and acceptance.

This module contains the typed, file-backed boundary for the existing Main
exception decision.  It deliberately receives the already-composed
``ReportingStore`` and typed finding inputs; it does not invoke an Agent or
interpret a workflow route.
"""

from __future__ import annotations

from collections.abc import MutableMapping, Sequence
from typing import Any, Literal

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    WorkflowExceptionInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    MainExceptionDecisionAcceptance,
    MainExceptionDecisionPreparation,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


class MainExceptionDecisionError(ValueError):
    """A deterministic failure while preparing or accepting a Main decision."""

    def __init__(self, message: str, *, keep_agents_alive: bool = True) -> None:
        super().__init__(message)
        self.keep_agents_alive = keep_agents_alive


ExceptionTrigger = Literal["reviewer_escalation", "author_response"]


def _main_exception_ids(
    *,
    trigger: str,
    verdicts: Sequence[ResolutionVerdict],
    responses: Sequence[RevisionResponse],
) -> set[str]:
    return (
        {
            verdict.finding_id
            for verdict in verdicts
            if verdict.verdict == "escalate"
        }
        if trigger == "reviewer_escalation"
        else {
            response.finding_id
            for response in responses
            if response.action in {"disputed", "needs_input"}
        }
    )


def _decision_ref(
    *,
    run_id: str,
    scope: str,
    trigger: str,
    exception_ids: set[str],
) -> tuple[str, str]:
    suffix = "-".join(sorted(exception_ids))
    root = f"Work/runs/{run_id}/exceptions/{scope}-{trigger}"
    return f"{root}-{suffix}.json", f"{root}-decision-{suffix}.json"


def prepare_main_exception_decision(
    *,
    store: ReportingStore,
    run_id: str,
    workflow_id: str,
    scope: str,
    subject_refs: Sequence[str],
    finding_refs: Sequence[str],
    verdicts: Sequence[ResolutionVerdict],
    responses: Sequence[RevisionResponse],
    trigger: ExceptionTrigger = "reviewer_escalation",
) -> MainExceptionDecisionPreparation:
    """Prepare or recover the existing explicit Main exception decision."""

    exception_ids = _main_exception_ids(
        trigger=trigger,
        verdicts=verdicts,
        responses=responses,
    )
    if not exception_ids:
        raise MainExceptionDecisionError(
            "Main exception decision requires explicit finding ids"
        )

    exception_input = WorkflowExceptionInput(
        run_id=run_id,
        scope=scope,
        trigger=trigger,
        finding_ids=sorted(exception_ids),
        subject_refs=list(subject_refs),
        finding_refs=list(finding_refs),
        verdicts=(
            [verdict for verdict in verdicts if verdict.verdict == "escalate"]
            if trigger == "reviewer_escalation"
            else []
        ),
        revision_responses=[
            response for response in responses if response.finding_id in exception_ids
        ],
    )
    input_ref, decision_ref = _decision_ref(
        run_id=run_id,
        scope=scope,
        trigger=trigger,
        exception_ids=exception_ids,
    )
    store.write_json(input_ref, exception_input.model_dump(mode="json"))
    envelope = TaskEnvelope(
        task_id=f"{scope}-review-exception",
        run_id=run_id,
        agent_id="main-agent",
        objective=(
            "只处理明确进入例外节点的 finding：核对当前正文、作者响应与可用审查理由，"
            "决定退回作者、接受争议、请求用户或停止不完整。"
        ),
        input_refs=[input_ref, *subject_refs, *finding_refs],
        constraints=[
            "不得重新执行整轮专业审查",
            "不得改写 finding、author response 或 reviewer verdict",
            "finding_ids 必须精确覆盖全部且仅覆盖本次 exception finding",
        ],
        allowed_outputs=["workflow_decision_submission"],
        input_contract_kind="workflow_exception_input",
        input_contract_ref=input_ref,
    )
    decision_path = store.workspace / decision_ref
    existing_result: WorkflowDecisionSubmission | None = None
    if decision_path.is_file():
        try:
            existing_result = WorkflowDecisionSubmission.model_validate_json(
                decision_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise MainExceptionDecisionError(
                "Persisted Main exception decision is invalid"
            ) from exc
        if set(existing_result.finding_ids) != exception_ids:
            raise MainExceptionDecisionError(
                "Persisted Main exception decision does not cover the current findings"
            )
    return MainExceptionDecisionPreparation(
        mode="continue_existing" if existing_result is not None else "invoke_agent",
        run_id=run_id,
        workflow_id=workflow_id,
        scope=scope,
        trigger=trigger,
        exception_ids=sorted(exception_ids),
        input_ref=input_ref,
        decision_ref=decision_ref,
        envelope=envelope,
        existing_result=existing_result,
    )


def accept_main_exception_decision(
    *,
    store: ReportingStore,
    preparation: MainExceptionDecisionPreparation,
    result: WorkflowDecisionSubmission | None,
    state: MutableMapping[str, Any] | None = None,
    raise_for_terminal_decisions: bool = True,
) -> MainExceptionDecisionAcceptance:
    """Persist and accept one typed Main decision with existing semantics."""

    accepted = preparation.existing_result if result is None else result
    if not isinstance(accepted, WorkflowDecisionSubmission):
        raise MainExceptionDecisionError("Main returned the wrong exception-decision type")
    if set(accepted.finding_ids) != set(preparation.exception_ids):
        raise MainExceptionDecisionError(
            "Main exception decision must cover exactly the exception findings"
        )
    store.write_json(preparation.decision_ref, accepted.model_dump(mode="json"))
    if state is not None:
        exception_refs = state.setdefault("review_exception_refs", [])
        if preparation.decision_ref not in exception_refs:
            exception_refs.append(preparation.decision_ref)
    if raise_for_terminal_decisions and accepted.decision == "request_user":
        raise MainExceptionDecisionError(accepted.rationale)
    if raise_for_terminal_decisions and accepted.decision == "stop_incomplete":
        raise MainExceptionDecisionError(
            accepted.rationale,
            keep_agents_alive=False,
        )
    return MainExceptionDecisionAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        scope=preparation.scope,
        trigger=preparation.trigger,
        decision_ref=preparation.decision_ref,
        result=accepted,
    )


__all__ = [
    "ExceptionTrigger",
    "MainExceptionDecisionError",
    "accept_main_exception_decision",
    "prepare_main_exception_decision",
]
