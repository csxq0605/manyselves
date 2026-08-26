"""Application-owned workflow commands exposed to the user-facing Main Agent."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol
from uuid import UUID, uuid4

from ..runtime.tools.registry import Tool


class WorkflowProjectionPort(Protocol):
    """The generic workflow operations needed by Main."""

    def list_workflows(self) -> list[dict[str, Any]]: ...

    def input_schema(self, workflow_id: str) -> dict[str, Any]: ...

    async def start(
        self,
        command_id: UUID,
        workflow_id: str,
        values: Any,
    ) -> dict[str, Any]: ...


class ConversationRunPort(Protocol):
    """Bind an accepted Run to the active durable Main conversation."""

    def bind_run_to_active_conversation(
        self,
        run_id: str,
        agent_id: str = "main",
    ) -> str | None: ...


class ManageWorkflowsTool(Tool):
    """Discover and start file-defined workflows through the generic projection."""

    name = "manage_workflows"
    description = (
        "Discover and start file-defined workflows for the current project. "
        "Use action='list' to see runnable workflow IDs, action='schema' to read "
        "one workflow's JSON Schema before starting it, and action='start' with "
        "workflow_id plus an input object to launch the Run. The accepted Run is "
        "automatically attached to this Main conversation and appears in the Run UI."
    )
    side_effect = "ordered_state"

    def __init__(
        self,
        *,
        projection_resolver: Callable[[], WorkflowProjectionPort],
        conversation_resolver: Callable[[], ConversationRunPort],
    ) -> None:
        self._projection_resolver = projection_resolver
        self._conversation_resolver = conversation_resolver

    async def __call__(
        self,
        action: str,
        workflow_id: str | None = None,
        input: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Inspect or start a file-defined workflow.

        Args:
            action: One of: list, schema, start.
            workflow_id: Workflow ID required for schema and start.
            input: Schema-conforming workflow input required for start.
        """
        projection = self._projection_resolver()
        if action == "list":
            return {
                "status": "ok",
                "workflows": [
                    item for item in projection.list_workflows() if item["runnable"]
                ],
            }
        if action == "schema":
            if workflow_id is None:
                return {"status": "error", "error": "workflow_id is required"}
            return {"status": "ok", **projection.input_schema(workflow_id)}
        if action == "start":
            if workflow_id is None:
                return {"status": "error", "error": "workflow_id is required"}
            accepted = await projection.start(uuid4(), workflow_id, input or {})
            run_id = str(accepted["run_id"])
            conversation_id = self._conversation_resolver().bind_run_to_active_conversation(
                run_id
            )
            return {
                **accepted,
                "conversation_id": conversation_id,
            }
        return {
            "status": "error",
            "error": "action must be one of: list, schema, start",
        }


def attach_main_workflow_tool(
    runtime_host: Any,
    *,
    projection_resolver: Callable[[], WorkflowProjectionPort],
    conversation_resolver: Callable[[], ConversationRunPort],
) -> bool:
    """Attach the generic Application workflow port to an existing Main loop."""

    manager = getattr(runtime_host, "loop_manager", None)
    register = getattr(manager, "register_agent_tool", None)
    if not callable(register):
        return False
    return bool(
        register(
            "main",
            ManageWorkflowsTool(
                projection_resolver=projection_resolver,
                conversation_resolver=conversation_resolver,
            ),
        )
    )
