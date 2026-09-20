"""Capability Tool implementations for the file-defined readiness workflow."""

from __future__ import annotations

from typing import Any, cast

from manyselves.capabilities.distribution_reporting.domain.evidence_readiness import (
    apply_evidence_decision,
    evaluate_evidence_readiness,
    reprepare_input,
)
from manyselves.capabilities.distribution_reporting.runtime.models.evidence_readiness import (
    EvidenceDecisionApplication,
    EvidenceReadinessInput,
    EvidenceReadinessState,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    PreparationContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CoverageMatrix,
)


def evaluate_readiness(value: PreparationContext) -> EvidenceReadinessState:
    return evaluate_evidence_readiness(
        EvidenceReadinessInput(
            run_id=value.run_id,
            request=value.request,
            coverage_matrix=cast(CoverageMatrix, value.coverage_matrix),
            preparation_context=value,
        )
    )


def route_readiness(value: EvidenceReadinessState) -> str:
    return value.route


def apply_decision(value: EvidenceDecisionApplication) -> EvidenceReadinessState:
    return apply_evidence_decision(value.state, value.input)


def prepare_supplement(value: EvidenceReadinessState) -> PreparationContext:
    return reprepare_input(value)


def build_evidence_readiness_tool_implementations() -> dict[str, Any]:
    """Bind the four declared readiness Tool IDs.

    Supplement preparation is deliberately only a typed projection here. The
    file-defined workflow owns the subsequent preparation subworkflow.
    """

    return {
        "evaluate-evidence-readiness": evaluate_readiness,
        "route-evidence-readiness": route_readiness,
        "apply-evidence-decision": apply_decision,
        "prepare-evidence-supplement": prepare_supplement,
    }


__all__ = [
    "apply_decision",
    "build_evidence_readiness_tool_implementations",
    "evaluate_readiness",
    "prepare_supplement",
    "route_readiness",
]
