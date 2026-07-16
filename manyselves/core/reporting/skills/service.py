"""Identity-scoped application services for Skill evolution."""

from __future__ import annotations

from pathlib import Path

from .governance import (
    EvaluationResult,
    FeedbackRecord,
    ModuleId,
    SkillCandidate,
    SkillGovernanceStore,
    SkillVersion,
)


class _SkillEvolutionService:
    def __init__(self, store: SkillGovernanceStore):
        self.store = store

    def record_feedback(
        self,
        *,
        skill_id: str,
        module_id: ModuleId,
        feedback: str,
        explicit_promotion_requested: bool,
        report_version_id: str,
        artifact_refs: list[str] | None = None,
    ) -> FeedbackRecord:
        return self.store.record_feedback(
            skill_id=skill_id,
            module_id=module_id,
            feedback=feedback,
            explicit_promotion_requested=explicit_promotion_requested,
            report_version_id=report_version_id,
            artifact_refs=artifact_refs,
        )

    def propose(
        self,
        feedback_id: str,
        *,
        title: str,
        submodules: list[str],
        proposed_content: str,
        reason: str,
        sample_refs: list[str] | None = None,
    ) -> SkillCandidate:
        return self.store.create_candidate(
            feedback_id,
            title=title,
            submodules=submodules,
            proposed_content=proposed_content,
            reason=reason,
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
        configuration: dict | None = None,
    ) -> EvaluationResult:
        return self.store.record_evaluation(
            candidate_id,
            baseline=baseline,
            candidate_score=candidate_score,
            regressions=regressions,
            model=model,
            configuration=configuration,
        )

    def publish(
        self, candidate_id: str, evaluation_id: str, *, user_confirmed: bool
    ) -> SkillVersion:
        return self.store.publish(candidate_id, evaluation_id, confirmed=user_confirmed)


class ProjectSkillEvolutionService(_SkillEvolutionService):
    """Project-local entry point owned by the project Main identity."""

    def __init__(self, workspace: Path):
        super().__init__(
            SkillGovernanceStore(Path(workspace) / "Capabilities/skills", scope="project")
        )


class ProductSkillEvolutionService(_SkillEvolutionService):
    """Product-wide entry point restricted to Product Skill Maintainer."""

    def __init__(self, product_root: Path, *, actor_id: str):
        if actor_id != "product-skill-maintainer":
            raise ValueError("product publication requires product-skill-maintainer identity")
        super().__init__(
            SkillGovernanceStore(Path(product_root) / "ProductCapabilities/skills", scope="product")
        )
