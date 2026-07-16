"""Explicit application boundary for governed Skill improvement actions."""

from __future__ import annotations

from .governance import (
    EvaluationResult,
    ModuleId,
    SkillCandidate,
    SkillGovernanceStore,
    SkillVersion,
)


class SkillGovernanceService:
    """Expose candidate and approval actions without automatic self-publication."""

    def __init__(self, store: SkillGovernanceStore):
        self.store = store

    def propose(
        self,
        *,
        module_id: ModuleId,
        original: str,
        revised: str,
        reason: str,
        failure_type: str | None = None,
        sample_refs: list[str] | None = None,
    ) -> SkillCandidate:
        return self.store.create_candidate(
            module_id=module_id,
            original=original,
            revised=revised,
            reason=reason,
            failure_type=failure_type,
            sample_refs=sample_refs,
        )

    def evaluate(
        self,
        candidate_id: str,
        *,
        baseline: float,
        candidate_score: float,
        regressions: list[str],
        model: str,
        configuration: dict,
    ) -> EvaluationResult:
        return self.store.record_evaluation(
            candidate_id,
            baseline=baseline,
            candidate_score=candidate_score,
            regressions=regressions,
            model=model,
            configuration=configuration,
        )

    def approve(
        self,
        candidate_id: str,
        evaluation_id: str,
        *,
        user_confirmed: bool,
    ) -> SkillVersion:
        return self.store.publish(
            candidate_id,
            evaluation_id,
            confirmed=user_confirmed,
        )
