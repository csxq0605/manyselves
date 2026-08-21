"""Typed state passed between file-defined production module Lane actions."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict

from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)

from .agentic_models import (
    ModuleReviewFindingSubmission,
    ModuleSubmission,
    TaskEnvelope,
)
from .models import REPORT_MODULE_IDS
from .parallel_runtime import LaneCompletion, LaneTaskSpec
from .review_lifecycle import ModuleInitialReviewPreparation


class DeclarativeModuleLaneAttempt(BaseModel):
    """Serializable identity of the current legacy-compatible Lane attempt."""

    model_config = ConfigDict(extra="forbid")

    spec: LaneTaskSpec
    spec_ref: str
    lane_attempt_id: str
    started_at_ns: int
    attempt_ref: str


class DeclarativeModuleAuthoringPreparation(BaseModel):
    """Serializable current TaskEnvelope and same-run authoring reuse state."""

    model_config = ConfigDict(extra="forbid")

    specialist_id: str
    envelope: TaskEnvelope | None
    resumed_payload: ModuleSubmission | None = None
    revision: int
    review: bool
    checkpoint: bool


class DeclarativeModuleAuthoringAgentResult(BaseModel):
    """Typed business result returned by the current module Agent adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    module: Any = None
    error: str | None = None


class DeclarativeModuleReviewPreparation(BaseModel):
    """Serializable exact initial Auditor turn prepared by Reporting."""

    model_config = ConfigDict(extra="forbid")

    envelope: TaskEnvelope
    reviewer_session_key: str
    prepared: ModuleInitialReviewPreparation


class DeclarativeModuleReviewAgentResult(BaseModel):
    """Typed business result returned by the current module Auditor adapter."""

    model_config = ConfigDict(extra="forbid")

    status: Literal["completed", "failed"]
    submission: ModuleReviewFindingSubmission | None = None
    error: str | None = None


class DeclarativeModuleRuntimeLaneContext(BaseModel):
    """Capability-owned state threaded through one file-defined module Lane."""

    model_config = ConfigDict(extra="forbid")

    module_id: str
    workflow_id: str
    reporting_state: dict[str, Any]
    status: Literal[
        "ready",
        "author_ready",
        "author_resumed",
        "authored",
        "review_ready",
        "review_resumed",
        "reviewed",
        "completed",
        "deferred",
        "failed",
    ]
    attempt: DeclarativeModuleLaneAttempt | None = None
    authoring: DeclarativeModuleAuthoringPreparation | None = None
    review: DeclarativeModuleReviewPreparation | None = None
    module: ModuleSubmission | None = None
    completion_ref: str | None = None
    completion: LaneCompletion | None = None
    error: str | None = None


def register_module_runtime_lane_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register one compiled specialization of the packaged Lane per module."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-runtime-lane",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-module-runtime-lane is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for module_id in REPORT_MODULE_IDS:
        workflow_id = f"distribution-module-{module_id}-runtime-lane"
        workflow = specialize_workflow(
            template,
            {
                "module_id": module_id,
                "author_id": f"module-{module_id}-specialist",
                "author_task_id": f"module-{module_id}-authoring",
                "author_conversation_key": f"specialist-{module_id}",
                "auditor_conversation_key": f"module-auditor-{module_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


__all__ = [
    "DeclarativeModuleAuthoringAgentResult",
    "DeclarativeModuleAuthoringPreparation",
    "DeclarativeModuleLaneAttempt",
    "DeclarativeModuleReviewAgentResult",
    "DeclarativeModuleReviewPreparation",
    "DeclarativeModuleRuntimeLaneContext",
    "register_module_runtime_lane_specializations",
]
