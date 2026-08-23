"""Capability-owned composition for the public full-report tail.

The file-defined tail uses statically parameterized Cross, Chief, and Final
lane workflows.  This module owns only those definition specializations and
combines the already migrated Final and Delivery Tool bindings.  Cross Agent
and lifecycle bindings remain an explicit next boundary; leaving their first
Tool unbound lets the generic Host report that boundary after compilation.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.runtime.chief_runtime import (
    build_chief_chapter_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition import (
    build_cross_owner_tool_implementations,
    cross_owner_agent_invokers,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import (
    build_delivery_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.final_delivery_binding import (
    build_final_chapter_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)

_CHAPTER_IDS = ("1", "3", "4")


@dataclass(frozen=True)
class ReportingTailComposition:
    """Provide full-report tail definitions and migrated Tool bindings.

    ``cross_runtime`` is an internal Capability composition port.  The
    definition specializations are always available; Cross actions are only
    bound when their owning lifecycle runtime is supplied.  ``chief_runtime``
    follows the same rule for the Chief chapter cohort.
    """

    workspace: Path
    cross_runtime: Any | None = None
    chief_runtime: Any | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "workspace", Path(self.workspace).resolve())

    @property
    def workflow_specializer(self):
        return register_reporting_tail_workflow_specializations

    def tool_implementations(self) -> dict[str, Any]:
        """Return the Capability-owned tail Tool bindings."""

        store = ReportingStore(self.workspace)
        chief_tools = (
            build_chief_chapter_tool_implementations(self.chief_runtime)
            if self.chief_runtime is not None
            else {}
        )
        return {
            **build_cross_owner_tool_implementations(self.cross_runtime),
            **chief_tools,
            **build_final_chapter_tool_implementations(
                workspace=self.workspace,
                store=store,
            ),
            **build_delivery_tool_implementations(
                workspace=self.workspace,
                store=store,
            ),
        }

    @property
    def agent_invokers(self) -> dict[str, Any]:
        """Expose already-composed Cross and Chief invokers for Host use."""

        invokers = dict(cross_owner_agent_invokers(self.cross_runtime))
        if self.chief_runtime is not None:
            invokers.update(dict(self.chief_runtime.agent_invokers))
        return invokers


def register_reporting_tail_workflow_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register all static child workflow definitions required by full-report."""

    workflows: dict[str, WorkflowDefinition] = {}
    workflows.update(_register_cross_owner_pipelines(definitions))
    workflows.update(_register_chief_chapter_lanes(definitions))
    workflows.update(_register_final_chapter_lanes(definitions))
    workflows.update(_register_final_review_lanes(definitions))
    return workflows


def _register_cross_owner_pipelines(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    template = _workflow_template(definitions, "distribution-cross-owner-pipeline")
    workflows: dict[str, WorkflowDefinition] = {}
    for module_id in REPORT_MODULE_IDS:
        workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        workflows[workflow_id] = _existing_or_specialized(
            definitions,
            workflow_id,
            template,
            {
                "owner_module_id": module_id,
                "conversation_key": f"cross-owner-{module_id}",
                "revision_agent_id": f"module-{module_id}-specialist",
                "revision_task_id": f"cross-owner-module-{module_id}-revision-r1",
                "revision_conversation_key": f"module-{module_id}",
                "local_review_conversation_key": f"module-auditor-{module_id}",
            },
        )
    return workflows


def _register_chief_chapter_lanes(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    template = _workflow_template(definitions, "distribution-chief-chapter-lane")
    workflows: dict[str, WorkflowDefinition] = {}
    for chapter_id in _CHAPTER_IDS:
        workflow_id = f"distribution-chief-chapter-{chapter_id}-lane"
        workflows[workflow_id] = _existing_or_specialized(
            definitions,
            workflow_id,
            template,
            {
                "chapter_id": chapter_id,
                "conversation_key": f"chief-chapter-{chapter_id}",
            },
        )
    return workflows


def _register_final_chapter_lanes(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    template = _workflow_template(definitions, "distribution-final-chapter-lane")
    workflows: dict[str, WorkflowDefinition] = {}
    for chapter_id in _CHAPTER_IDS:
        workflow_id = f"distribution-final-chapter-{chapter_id}-lane"
        workflows[workflow_id] = _existing_or_specialized(
            definitions,
            workflow_id,
            template,
            {
                "chapter_id": chapter_id,
                "conversation_key": f"final-chapter-{chapter_id}",
            },
        )
    return workflows


def _register_final_review_lanes(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    workflows: dict[str, WorkflowDefinition] = {}
    for phase in ("chief-revision", "recheck"):
        template = _workflow_template(
            definitions,
            f"distribution-final-{phase}-lane",
        )
        for chapter_id in _CHAPTER_IDS:
            workflow_id = f"distribution-final-{phase}-{chapter_id}-lane"
            workflows[workflow_id] = _existing_or_specialized(
                definitions,
                workflow_id,
                template,
                {
                    "chapter_id": chapter_id,
                    "conversation_key": (
                        f"chief-chapter-{chapter_id}"
                        if phase == "chief-revision"
                        else f"final-chapter-{chapter_id}"
                    ),
                },
            )
    return workflows


def _workflow_template(
    definitions: DefinitionRegistry,
    workflow_id: str,
) -> WorkflowDefinition:
    definition = definitions.require(DefinitionKind.WORKFLOW, workflow_id)
    if not isinstance(definition, WorkflowDefinition):
        raise TypeError(f"{workflow_id} is not a workflow")
    return definition


def _existing_or_specialized(
    definitions: DefinitionRegistry,
    workflow_id: str,
    template: WorkflowDefinition,
    parameters: dict[str, str],
) -> WorkflowDefinition:
    existing = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
    if existing is not None:
        if not isinstance(existing, WorkflowDefinition):
            raise TypeError(f"{workflow_id} is not a workflow")
        return existing
    workflow = specialize_workflow(
        template,
        parameters,
        workflow_id=workflow_id,
    )
    definitions.register(workflow)
    return workflow


__all__ = [
    "ReportingTailComposition",
    "register_reporting_tail_workflow_specializations",
]
