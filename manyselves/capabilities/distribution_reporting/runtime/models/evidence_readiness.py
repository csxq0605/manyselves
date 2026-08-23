"""Typed values exchanged by the Distribution evidence-readiness workflow."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .preparation import PreparationContext
from .reporting import (
    CoverageMatrix,
    EvidenceDecisionAction,
    ReportingModel,
    ReportRequest,
    UserSupplement,
)

EvidenceReadinessStatus = Literal[
    "ready",
    "waiting",
    "blocked",
    "draft",
    "skipped",
    "preparing",
    "stopped",
]
EvidenceReadinessRoute = Literal[
    "continue",
    "ask",
    "block",
    "prepare",
    "stop",
]


class EvidenceReadinessInput(ReportingModel):
    """Capability input after deterministic preparation has evaluated coverage."""

    run_id: str = Field(min_length=1)
    request: ReportRequest
    coverage_matrix: CoverageMatrix
    preparation_context: PreparationContext | None = None


class EvidenceDecisionInput(ReportingModel):
    """The only user input accepted by the readiness Interaction."""

    action: EvidenceDecisionAction
    supplements: list[UserSupplement] = Field(default_factory=list)
    decision_note: str | None = None


class EvidenceReadinessState(ReportingModel):
    """Serializable readiness result carried through generic Actions."""

    run_id: str = Field(min_length=1)
    request: ReportRequest
    coverage_matrix: CoverageMatrix
    status: EvidenceReadinessStatus
    route: EvidenceReadinessRoute
    missing_evidence: list[str] = Field(default_factory=list)
    affected_modules: list[str] = Field(default_factory=list)
    selected_action: EvidenceDecisionAction | None = None
    supplements: list[UserSupplement] = Field(default_factory=list)
    decision_note: str | None = None
    preparation_context: PreparationContext


class EvidenceDecisionApplication(ReportingModel):
    """Typed join of current readiness state and one submitted decision."""

    state: EvidenceReadinessState
    input: EvidenceDecisionInput


__all__ = [
    "EvidenceDecisionApplication",
    "EvidenceDecisionInput",
    "EvidenceReadinessInput",
    "EvidenceReadinessRoute",
    "EvidenceReadinessState",
    "EvidenceReadinessStatus",
]
