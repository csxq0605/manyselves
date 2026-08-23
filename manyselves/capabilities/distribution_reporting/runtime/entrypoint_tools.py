"""Deterministic projections between top-level Reporting workflow contexts."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from copy import deepcopy
from typing import Any

from manyselves.kernel.definitions import WorkflowDefinition

from .models.agentic import ModuleSubmission
from .models.entrypoint import (
    ReportingPreparationAttachment,
    ReportingRunContext,
    ReportingRunInitializerInput,
)
from .models.evidence_readiness import EvidenceReadinessState
from .models.preparation import PreparationContext
from .models.reporting import REPORT_MODULE_IDS


def initialize_reporting_run(value: Any) -> ReportingRunContext:
    """Bind the generic Run identity to the validated public request."""

    inputs = ReportingRunInitializerInput.model_validate(value)
    return ReportingRunContext(
        run_id=inputs.run_id,
        request=inputs.request,
        preparation_context={
            "run_id": inputs.run_id,
            "request": inputs.request,
        },
    )


def attach_preparation(value: Any) -> ReportingRunContext:
    """Attach the exact result returned by the Preparation subworkflow."""

    inputs = ReportingPreparationAttachment.model_validate(value)
    return inputs.context.model_copy(
        update={
            "preparation_context": inputs.preparation,
        },
    )


def project_preparation_context(value: Any) -> PreparationContext:
    """Project the nested Preparation value for a child workflow input."""

    return ReportingRunContext.model_validate(value).preparation_context


def attach_readiness_preparation(value: Any) -> PreparationContext:
    """Project the Preparation value carried by a readiness result."""

    return EvidenceReadinessState.model_validate(value).preparation_context


def attach_module_results(value: Any) -> dict[str, Any]:
    """Attach typed module submissions returned by the module cohort."""

    if not isinstance(value, Mapping):
        raise TypeError("module result attachment requires an object")
    reporting_state = value.get("reporting_state")
    module_results = value.get("module_results")
    if not isinstance(reporting_state, Mapping):
        raise TypeError("module result attachment requires reporting_state")
    if not isinstance(module_results, Mapping):
        raise TypeError("module result attachment requires module_results")

    submissions: dict[str, ModuleSubmission] = {}
    for result in module_results.values():
        if isinstance(result, Mapping) and "module" in result:
            result = result["module"]
        module = ModuleSubmission.model_validate(result)
        submissions[module.module_id] = module

    attached = deepcopy(dict(reporting_state))
    attached["module_submissions"] = submissions
    return attached


def build_reporting_state(context: ReportingRunContext) -> dict[str, Any]:
    """Project typed Preparation output to the existing stage state shape."""

    preparation = context.preparation_context
    requested_modules = tuple(context.request.target_modules)
    return {
        "run_id": context.run_id,
        "request": context.request,
        "resume": preparation.resume,
        "input_snapshot_ref": preparation.input_snapshot_ref,
        "input_snapshot_digest": preparation.input_snapshot_digest,
        "project_manifest": preparation.project_manifest,
        "preparation_worker_results": preparation.preparation_worker_results,
        "parsed_artifacts": preparation.parsed_artifacts,
        "preparation_parallelism": preparation.preparation_parallelism,
        "evidence_items": preparation.evidence_items,
        "photo_assets": preparation.photo_assets,
        "photo_evidence_adjacency": preparation.photo_evidence_adjacency,
        "mapping_gaps": preparation.mapping_gaps,
        "coverage_matrix": preparation.coverage_matrix,
        "report_taxonomy": preparation.report_taxonomy,
        "special_topic_plan": preparation.special_topic_plan,
        "preparation_refs": preparation.preparation_refs,
        "preparation_completion_ref": preparation.preparation_completion_ref,
        "evidence_index_ref": preparation.evidence_index_ref,
        "source_ledger_ref": preparation.source_ledger_ref,
        "requested_modules": requested_modules,
        "full_report": set(requested_modules) == set(REPORT_MODULE_IDS),
    }


def build_entrypoint_tool_implementations() -> dict[str, Any]:
    """Bind the original three typed entrypoint projections."""

    return {
        "initialize-reporting-run": initialize_reporting_run,
        "attach-reporting-preparation": attach_preparation,
        "build-reporting-state": build_reporting_state,
    }


def build_public_entrypoint_tool_implementations() -> dict[str, Any]:
    """Bind all pure projections used by public file workflow roots."""

    return {
        **build_entrypoint_tool_implementations(),
        "project-preparation-context": project_preparation_context,
        "attach-readiness-preparation": attach_readiness_preparation,
        "attach-module-results": attach_module_results,
    }


def specialize_module_cohort_workflow(
    workflow: WorkflowDefinition,
    target_modules: Iterable[str],
) -> WorkflowDefinition:
    """Specialize the fixed module cohort to the requested module definitions.

    The generic compiler receives this definition before planning.  Unselected
    module branches, Join inputs, reducer inputs, state bindings, and unused
    module tasks are removed from the graph instead of running five lanes and
    discarding their results.
    """

    requested = set(target_modules)
    selected = tuple(module_id for module_id in REPORT_MODULE_IDS if module_id in requested)
    payload = workflow.model_dump(mode="python")
    if selected == tuple(REPORT_MODULE_IDS):
        return workflow.model_copy(deep=True)

    selected_action_ids = {
        "prepare-module-cohort",
        "module-cohort",
        "join-module-cohort",
        "reduce-module-cohort",
        "finish-module-cohort",
    }
    for module_id in selected:
        selected_action_ids.update(
            {
                f"execute-module-{module_id}",
                f"complete-module-{module_id}-branch",
                f"resume-module-{module_id}-after-join",
            }
        )

    actions = [
        action
        for action in payload["actions"]
        if action["id"] in selected_action_ids
    ]
    if len(selected) == 1:
        selected_module = selected[0]
        selected_action_ids.discard("module-cohort")
        selected_action_ids.discard("join-module-cohort")
        selected_action_ids.discard(f"resume-module-{selected_module}-after-join")
        actions = [
            action
            for action in actions
            if action["id"] in selected_action_ids
        ]
    for action in actions:
        if action["id"] == "module-cohort":
            action["branches"] = {
                module_id: f"execute-module-{module_id}"
                for module_id in selected
            }
        elif action["id"] == "join-module-cohort":
            action["inputs"] = {
                module_id: f"outcome-{module_id}" for module_id in selected
            }
        elif action["id"] == "reduce-module-cohort":
            action["input_variables"] = {
                module_id: f"outcome-{module_id}" for module_id in selected
            }
        elif (
            len(selected) == 1
            and action["id"] == f"complete-module-{selected[0]}-branch"
        ):
            action["target"] = "reduce-module-cohort"

    payload["actions"] = actions
    payload["tasks"] = [
        task
        for task in payload.get("tasks", [])
        if not task.startswith("module-")
        or any(task.startswith(f"module-{module_id}-") for module_id in selected)
    ]
    payload["state"] = {
        key: value
        for key, value in payload.get("state", {}).items()
        if not key.startswith("module-id-")
        or key.removeprefix("module-id-") in selected
    }
    payload["id"] = (
        f"{workflow.id}-selected-" + "-".join(module_id.replace(".", "-") for module_id in selected)
    )
    return WorkflowDefinition.model_validate(payload)


__all__ = [
    "attach_module_results",
    "attach_preparation",
    "attach_readiness_preparation",
    "build_entrypoint_tool_implementations",
    "build_public_entrypoint_tool_implementations",
    "build_reporting_state",
    "initialize_reporting_run",
    "project_preparation_context",
    "specialize_module_cohort_workflow",
]
