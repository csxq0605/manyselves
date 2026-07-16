"""Identity-scoped tools for explicitly requested Skill evolution."""

import uuid
from pathlib import Path
from typing import Any, Literal

from ...config.schema import AgentDefaults
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..reporting.skills.service import (
    ProductSkillEvolutionService,
    ProjectSkillEvolutionService,
    _SkillEvolutionService,
)
from .registry import Tool


class _SkillEvolutionTool(Tool):
    def __init__(self, service: _SkillEvolutionService):
        self.service = service

    async def __call__(
        self,
        action: Literal["record_feedback", "propose", "evaluate", "publish", "rollback"],
        skill_id: str | None = None,
        module_id: str | None = None,
        feedback: str | None = None,
        report_version_id: str | None = None,
        explicit_promotion_requested: bool = False,
        feedback_id: str | None = None,
        title: str | None = None,
        submodules: list[str] | None = None,
        proposed_content: str | None = None,
        reason: str | None = None,
        candidate_id: str | None = None,
        evaluation_id: str | None = None,
        baseline: float | None = None,
        candidate_score: float | None = None,
        regressions: list[str] | None = None,
        model: str = "unspecified",
        user_confirmed: bool = False,
        version_id: str | None = None,
    ) -> dict[str, Any]:
        """Advance exactly one explicit governance action."""

        if action == "record_feedback":
            if not all([skill_id, module_id, feedback, report_version_id]):
                raise ValueError(
                    "record_feedback requires skill, module, feedback, and report version"
                )
            result = self.service.record_feedback(
                skill_id=skill_id,
                module_id=module_id,  # type: ignore[arg-type]
                feedback=feedback,
                explicit_promotion_requested=explicit_promotion_requested,
                report_version_id=report_version_id,
            )
        elif action == "propose":
            if not all([feedback_id, title, submodules, proposed_content, reason]):
                raise ValueError("propose requires feedback, metadata, content, and reason")
            result = self.service.propose(
                feedback_id,
                title=title,
                submodules=submodules,
                proposed_content=proposed_content,
                reason=reason,
            )
        elif action == "evaluate":
            if candidate_id is None or baseline is None or candidate_score is None:
                raise ValueError("evaluate requires candidate and scores")
            result = self.service.evaluate(
                candidate_id,
                baseline=baseline,
                candidate_score=candidate_score,
                regressions=regressions or [],
                model=model,
            )
        elif action == "publish":
            if candidate_id is None or evaluation_id is None:
                raise ValueError("publish requires candidate and evaluation")
            result = self.service.publish(
                candidate_id,
                evaluation_id,
                user_confirmed=user_confirmed,
            )
        else:
            if skill_id is None or version_id is None:
                raise ValueError("rollback requires skill_id and version_id")
            result = self.service.store.rollback(skill_id=skill_id, version_id=version_id)
        return result.model_dump(mode="json")


class ProjectSkillEvolutionTool(_SkillEvolutionTool):
    name = "project_skill_evolution"
    description = (
        "Govern one project-local Skill after explicit user intent: record feedback, "
        "propose, evaluate, explicitly publish, or rollback."
    )

    def __init__(self, workspace):
        super().__init__(ProjectSkillEvolutionService(workspace))


class ProductSkillEvolutionTool(_SkillEvolutionTool):
    name = "product_skill_evolution"
    description = "Product Skill Maintainer-only governance for product-wide active Skills."

    def __init__(self, product_root):
        super().__init__(
            ProductSkillEvolutionService(product_root, actor_id="product-skill-maintainer")
        )


class RunProductSkillMaintainerTool(Tool):
    name = "run_product_skill_maintainer"
    description = (
        "Delegate an explicitly requested product-wide Skill evolution action to the "
        "Product Skill Maintainer identity; this does not grant Main product publication."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults,
    ):
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.llm_provider = llm_provider
        self.agent_defaults = agent_defaults

    async def __call__(
        self, instruction: str, input_refs: list[str] | None = None
    ) -> dict[str, Any]:
        """Run one Product Skill Maintainer task in its restricted identity."""

        from ..reporting.agent_runner import ReportingAgentRunner
        from ..reporting.agentic_models import TaskEnvelope
        from ..reporting.config import load_packaged_agents

        run_id = f"product-skill-{uuid.uuid4().hex[:10]}"
        runner = ReportingAgentRunner(
            self.workspace,
            self.bus,
            self.llm_provider,
            self.agent_defaults,
        )
        envelope = TaskEnvelope(
            task_id="product-skill-evolution",
            run_id=run_id,
            agent_id="product-skill-maintainer",
            objective=instruction,
            input_refs=input_refs or [],
            constraints=[
                "只有用户明确要求产品级演进时才可行动",
                "必须按反馈、候选、评测、用户确认、版本的顺序推进",
            ],
            allowed_outputs=["skill_evolution_submission"],
        )
        try:
            result = await runner.run(
                load_packaged_agents()["product-skill-maintainer"],
                envelope,
                envelope.input_refs,
                workflow_id=f"product-skill:{run_id}",
            )
            return result.model_dump(mode="json")
        finally:
            await runner.close_workflow(f"product-skill:{run_id}")
