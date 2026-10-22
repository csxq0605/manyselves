"""Generic Run tools for Main: read a Run's status and resume it.

They work for any Capability's Runs.  Resume keeps the Run id; when the
definitions changed since the Run was compiled, the Run is executed again in
place and every unchanged model turn is reused instead of being redone.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from uuid import uuid4

from manyselves.runtime.tools.registry import Tool


class GetRunStatusTool(Tool):
    name = "get_run_status"
    description = (
        "Read one Run's status. For a failed Run, also where it stopped, the error, "
        "and its class: temporary (Provider/network), model (an Agent gave no usable "
        "result), program (a Tool or step failed; resuming fails the same way) or "
        "cancelled. Also lists archived versions of the Run."
    )
    side_effect = "pure_read"
    parallel_safe = True

    def __init__(self, *, projection_resolver: Callable[[], Any]) -> None:
        self._projection_resolver = projection_resolver

    async def __call__(self, run_id: str) -> dict[str, Any]:
        """Return the Run's status, its failure if any, and its versions."""

        projection = self._projection_resolver()
        run = projection.get_run(run_id)["run"]
        failure = projection.describe_failure(run_id)
        supervisor = getattr(projection, "supervisor", None)
        return {
            "run_id": run_id,
            "workflow_id": run.get("workflow_id"),
            "status": run.get("status"),
            "active": run.get("active"),
            "failure": failure.to_json() if failure is not None else None,
            "auto_resumes": supervisor.auto_resumes(run_id) if supervisor else 0,
            "versions": projection.versions(run_id),
        }


class ResumeRunTool(Tool):
    name = "resume_run"
    description = (
        "Resume a failed or interrupted Run under the same run id. Completed steps "
        "and unchanged model turns are reused, not redone. Do not use it for a "
        "program failure that has not been fixed; it would fail the same way."
    )
    side_effect = "ordered_state"

    def __init__(self, *, projection_resolver: Callable[[], Any]) -> None:
        self._projection_resolver = projection_resolver

    async def __call__(self, run_id: str) -> dict[str, Any]:
        """Resume the Run and return the accepted command."""

        return await self._projection_resolver().resume(uuid4(), run_id)


def attach_main_run_tools(
    runtime_host: Any,
    *,
    projection_resolver: Callable[[], Any],
) -> bool:
    """Give Main the generic Run tools."""

    tools = (
        lambda: GetRunStatusTool(projection_resolver=projection_resolver),
        lambda: ResumeRunTool(projection_resolver=projection_resolver),
    )
    names = (GetRunStatusTool.name, ResumeRunTool.name)
    register_factory = getattr(runtime_host, "register_agent_tool_factory", None)
    if callable(register_factory):
        return all(
            bool(register_factory("main", name, factory))
            for name, factory in zip(names, tools, strict=True)
        )
    manager = getattr(runtime_host, "loop_manager", None)
    register_tool = getattr(manager, "register_agent_tool", None)
    if not callable(register_tool):
        return False
    return all(bool(register_tool("main", factory())) for factory in tools)


__all__ = ["GetRunStatusTool", "ResumeRunTool", "attach_main_run_tools"]
