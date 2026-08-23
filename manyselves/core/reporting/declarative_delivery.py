"""Reporting-owned bindings for the file-declared Delivery stages."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import (
    _restore_delivery_state,
    build_delivery_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.models.delivery import (
    DeliveryContext,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
)
from manyselves.kernel.executors import ExecutorRegistry
from manyselves.kernel.workflow import ResolvedPlan, WorkflowCompiler

_DELIVERY_CONTEXT_KEY = "_declarative_delivery_context"


def compile_delivery_workflow(
    definitions: DefinitionRegistry,
    executors: ExecutorRegistry,
) -> ResolvedPlan:
    """Compile the packaged Render, Publish, and completion sequence."""

    workflow = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-report-delivery",
    )
    if not isinstance(workflow, WorkflowDefinition):
        raise TypeError("distribution-report-delivery is not a workflow")
    return WorkflowCompiler(executors).compile(workflow, definitions)


class DeclarativeDeliveryRuntime:
    """Bind three declared actions to the current Reporting delivery semantics."""

    def __init__(
        self,
        runner: Any,
        *,
        workspace: Path | None = None,
        store: ReportingStore | None = None,
        prepare_tool: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        publish_tool: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        complete_tool: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self._runner = getattr(runner, "_runner", runner)
        self.current_state: dict[str, Any] = {}
        service = getattr(self._runner, "service", None)
        workspace = workspace or getattr(service, "workspace", None)
        store = store or getattr(service, "store", None)
        if prepare_tool is None and workspace is not None:
            prepare_tool = build_delivery_tool_implementations(
                workspace=Path(workspace),
                store=store or ReportingStore(Path(workspace)),
            )["prepare-render-delivery"]
        if publish_tool is None and workspace is not None:
            publish_tool = build_delivery_tool_implementations(
                workspace=Path(workspace),
                store=store or ReportingStore(Path(workspace)),
            )["publish-materialize-delivery"]
        if complete_tool is None and workspace is not None:
            complete_tool = build_delivery_tool_implementations(
                workspace=Path(workspace),
                store=store or ReportingStore(Path(workspace)),
            )["complete-delivery"]
        self._prepare_tool = prepare_tool
        self._publish_tool = publish_tool
        self._complete_tool = complete_tool

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            return state
        if self._prepare_tool is None:
            raise RuntimeError("Delivery prepare Tool requires a Capability binding")
        result = self._prepare_tool(state)
        if result is not state:
            state.clear()
            state.update(result)
        return state

    def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            return state
        if self._publish_tool is None:
            raise RuntimeError("Delivery publish Tool requires a workspace binding")
        result = self._publish_tool(state)
        if result is not state:
            state.clear()
            state.update(result)
        return state

    def complete(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            self._serialize_output_artifacts(state)
            return state
        if self._complete_tool is None:
            raise RuntimeError("Delivery complete Tool requires a workspace binding")
        result = self._complete_tool(state)
        if result is not state:
            state.clear()
            state.update(result)
        self._serialize_output_artifacts(state)
        return state

    @staticmethod
    def _serialize_output_artifacts(state: dict[str, Any]) -> None:
        artifacts = state.get("output_artifacts")
        if not isinstance(artifacts, list):
            return
        state["output_artifacts"] = [
            artifact.model_dump(mode="json")
            if isinstance(artifact, BaseModel)
            else artifact
            for artifact in artifacts
        ]

    @staticmethod
    def _load_context(state: dict[str, Any]) -> DeliveryContext:
        return DeliveryContext.model_validate(
            {
                "state": state,
                **state[_DELIVERY_CONTEXT_KEY],
            }
        )

    @staticmethod
    def _restore_state(state: dict[str, Any]) -> None:
        _restore_delivery_state(state)

__all__ = ["DeclarativeDeliveryRuntime", "compile_delivery_workflow"]
