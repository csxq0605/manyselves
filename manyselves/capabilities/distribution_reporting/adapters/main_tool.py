"""Distribution Reporting commands exposed by the dedicated Demo Main Agent."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, Protocol
from uuid import UUID, uuid4

from manyselves.capabilities.distribution_reporting.runtime.models.entrypoint import (
    PublicAggregateExistingRequest,
    PublicFullReportRequest,
    PublicModuleReportRequest,
    PublicRenderExistingRequest,
    PublicReviseReportRequest,
    PublicTemplateDistillationRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    CostControlMode,
    UserSupplement,
)
from manyselves.runtime.tools.registry import Tool

ReportOperation = Literal[
    "distill_template_skill",
    "full_report",
    "module_report",
    "aggregate_existing",
    "render_existing",
    "revise_report",
]

_WORKFLOW_BY_OPERATION: dict[ReportOperation, str] = {
    "distill_template_skill": "distill-template-skill",
    "full_report": "full-report",
    "module_report": "module-report",
    "aggregate_existing": "aggregate-existing",
    "render_existing": "render-existing",
    "revise_report": "revise-report",
}


class WorkflowStartPort(Protocol):
    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]: ...


class ConversationRunPort(Protocol):
    def bind_run_to_active_conversation(
        self,
        run_id: str,
        agent_id: str = "main",
    ) -> str | None: ...


class RunReportingWorkflowTool(Tool):
    """Expose the six Distribution Reporting operations to Main."""

    name = "run_reporting_workflow"
    description = (
        "Start one Distribution Reporting operation selected from "
        "distill_template_skill, full_report, module_report, aggregate_existing, "
        "render_existing, or revise_report. revise_report copies baseline_run_id into a "
        "new Run; requested_changes maps subsection IDs to initial edits; impact_mode=auto "
        "builds an impact list from current inputs then continues; impact_mode=confirm "
        "waits for user acceptance of that list. Cross still links consistency revisions. "
        "NOT interruption resume."
    )
    side_effect = "ordered_state"

    def __init__(
        self,
        *,
        projection_resolver: Callable[[], WorkflowStartPort],
        conversation_resolver: Callable[[], ConversationRunPort],
    ) -> None:
        self._projection_resolver = projection_resolver
        self._conversation_resolver = conversation_resolver

    async def __call__(
        self,
        instruction: str,
        operation: ReportOperation,
        target_modules: list[str] | None = None,
        source_module_refs: dict[str, str] | None = None,
        source_markdown_ref: str | None = None,
        output_filename: str | None = None,
        baseline_run_id: str | None = None,
        requested_changes: dict[str, str] | None = None,
        impact_mode: Literal["none", "auto", "confirm"] = "none",
        execution_requirements: list[str] | None = None,
        user_supplements: list[dict[str, Any]] | None = None,
        missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "draft",
        cost_control_mode: CostControlMode = "observe",
        max_provider_attempts: int = 600,
        max_total_tokens: int = 30_000_000,
        preparation_mode: Literal["serial", "deterministic_workers"] = (
            "deterministic_workers"
        ),
        preparation_concurrency: int = 5,
        template_ref: str | None = None,
        inspect_max_chars: int = 100_000,
    ) -> dict[str, Any]:
        """Start the selected report operation through its public Workflow root."""

        workflow_id = _WORKFLOW_BY_OPERATION[operation]
        if operation == "distill_template_skill":
            if template_ref is None:
                raise ValueError("distill_template_skill requires template_ref")
            request = PublicTemplateDistillationRequest(
                template_ref=template_ref,
                inspect_max_chars=inspect_max_chars,
            )
        else:
            values = {
                "instruction": instruction,
                "target_modules": (
                    target_modules
                    if target_modules is not None
                    else list(REPORT_MODULE_IDS)
                ),
                "source_module_refs": source_module_refs,
                "source_markdown_ref": (
                    Path(source_markdown_ref) if source_markdown_ref is not None else None
                ),
                "output_filename": output_filename,
                "baseline_run_id": baseline_run_id,
                "requested_changes": requested_changes or {},
                "impact_mode": impact_mode,
                "execution_requirements": execution_requirements or [],
                "user_supplements": [
                    UserSupplement.model_validate(item)
                    for item in (user_supplements or [])
                ],
                "missing_evidence_policy": missing_evidence_policy,
                "cost_control_mode": cost_control_mode,
                "max_provider_attempts": max_provider_attempts,
                "max_total_tokens": max_total_tokens,
                "preparation_mode": preparation_mode,
                "preparation_concurrency": preparation_concurrency,
            }
            request_type = {
                "full_report": PublicFullReportRequest,
                "module_report": PublicModuleReportRequest,
                "aggregate_existing": PublicAggregateExistingRequest,
                "render_existing": PublicRenderExistingRequest,
                "revise_report": PublicReviseReportRequest,
            }[operation]
            request = request_type.model_validate(values)

        public_input = request.model_dump(mode="json", exclude={"operation"})
        accepted = await self._projection_resolver().start(
            uuid4(),
            workflow_id,
            public_input,
        )
        run_id = str(accepted["run_id"])
        conversation_id = self._conversation_resolver().bind_run_to_active_conversation(
            run_id
        )
        return {**accepted, "conversation_id": conversation_id}


def attach_main_reporting_tool(
    runtime_host: Any,
    *,
    projection_resolver: Callable[[], WorkflowStartPort],
    conversation_resolver: Callable[[], ConversationRunPort],
) -> bool:
    """Attach the Demo-specific Main command without moving it into Kernel."""

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
