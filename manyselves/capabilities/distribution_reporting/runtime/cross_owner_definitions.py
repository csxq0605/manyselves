"""Capability-owned Definition specializations for Cross owner pipelines."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)


def register_cross_owner_pipeline_specializations(
    definitions: DefinitionRegistry,
) -> dict[str, WorkflowDefinition]:
    """Register one statically bound owner-pipeline workflow per module."""

    template = definitions.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-pipeline",
    )
    if not isinstance(template, WorkflowDefinition):
        raise TypeError("distribution-cross-owner-pipeline is not a workflow")
    workflows: dict[str, WorkflowDefinition] = {}
    for module_id in REPORT_MODULE_IDS:
        workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        registered = definitions.get(DefinitionKind.WORKFLOW, workflow_id)
        if registered is not None:
            if not isinstance(registered, WorkflowDefinition):
                raise TypeError(f"{workflow_id} is not a workflow")
            workflows[workflow_id] = registered
            continue
        workflow = specialize_workflow(
            template,
            {
                "owner_module_id": module_id,
                "conversation_key": f"cross-owner-{module_id}",
                "revision_agent_id": f"module-{module_id}-specialist",
                "revision_task_id": f"cross-owner-module-{module_id}-revision-r1",
                "revision_conversation_key": f"module-{module_id}",
                "local_review_conversation_key": f"module-auditor-{module_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


__all__ = ["register_cross_owner_pipeline_specializations"]
