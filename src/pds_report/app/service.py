from __future__ import annotations

from importlib import resources
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from pds_report.agents.base import AgentContext
from pds_report.agents.builtin import build_builtin_agents
from pds_report.app.request_parser import parse_report_request
from pds_report.domain.models import OutputArtifact, RunStatus
from pds_report.infrastructure.project_store import ProjectStore
from pds_report.workflow.bus import MessageBus
from pds_report.workflow.config import load_agent_definitions, load_workflow
from pds_report.workflow.runner import WorkflowRunner
from pds_report.workflow.tasks import TaskBoard


class ApplicationReply(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: str
    status: RunStatus
    message: str
    files: list[Path] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class ReportApplication:
    def __init__(self, runner: WorkflowRunner | None = None) -> None:
        self.runner = runner or WorkflowRunner()

    async def run_message(self, project_root: Path, message: str) -> ApplicationReply:
        store = ProjectStore(project_root)
        store.create()
        request = parse_report_request(message)
        run_id = f"run-{uuid4().hex[:12]}"
        bus = MessageBus()
        task_board = TaskBoard()
        context = AgentContext(
            project_root=store.root,
            run_id=run_id,
            state={"report_request": request},
            bus=bus,
            task_board=task_board,
        )

        resource_root = Path(str(resources.files("pds_report.resources")))
        definitions = load_agent_definitions(resource_root / "agents")
        workflow = load_workflow(resource_root / "workflows" / "phase-a.yml", definitions)
        result = await self.runner.run(
            workflow,
            definitions,
            build_builtin_agents(store),
            context,
        )
        store.save_run(
            run_id,
            {
                "run_id": run_id,
                "status": result.status,
                "state": result.state,
                "errors": result.errors,
                "tasks": result.tasks,
                "events": bus.history,
            },
        )

        output_artifacts = result.state.get("output_artifacts", [])
        files = [
            store.root / artifact.relative_path
            for artifact in output_artifacts
            if isinstance(artifact, OutputArtifact)
        ]
        summary = result.state.get("run_summary")
        if isinstance(summary, dict) and isinstance(summary.get("message"), str):
            reply_message = summary["message"]
        elif result.errors:
            reply_message = f"流程失败：{result.errors[0]}"
        else:
            reply_message = "流程结束，但没有生成交付摘要。"
        return ApplicationReply(
            run_id=run_id,
            status=result.status,
            message=reply_message,
            files=files,
            errors=result.errors,
        )
