"""Reporting-owned bindings for the file-declared Delivery stages."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from pydantic import BaseModel

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    ReportRequest,
    SpecialTopicPlan,
)
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
)
from manyselves.kernel.executors import ExecutorRegistry
from manyselves.kernel.workflow import ResolvedPlan, WorkflowCompiler

from .agentic_models import EditedReportSubmission, ModuleSubmission
from .workflow import _DeliveryContext

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

    def __init__(self, runner: Any) -> None:
        self._runner = getattr(runner, "_runner", runner)
        self.current_state: dict[str, Any] = {}

    def prepare(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            return state
        context = self._runner._prepare_and_render_delivery(state)
        self._save_context(state, context)
        return state

    def publish(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            return state
        context = self._runner._publish_and_materialize_delivery(
            self._load_context(state)
        )
        self._save_context(state, context)
        return state

    def complete(self, state: dict[str, Any]) -> dict[str, Any]:
        self._restore_state(state)
        self.current_state = state
        if "delivery_completion_ref" in state:
            self._serialize_output_artifacts(state)
            return state
        self._runner._complete_delivery(self._load_context(state))
        state.pop(_DELIVERY_CONTEXT_KEY)
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
    def _save_context(
        state: dict[str, Any],
        context: _DeliveryContext,
    ) -> None:
        state[_DELIVERY_CONTEXT_KEY] = context.model_dump(
            mode="json",
            exclude={"state"},
        )

    @staticmethod
    def _load_context(state: dict[str, Any]) -> _DeliveryContext:
        return _DeliveryContext.model_validate(
            {
                "state": state,
                **state[_DELIVERY_CONTEXT_KEY],
            }
        )

    @staticmethod
    def _restore_state(state: dict[str, Any]) -> None:
        modules = state.get("module_submissions")
        if isinstance(modules, Mapping):
            state["module_submissions"] = {
                module_id: ModuleSubmission.model_validate(value)
                for module_id, value in modules.items()
            }
        request = state.get("request")
        if request is not None:
            state["request"] = ReportRequest.model_validate(request)
        evidence = state.get("evidence_items")
        if isinstance(evidence, list):
            state["evidence_items"] = [
                EvidenceItem.model_validate(item) for item in evidence
            ]
        photos = state.get("photo_assets")
        if isinstance(photos, list):
            state["photo_assets"] = [PhotoAsset.model_validate(item) for item in photos]
        edited = state.get("edited_report")
        if edited is not None:
            state["edited_report"] = EditedReportSubmission.model_validate(edited)
        plan = state.get("special_topic_plan")
        if isinstance(plan, Mapping):
            state["special_topic_plan"] = SpecialTopicPlan.model_validate(plan)


__all__ = ["DeclarativeDeliveryRuntime", "compile_delivery_workflow"]
