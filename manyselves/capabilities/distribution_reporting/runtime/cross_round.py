"""Capability-owned Cross owner round transitions.

This module contains the typed, deterministic part of one Cross reviewer
round.  Agent dispatch, Main exception handling, and artifact persistence are
ports supplied by the owning runtime; the transition itself does not know
about the Kernel, a reporting runner, Provider sessions, hashes, or CAS.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from .models.agentic import (
    CrossOwnerFindingSubmission,
    CrossReviewFinding,
    ResolutionVerdict,
    WorkflowDecisionSubmission,
)
from .models.review import (
    CrossOwnerRecheckAcceptance,
    CrossOwnerRoundProgress,
    MainExceptionDecisionAcceptance,
)


class CrossRoundError(ValueError):
    """Raised when a typed Cross round cannot be advanced."""


MainExceptionResolver = Callable[
    ...,
    Awaitable[WorkflowDecisionSubmission | MainExceptionDecisionAcceptance],
]
ImmutableArtifactWriter = Callable[[str, Any], str]


def _validate_new_finding_ids(findings: list[CrossReviewFinding]) -> None:
    ids = [finding.id for finding in findings]
    if len(ids) != len(set(ids)):
        raise CrossRoundError("cross findings contains duplicate ids")


def _decision_result(
    decision: WorkflowDecisionSubmission | MainExceptionDecisionAcceptance,
) -> WorkflowDecisionSubmission:
    if isinstance(decision, MainExceptionDecisionAcceptance):
        return decision.result
    return decision


async def advance_cross_owner_round(
    *,
    workflow_id: str,
    initial_input_ref: str,
    initial_result_ref: str,
    initial_result: CrossOwnerFindingSubmission,
    acceptance: CrossOwnerRecheckAcceptance,
    previous: CrossOwnerRoundProgress | None = None,
    main_decision: MainExceptionDecisionAcceptance | None = None,
    main_exception_resolver: MainExceptionResolver | None = None,
    write_immutable: ImmutableArtifactWriter | None = None,
) -> CrossOwnerRoundProgress:
    """Advance one accepted Cross-owner verdict into typed round progress.

    The transition is mechanically equivalent to the legacy
    ``advance_cross_owner_round`` algorithm.  ``main_exception_resolver`` is
    used only when an escalation has no already accepted Main decision.  When
    new findings are emitted, ``write_immutable`` must be supplied by the
    Capability persistence composition so the existing immutable artifact is
    retained without embedding filesystem behavior here.
    """

    owner_module_id = acceptance.owner_module_id
    verdict = acceptance.result
    lane = acceptance.lane
    _validate_new_finding_ids(verdict.new_findings)

    if previous is None:
        pending = {finding.id: finding for finding in acceptance.required_findings}
        resolved_ids: set[str] = set()
        finding_refs = [initial_result_ref]
        verdict_refs: list[str] = []
        all_findings = list(initial_result.findings)
        terminal_verdicts: dict[str, ResolutionVerdict] = {}
    else:
        pending = {finding.id: finding for finding in previous.pending}
        resolved_ids = set(previous.resolved_ids)
        finding_refs = list(previous.finding_refs)
        verdict_refs = list(previous.verdict_refs)
        all_findings = list(previous.findings)
        terminal_verdicts = {
            item.finding_id: item for item in previous.verdicts
        }

    verdict_refs.append(acceptance.result_ref)
    terminal_verdicts.update(
        {item.finding_id: item for item in verdict.verdicts}
    )

    escalated = [item for item in verdict.verdicts if item.verdict == "escalate"]
    main_accepts: set[str] = set()
    if escalated:
        if main_decision is not None:
            decision = main_decision.result
        else:
            if main_exception_resolver is None:
                raise CrossRoundError(
                    "escalated Cross findings require a Main exception resolver"
                )
            decision = _decision_result(
                await main_exception_resolver(
                    workflow_id=workflow_id,
                    scope="cross",
                    subject_refs=[lane.completion.subject.ref],
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=lane.responses,
                )
            )
        if decision.decision == "accept_dispute":
            main_accepts = set(decision.finding_ids)

    next_pending = {
        item.finding_id: pending[item.finding_id]
        for item in verdict.verdicts
        if item.verdict == "open"
        or (item.verdict == "escalate" and item.finding_id not in main_accepts)
    }
    resolved_ids.update(
        item.finding_id
        for item in verdict.verdicts
        if item.verdict == "resolved" or item.finding_id in main_accepts
    )

    for finding in verdict.new_findings:
        if finding.id in pending or finding.id in resolved_ids:
            raise CrossRoundError(
                f"new Cross owner finding reuses an existing id: {finding.id}"
            )
        next_pending[finding.id] = finding
        all_findings.append(finding)

    if verdict.new_findings:
        if write_immutable is None:
            raise CrossRoundError(
                "new Cross owner findings require the immutable artifact writer"
            )
        regression_ref = write_immutable(
            (
                f"Work/runs/{acceptance.run_id}/reviews/"
                f"cross-owner-regression-findings-r{acceptance.review_round}-"
                f"{owner_module_id}.json"
            ),
            CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=verdict.coverage,
                findings=verdict.new_findings,
                synthesis_inputs=[],
            ),
        )
        finding_refs.append(regression_ref)

    return CrossOwnerRoundProgress(
        run_id=acceptance.run_id,
        workflow_id=workflow_id,
        owner_module_id=owner_module_id,
        initial_input_ref=initial_input_ref,
        initial_result_ref=initial_result_ref,
        initial_result=initial_result,
        review_round=acceptance.review_round,
        next_review_round=acceptance.review_round + 1,
        next_owner_input_ref=acceptance.result_ref,
        next_action="revise" if next_pending else "completed",
        pending=list(next_pending.values()),
        resolved_ids=sorted(resolved_ids),
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        findings=all_findings,
        verdicts=list(terminal_verdicts.values()),
        lane=lane,
        verdict_ref=acceptance.result_ref,
        verdict=verdict,
    )


__all__ = ["CrossRoundError", "advance_cross_owner_round"]
