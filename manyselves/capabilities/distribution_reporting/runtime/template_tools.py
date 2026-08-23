"""Provider-facing Tool composition for the file-defined template task.

The concrete result tools are Capability-owned.  This module only assembles the
small set declared by ``template-skill-distillation``; run identity, durable
attempt correlation, and any generic continuation reader remain injected by
the caller that owns those Runtime concerns.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    ListResultPartsTool,
    ReportBlockedTool,
    SubmitResultTool,
    WriteResultPartTool,
)
from manyselves.capabilities.distribution_reporting.runtime.contracts.submissions import (
    submission_schema,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    TemplateDistillationInput,
)
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    TaskCorrelation,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.loops.bus import MessageBus
from manyselves.core.tools.document_tool import InspectDocumentTool
from manyselves.core.tools.registry import Tool, ToolRegistry

TemplateRecoveryEventCallback = Callable[
    [str, dict[str, Any]], Awaitable[Any]
]


def template_result_part_tool_schema(
    expected_part_ids: Iterable[str],
) -> dict[str, Any]:
    """Return the provider-visible single-part shape for Template distillation."""

    return {
        "type": "object",
        "properties": {
            "part_id": {
                "type": "string",
                "enum": list(expected_part_ids),
                "description": "One fixed part id assigned by the current task.",
            },
            "content": {
                "type": "string",
                "minLength": 1,
                "maxLength": 48_000,
                "description": (
                    "Complete reader-visible prose for this one durable part. Always "
                    "supply the full intended prose. Every call must contain all "
                    "required arguments. After a successful write, use "
                    "list_result_parts and keep an already-ready part without "
                    "rewriting it unless correction feedback explicitly names that "
                    "part."
                ),
            },
        },
        "required": ["part_id", "content"],
        "additionalProperties": False,
    }


def build_template_distillation_provider_tools(
    workspace: Path,
    *,
    envelope: TaskEnvelope,
    template_input: TemplateDistillationInput,
    session_id: str,
    workflow_id: str,
    bus: MessageBus,
    store: ReportingStore,
    tool_names: Iterable[str],
    continuation_reader: Tool | None = None,
    task_correlation: TaskCorrelation | None = None,
    recovery_event_callback: TemplateRecoveryEventCallback | None = None,
) -> ToolRegistry:
    """Build the exact Provider ToolRegistry for Template distillation.

    ``continuation_reader`` is an injected generic Runtime tool.  It is kept
    outside this Capability helper because its run-level result index is owned
    by the Runtime/legacy composition boundary rather than by Template.
    """

    registry = ToolRegistry()
    expected_part_ids = list(template_input.required_part_ids)
    available: dict[str, Tool] = {
        "inspect_document": InspectDocumentTool(
            workspace,
            one_shot=True,
            allow_template_distiller_source=True,
            required_path=template_input.template_ref,
            required_max_chars=template_input.inspect_max_chars,
            cache_ref=(
                f"Work/runs/{envelope.run_id}/context/template-inspection.json"
            ),
        ),
        "write_result_part": WriteResultPartTool(
            envelope.run_id,
            envelope.task_id,
            envelope.revision,
            store,
            expected_part_ids,
        ),
        "list_result_parts": ListResultPartsTool(
            envelope.run_id,
            envelope.task_id,
            envelope.revision,
            store,
            expected_part_ids,
        ),
        "submit_result": SubmitResultTool(
            envelope.agent_id,
            session_id,
            envelope.run_id,
            envelope.task_id,
            store,
            bus,
            workflow_id,
            allowed_outputs=list(envelope.allowed_outputs),
            revision=envelope.revision,
            input_contract_kind=envelope.input_contract_kind,
            input_contract_ref=envelope.input_contract_ref,
            submission_schemas={
                "template_skill_submission": submission_schema(
                    "template_skill_submission"
                )
            },
            task_correlation=task_correlation,
            recovery_event_callback=recovery_event_callback,
        ),
        "report_blocked": ReportBlockedTool(
            envelope.agent_id,
            session_id,
            envelope.run_id,
            envelope.task_id,
            store,
            bus,
            workflow_id,
            task_correlation=task_correlation,
        ),
    }
    if continuation_reader is not None:
        available["open_tool_result"] = continuation_reader

    for name in tool_names:
        try:
            tool = available[name]
        except KeyError as exc:
            raise ValueError(
                f"unsupported template distillation tool: {name}"
            ) from exc
        registry.register(tool)

    if registry.get("submit_result") is not None:
        registry._schema_cache["submit_result"] = submission_schema(
            "template_skill_submission"
        )
    if registry.get("write_result_part") is not None:
        registry._schema_cache["write_result_part"] = template_result_part_tool_schema(
            expected_part_ids
        )
    return registry


__all__ = [
    "build_template_distillation_provider_tools",
    "template_result_part_tool_schema",
]
