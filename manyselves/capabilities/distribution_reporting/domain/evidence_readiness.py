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
    CoverageMatrix,
    CoverageStatus,
    ReportingModel,
    ReportRequest,
)


class EvidenceReadinessDecision(ReportingModel):
    """Whether the explicit missing-evidence policy requires a pause."""

    should_block: bool
    missing_evidence: list[str]
    affected_modules: list[str]


class ReportingBlockedError(RuntimeError):
    def __init__(
        self,
        missing_evidence: list[str],
        affected_modules: list[str] | None = None,
    ):
        self.missing_evidence = missing_evidence
        self.affected_modules = affected_modules or []
        super().__init__(
            "reporting requires evidence confirmation: "
            + ", ".join(missing_evidence)
        )


class EvidenceReadinessPolicy:
    """Apply only the request's explicit ask/block/skip/draft policy."""

    @staticmethod
    def evaluate(
        request: ReportRequest,
        coverage: CoverageMatrix,
    ) -> EvidenceReadinessDecision:
        missing: list[str] = []
        affected: list[str] = []
        for module_id in request.target_modules:
            module_has_gap = False
            entry = coverage.entries[module_id]
            for submodule_id, submodule in entry.submodules.items():
                if submodule.status is CoverageStatus.READY:
                    continue
                module_has_gap = True
                missing.extend(
                    submodule.gaps
                    or [f"{submodule_id} 缺少可追溯客户证据"]
                )
            if module_has_gap:
                affected.append(module_id)
        missing = list(dict.fromkeys(missing))
        return EvidenceReadinessDecision(
            should_block=(
                bool(missing)
                and request.missing_evidence_policy in {"ask", "block"}
            ),
            missing_evidence=missing,
            affected_modules=affected,
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
    "EvidenceReadinessDecision",
    "EvidenceReadinessPolicy",
    "ReportingBlockedError",
    "apply_evidence_decision",
    "evaluate_evidence_readiness",
    "reprepare_input",
]
