"""Distribution Reporting commands exposed to the Main Agent.

Five operations, one per user-facing flow:

- ``full_report``: a new report from the project's inputs (a new run);
- ``revise_report``: change an existing report as its next version, under
  the same run id; unchanged steps are reused, not redone;
- ``extract_skills``: distill the writing Skills from the report template;
- ``publish_skills``: show what publishing a distillation would change, and
  publish it only with ``confirm=true`` after the person agreed;
- ``render_existing``: render an existing Markdown report to DOCX.

Resuming a stopped run is not here: Main's generic ``resume_run`` does it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from manyselves.capabilities.distribution_reporting.runtime.models.entrypoint import (
    PublicFullReportRequest,
    PublicRenderExistingRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.template_skills import (
    preview_template_skills,
    publish_template_skills,
)
from manyselves.runtime.tools.registry import Tool

ReportOperation = Literal[
    "full_report",
    "revise_report",
    "extract_skills",
    "publish_skills",
    "render_existing",
]


class WorkflowPort(Protocol):
    workspace: Path

    async def start(self, command_id: UUID, workflow_id: str, values: Any) -> dict[str, Any]: ...

    async def rerun(
        self, command_id: UUID, run_id: str, values: Any = None
    ) -> dict[str, Any]: ...


class ConversationRunPort(Protocol):
    def bind_run_to_active_conversation(
        self,
        run_id: str,
        agent_id: str = "main",
    ) -> str | None: ...


class RunReportingWorkflowTool(Tool):
    """Expose the Distribution Reporting operations to Main."""

    name = "run_reporting_workflow"
    description = (
        "Distribution report operations. full_report: new report from Inputs. "
        "revise_report: change the report of run_id as its next version under the same "
        "run id (changes: [{target: section id like 2.3.1, a module 2.3, 1, 3 or 全文; "
        "instruction; scope: text (wording only) or judgment}], new_inputs=true re-reads "
        "the current input files); only affected rows, sections and chapter 1/3 entries "
        "are redone. extract_skills: distill writing Skills from the template (a run). "
        "publish_skills: with run_id of an extraction run, returns what would change; "
        "publish only with confirm=true after the user agreed. render_existing: render "
        "source_markdown_ref to DOCX. To continue a stopped run use resume_run instead."
    )
    side_effect = "ordered_state"

    def __init__(
        self,
        *,
        projection_resolver: Callable[[], WorkflowPort],
        conversation_resolver: Callable[[], ConversationRunPort],
    ) -> None:
        self._projection_resolver = projection_resolver
        self._conversation_resolver = conversation_resolver

    async def __call__(
        self,
        operation: ReportOperation,
        instruction: str = "",
        run_id: str | None = None,
        changes: list[dict[str, Any]] | None = None,
        new_inputs: bool = False,
        roles: list[str] | None = None,
        confirm: bool = False,
        source_markdown_ref: str | None = None,
        output_filename: str | None = None,
        execution_requirements: list[str] | None = None,
        user_supplements: list[dict[str, Any]] | None = None,
        missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "draft",
    ) -> dict[str, Any]:
        """Run one report operation; see the tool description."""

        projection = self._projection_resolver()
        if operation == "publish_skills":
            if not run_id:
                raise ValueError("publish_skills needs the run_id of an extraction run")
            workspace = Path(projection.workspace)
            if not confirm:
                return {"status": "preview", **preview_template_skills(workspace, run_id)}
            return {"status": "published", **publish_template_skills(workspace, run_id)}
        if operation == "revise_report":
            if not run_id:
                raise ValueError("revise_report needs the run_id of the report to change")
            accepted = await projection.rerun(
                uuid4(),
                run_id,
                {
                    "instruction": instruction or "按修改要求修改报告。",
                    "changes": changes or [],
                    "new_inputs": new_inputs,
                },
            )
            return self._bound(accepted)
        if operation == "extract_skills":
            values = {"roles": roles} if roles else {}
            accepted = await projection.start(uuid4(), "extract-template-skills", values)
            return self._bound(accepted)
        common = {
            "instruction": instruction or "生成报告。",
            "output_filename": output_filename,
            "execution_requirements": execution_requirements or [],
            "user_supplements": [
                UserSupplement.model_validate(item) for item in (user_supplements or [])
            ],
            "missing_evidence_policy": missing_evidence_policy,
        }
        if operation == "render_existing":
            request = PublicRenderExistingRequest.model_validate(
                {
                    **common,
                    "source_markdown_ref": (
                        Path(source_markdown_ref) if source_markdown_ref else None
                    ),
                }
            )
            workflow_id = "render-existing"
        else:
            request = PublicFullReportRequest.model_validate(
                {**common, "target_modules": list(REPORT_MODULE_IDS)}
            )
            workflow_id = "full-report"
        accepted = await projection.start(
            uuid4(), workflow_id, request.model_dump(mode="json", exclude={"operation"})
        )
        return self._bound(accepted)

    def _bound(self, accepted: dict[str, Any]) -> dict[str, Any]:
        run_id = str(accepted["run_id"])
        conversation_id = self._conversation_resolver().bind_run_to_active_conversation(
            run_id
        )
        return {**accepted, "conversation_id": conversation_id}


def attach_main_reporting_tool(
    runtime_host: Any,
    *,
    projection_resolver: Callable[[], WorkflowPort],
    conversation_resolver: Callable[[], ConversationRunPort],
) -> bool:
    """Attach the report command to Main without moving it into the kernel."""

    def factory() -> RunReportingWorkflowTool:
        return RunReportingWorkflowTool(
            projection_resolver=projection_resolver,
            conversation_resolver=conversation_resolver,
        )

    register_factory = getattr(runtime_host, "register_agent_tool_factory", None)
    if callable(register_factory):
        return bool(
            register_factory("main", RunReportingWorkflowTool.name, factory)
        )
    manager = getattr(runtime_host, "loop_manager", None)
    register_tool = getattr(manager, "register_agent_tool", None)
    if not callable(register_tool):
        return False
    return bool(register_tool("main", factory()))


__all__ = ["RunReportingWorkflowTool", "attach_main_reporting_tool"]
