"""Main-Agent tool for the project-local power-distribution workflow."""

from pathlib import Path
from typing import Any, Literal

from ..loops.bus import MessageBus
from ..reporting.models import ReportRequest
from ..reporting.service import ReportingService
from .registry import Tool
from .task_board import TaskBoard


class RunReportingWorkflowTool(Tool):
    name = "run_reporting_workflow"
    description = (
        "Run the configured power-distribution report Phase A workflow in the current project. "
        "Use this for report generation, evidence coverage checks, and requested module rewrites."
    )

    def __init__(self, workspace: Path, bus: MessageBus, task_board: TaskBoard):
        self.workspace = Path(workspace).resolve()
        self.service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
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
