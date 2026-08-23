"""Capability-owned Cross-owner recheck preparation and acceptance.

The helper keeps the typed recheck boundary independent from the legacy
workflow runner.  Callers provide the frozen owner input, completed lane,
initial acceptance, and persistence ports explicitly.  The reviewer session
key is carried from the initial acceptance so a recheck remains in the same
Cross-owner session.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping

from .models.agentic import (
    CrossOwnerVerdictSubmission,
    CrossReviewFinding,
    TaskEnvelope,
)
from .models.inputs import CrossOwnerInput
from .models.review import (
    CrossOwnerInitialReviewAcceptance,
    CrossOwnerRecheckAcceptance,
    CrossOwnerRecheckPreparation,
    _CrossOwnerLaneResult,
)


class CrossRecheckError(ValueError):
    """Raised when a typed Cross recheck boundary cannot be completed."""


ResultReader = Callable[[str], object | None]
ImmutableResultWriter = Callable[[str, CrossOwnerVerdictSubmission], str]


def _result_ref(*, run_id: str, owner_module_id: str, review_round: int) -> str:
    return (
        f"Work/runs/{run_id}/reviews/"
        f"cross-owner-verdicts-r{review_round}-{owner_module_id}.json"
    )


def _coerce_verdict(value: object) -> CrossOwnerVerdictSubmission:
    if isinstance(value, CrossOwnerVerdictSubmission):
        return value
    if isinstance(value, Mapping):
        return CrossOwnerVerdictSubmission.model_validate(value)
    raise CrossRecheckError("persisted Cross owner recheck result is not typed")


def _validate_verdict(
    verdict: CrossOwnerVerdictSubmission,
    *,
    owner_module_id: str,
    required_findings: list[CrossReviewFinding],
) -> None:
    expected_ids = {finding.id for finding in required_findings}
    actual_ids = {item.finding_id for item in verdict.verdicts}
    if verdict.owner_module_id != owner_module_id:
        raise CrossRecheckError(
            f"Cross owner recheck result belongs to another owner: {owner_module_id}"
        )
    if actual_ids != expected_ids:
        raise CrossRecheckError(
            f"Cross owner {owner_module_id} verdict coverage mismatch"
        )


def _validate_frozen_input(
    *,
    initial: CrossOwnerInitialReviewAcceptance,
    lane: _CrossOwnerLaneResult,
    frozen_owner_input: CrossOwnerInput,
    review_round: int,
    required_findings: list[CrossReviewFinding],
) -> None:
    owner_module_id = initial.owner_module_id
    if (
        frozen_owner_input.run_id != initial.run_id
        or frozen_owner_input.owner_module_id != owner_module_id
        or frozen_owner_input.phase != "recheck"
        or frozen_owner_input.review_round != review_round
    ):
        raise CrossRecheckError(
            f"Cross owner recheck input identity mismatch: {owner_module_id}"
        )
    if lane.module.module_id != owner_module_id:
        raise CrossRecheckError(
            f"Cross owner recheck lane belongs to another owner: {owner_module_id}"
        )
    subject = lane.completion.subject
    subject_ref = subject.ref if hasattr(subject, "ref") else subject
    if frozen_owner_input.owner_subject_ref != subject_ref:
        raise CrossRecheckError(
            f"Cross owner recheck input does not bind recovered lane: {owner_module_id}"
        )
    if {
        finding.id for finding in frozen_owner_input.required_findings
    } != {finding.id for finding in required_findings}:
        raise CrossRecheckError(
            f"Cross owner recheck input does not cover its required findings: {owner_module_id}"
        )


def _build_recheck_envelope(
    *,
    run_id: str,
    owner_module_id: str,
    review_round: int,
    initial_result_ref: str,
    owner_input_ref: str,
    frozen_owner_input: CrossOwnerInput,
) -> TaskEnvelope:
    return TaskEnvelope(
        task_id=f"cross-owner-{owner_module_id}-r{review_round}-recheck",
        run_id=run_id,
        agent_id="cross-module-reviewer",
        objective=(
            f"由 Cross owner {owner_module_id} 原会话复审本轮 owner 修订，"
            "逐项给出原 finding verdict；related 模块仍只读。"
        ),
        input_refs=[owner_input_ref],
        constraints=[
            f"唯一 Cross owner 写作范围是模块 {owner_module_id}",
            "related_module_views 是紧凑只读关系视图，不得修改或创建其他模块 finding",
            (
                "recheck 只提交 cross_owner_verdict_submission，逐项覆盖全部 "
                "required_findings；new_findings 只允许当前修订引入的真实回归"
            ),
            "coverage 仅证明当前 owner 检查过六个维度，不代表 approved",
        ],
        allowed_outputs=["cross_owner_verdict_submission"],
        allowed_tools=["submit_result"],
        revision=review_round,
        prior_result_ref=initial_result_ref,
        artifact_delivery_modes={owner_input_ref: "inline"},
        target_submodule_ids=list(frozen_owner_input.owner_scope_submodule_ids),
        input_contract_kind="cross_owner_input",
        input_contract_ref=owner_input_ref,
        inline_context="",
    )


def prepare_cross_owner_recheck(
    *,
    workflow_id: str,
    initial: CrossOwnerInitialReviewAcceptance,
    lane: _CrossOwnerLaneResult,
    frozen_owner_input: CrossOwnerInput,
    owner_input_ref: str,
    review_round: int,
    required_findings: list[CrossReviewFinding],
    read_result: ResultReader | None = None,
) -> CrossOwnerRecheckPreparation:
    """Prepare a fresh or persisted Cross-owner reviewer recheck."""

    _validate_frozen_input(
        initial=initial,
        lane=lane,
        frozen_owner_input=frozen_owner_input,
        review_round=review_round,
        required_findings=required_findings,
    )
    run_id = initial.run_id
    owner_module_id = initial.owner_module_id
    verdict_ref = _result_ref(
        run_id=run_id,
        owner_module_id=owner_module_id,
        review_round=review_round,
    )
    if read_result is not None:
        persisted = read_result(verdict_ref)
        if persisted is not None:
            existing = _coerce_verdict(persisted)
            _validate_verdict(
                existing,
                owner_module_id=owner_module_id,
                required_findings=required_findings,
            )
            return CrossOwnerRecheckPreparation(
                mode="continue_existing",
                run_id=run_id,
                workflow_id=workflow_id,
                owner_module_id=owner_module_id,
                review_round=review_round,
                reviewer_session_key=initial.reviewer_session_key,
                initial_result_ref=initial.result_ref,
                initial_result=initial.result,
                lane=lane,
                owner_input_ref=owner_input_ref,
                required_findings=required_findings,
                existing_result_ref=verdict_ref,
                existing_result=existing,
            )

    return CrossOwnerRecheckPreparation(
        mode="invoke_agent",
        run_id=run_id,
        workflow_id=workflow_id,
        owner_module_id=owner_module_id,
        review_round=review_round,
        reviewer_session_key=initial.reviewer_session_key,
        initial_result_ref=initial.result_ref,
        initial_result=initial.result,
        lane=lane,
        owner_input_ref=owner_input_ref,
        required_findings=required_findings,
        envelope=_build_recheck_envelope(
            run_id=run_id,
            owner_module_id=owner_module_id,
            review_round=review_round,
            initial_result_ref=initial.result_ref,
            owner_input_ref=owner_input_ref,
            frozen_owner_input=frozen_owner_input,
        ),
    )


def accept_cross_owner_recheck(
    *,
    preparation: CrossOwnerRecheckPreparation,
    result: CrossOwnerVerdictSubmission | None,
    write_immutable: ImmutableResultWriter | None = None,
) -> CrossOwnerRecheckAcceptance:
    """Accept a fresh or already persisted typed Cross-owner verdict."""

    if preparation.mode == "continue_existing":
        if result is not None:
            raise CrossRecheckError(
                "cannot accept a Cross owner recheck after persisted continuation"
            )
        if (
            preparation.existing_result is None
            or preparation.existing_result_ref is None
        ):
            raise CrossRecheckError("Cross owner continuation has no persisted result")
        accepted = preparation.existing_result
        result_ref = preparation.existing_result_ref
    else:
        if result is None:
            raise CrossRecheckError("Cross owner recheck Agent result is missing")
        if not isinstance(result, CrossOwnerVerdictSubmission):
            raise CrossRecheckError("Cross owner recheck Agent result is not typed")
        accepted = result
        _validate_verdict(
            accepted,
            owner_module_id=preparation.owner_module_id,
            required_findings=preparation.required_findings,
        )
        if write_immutable is None:
            raise CrossRecheckError(
                "fresh Cross owner recheck requires the immutable result writer"
            )
        result_ref = write_immutable(
            _result_ref(
                run_id=preparation.run_id,
                owner_module_id=preparation.owner_module_id,
                review_round=preparation.review_round,
            ),
            accepted,
        )

    return CrossOwnerRecheckAcceptance(
        run_id=preparation.run_id,
        workflow_id=preparation.workflow_id,
        owner_module_id=preparation.owner_module_id,
        review_round=preparation.review_round,
        reviewer_session_key=preparation.reviewer_session_key,
        initial_result_ref=preparation.initial_result_ref,
        initial_result=preparation.initial_result,
        lane=preparation.lane,
        owner_input_ref=preparation.owner_input_ref,
        required_findings=preparation.required_findings,
        result_ref=result_ref,
        result=accepted,
    )


__all__ = [
    "CrossRecheckError",
    "accept_cross_owner_recheck",
    "prepare_cross_owner_recheck",
]
