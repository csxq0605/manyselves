"""Definition specializations for the distribution-reporting module Lane."""

from manyselves.core.reporting.models import REPORT_MODULE_IDS
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)


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
                "revision_task_id": f"module-{module_id}-runtime-revision",
                "revision_conversation_key": f"module-{module_id}",
            },
            workflow_id=workflow_id,
        )
        definitions.register(workflow)
        workflows[workflow_id] = workflow
    return workflows


__all__ = ["register_module_runtime_lane_specializations"]
