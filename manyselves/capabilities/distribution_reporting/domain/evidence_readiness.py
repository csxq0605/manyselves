"""Evidence readiness policy owned by the Distribution Reporting Capability."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.runtime.models.evidence_readiness import (
    EvidenceDecisionInput,
    EvidenceReadinessInput,
    EvidenceReadinessState,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    PreparationContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CoverageStatus,
    ReportRequest,
)


def _missing_evidence(
    request: ReportRequest,
    context: EvidenceReadinessInput,
) -> tuple[list[str], list[str]]:
    missing: list[str] = []
    affected: list[str] = []
    requested = set(request.target_modules)
    for module_id, entry in context.coverage_matrix.entries.items():
        if module_id not in requested or entry.status is CoverageStatus.READY:
            continue
        affected.append(module_id)
        missing.extend(entry.gaps)
        for submodule in entry.submodules.values():
            if submodule.status is not CoverageStatus.READY:
                missing.extend(submodule.gaps)
    return list(dict.fromkeys(missing)), list(dict.fromkeys(affected))


def evaluate_evidence_readiness(
    context: EvidenceReadinessInput,
) -> EvidenceReadinessState:
    """Map coverage and the request policy to a generic workflow route."""

    missing, affected = _missing_evidence(context.request, context)
    if not missing:
        status = "ready"
        route = "continue"
    elif context.request.missing_evidence_policy == "ask":
        status = "waiting"
        route = "ask"
    elif context.request.missing_evidence_policy == "block":
        status = "blocked"
        route = "block"
    elif context.request.missing_evidence_policy == "draft":
        status = "draft"
        route = "continue"
    else:
        status = "skipped"
        route = "continue"
    preparation_context = context.preparation_context or PreparationContext(
        run_id=context.run_id,
        request=context.request,
        coverage_matrix=context.coverage_matrix,
    )
    return EvidenceReadinessState(
        run_id=context.run_id,
        request=context.request,
        coverage_matrix=context.coverage_matrix,
        status=status,
        route=route,
        missing_evidence=missing,
        affected_modules=affected,
        preparation_context=preparation_context,
    )


def apply_evidence_decision(
    state: EvidenceReadinessState,
    decision: EvidenceDecisionInput,
) -> EvidenceReadinessState:
    """Apply a submitted decision without owning persistence or orchestration."""

    if decision.action == "supplement":
        supplements = [*state.request.user_supplements, *decision.supplements]
        request = state.request.model_copy(update={"user_supplements": supplements})
        preparation_context = state.preparation_context.model_copy(
            update={"request": request, "resume": False}
        )
        return state.model_copy(
            update={
                "request": request,
                "preparation_context": preparation_context,
                "status": "preparing",
                "route": "prepare",
                "selected_action": decision.action,
                "supplements": decision.supplements,
                "decision_note": decision.decision_note,
            }
        )
    if decision.action == "draft":
        return state.model_copy(
            update={
                "status": "draft",
                "route": "continue",
                "selected_action": decision.action,
                "supplements": decision.supplements,
                "decision_note": decision.decision_note,
            }
        )
    if decision.action == "skip":
        return state.model_copy(
            update={
                "status": "skipped",
                "route": "continue",
                "selected_action": decision.action,
                "supplements": decision.supplements,
                "decision_note": decision.decision_note,
            }
        )
    return state.model_copy(
        update={
            "status": "stopped",
            "route": "stop",
            "selected_action": decision.action,
            "supplements": decision.supplements,
            "decision_note": decision.decision_note,
        }
    )


def reprepare_input(state: EvidenceReadinessState) -> PreparationContext:
    """Return typed same-run input to the Capability preparation boundary.

    Preparation remains a separate Capability Tool/subworkflow. This pure
    projection carries the updated request and leaves coverage to that
    preparation implementation; it does not invent evidence or snapshots.
    """

    return state.preparation_context


__all__ = [
    "apply_evidence_decision",
    "evaluate_evidence_readiness",
    "reprepare_input",
]
