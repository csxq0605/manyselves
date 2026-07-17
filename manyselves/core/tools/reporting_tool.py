"""Main-Agent tool for the project-local power-distribution workflow."""

from pathlib import Path
from typing import Any, Literal

from ...config.schema import AgentDefaults
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..reporting.models import EvidenceDecisionAction, ReportRequest, RevisionRequest
from ..reporting.service import ReportingService
from .registry import Tool
from .task_board import TaskBoard
from .outcomes import ToolOutcome, normalize_tool_outcome


def reporting_result_outcome(payload: dict[str, Any]) -> ToolOutcome:
    """Normalize a reporting entry-point result for loop and GUI consumers."""

    return normalize_tool_outcome(payload, "run_reporting_workflow")


class RunReportingWorkflowTool(Tool):
    name = "run_reporting_workflow"
    description = (
        "Run the complete provider-backed V2 multi-agent power-distribution report workflow. "
        "Use this for five-module report generation, evidence coverage checks, and local rewrites."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
            llm_provider=llm_provider,
            agent_defaults=agent_defaults,
        )

    async def __call__(
        self,
        instruction: str,
        target_modules: list[str] | None = None,
        execution_requirements: list[str] | None = None,
        missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "ask",
    ) -> dict[str, Any]:
        """Run the report workflow.

        Args:
            instruction: The user's report request without invented requirements.
            target_modules: Fixed report modules to run, from 2.1 through 2.5.
            execution_requirements: Turn-specific requirements such as deep reasoning.
            missing_evidence_policy: How to handle submodules without customer evidence.
        """

        request = ReportRequest(
            instruction=instruction,
            target_modules=target_modules or ["2.1", "2.2", "2.3", "2.4", "2.5"],
            execution_requirements=execution_requirements or [],
            missing_evidence_policy=missing_evidence_policy,
        )
        result = await self.service.run(request)
        payload = result.model_dump(mode="json")
        payload["output_paths"] = [
            Path(path).relative_to(self.workspace).as_posix() for path in result.output_paths
        ]
        return payload


class ResumeReportingWorkflowTool(Tool):
    name = "resume_reporting_workflow"
    description = (
        "Resume the same report run after a missing-evidence decision. "
        "Use the pending decision_id and one of supplement, draft, skip, or stop."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
            llm_provider=llm_provider,
            agent_defaults=agent_defaults,
        )

    async def __call__(
        self,
        decision_id: str,
        action: EvidenceDecisionAction,
        user_notes: str | None = None,
    ) -> dict[str, Any]:
        """Resume a pending evidence decision in its original report run."""

        result = await self.service.resume(decision_id, action, user_notes)
        payload = result.model_dump(mode="json")
        payload["output_paths"] = [
            Path(path).relative_to(self.workspace).as_posix() for path in result.output_paths
        ]
        return payload


class ReviseReportingWorkflowTool(Tool):
    name = "revise_reporting_workflow"
    description = (
        "Revise a delivered report from an immutable baseline version. "
        "Use for report feedback; Skill promotion remains false unless explicitly requested."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
            llm_provider=llm_provider,
            agent_defaults=agent_defaults,
        )

    async def __call__(
        self,
        baseline_version_id: str,
        feedback: str,
        target_module_ids: list[str],
        target_submodule_ids: list[str] | None = None,
        target_claim_ids: list[str] | None = None,
        promote_to_skill: bool = False,
        promote_skill_id: str | None = None,
    ) -> dict[str, Any]:
        """Run a version-aware local report revision."""

        result = await self.service.revise(
            RevisionRequest(
                baseline_version_id=baseline_version_id,
                feedback=feedback,
                target_module_ids=target_module_ids,
                target_submodule_ids=target_submodule_ids or [],
                target_claim_ids=target_claim_ids or [],
                promote_to_skill=promote_to_skill,
                promote_skill_id=promote_skill_id,
            )
        )
        payload = result.model_dump(mode="json")
        payload["output_paths"] = [
            Path(path).relative_to(self.workspace).as_posix() for path in result.output_paths
        ]
        return payload
