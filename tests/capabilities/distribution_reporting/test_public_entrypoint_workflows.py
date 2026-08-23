"""Characterization for the public file-defined Reporting workflow roots."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.entrypoint_tools import (
    attach_module_results,
    build_public_entrypoint_tool_implementations,
    specialize_module_cohort_workflow,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_definitions import (
    register_module_runtime_lane_specializations,
)
from manyselves.core.reporting.declarative_chief_chapter_cohort import (
    register_chief_chapter_lane_specializations,
)
from manyselves.core.reporting.declarative_cross_owner_cohort import (
    register_cross_owner_pipeline_specializations,
)
from manyselves.core.reporting.declarative_final_chapter_cohort import (
    register_final_chapter_lane_specializations,
)
from manyselves.core.reporting.declarative_final_review_cycle import (
    register_final_review_lane_specializations,
)
from manyselves.kernel.definitions import (
    DefinitionKind,
    DefinitionRegistry,
    WorkflowDefinition,
    specialize_workflow,
)
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler
from manyselves.kernel.workflow.compiler import CompilerError

WORKFLOW_ROOT = (
    Path(__file__).parents[3]
    / "manyselves"
    / "capabilities"
    / "distribution_reporting"
    / "workflows"
)


@pytest.mark.parametrize("workflow_id", ("full-report", "module-report"))
def test_public_entrypoint_workflow_file_exists(workflow_id: str) -> None:
    """Both public operation names are backed by file definitions."""

    assert (WORKFLOW_ROOT / f"{workflow_id}.yaml").is_file()


def _workflow(workflow_id: str) -> WorkflowDefinition:
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
    assert isinstance(workflow, WorkflowDefinition)
    return workflow


def test_public_entrypoint_tools_are_pure_and_file_declared() -> None:
    _capability, registry = load_distribution_reporting_capability()
    implementations = build_public_entrypoint_tool_implementations()

    assert set(implementations) == {
        "initialize-reporting-run",
        "attach-reporting-preparation",
        "build-reporting-state",
        "project-preparation-context",
        "attach-readiness-preparation",
        "attach-module-results",
    }
    for tool_id in implementations:
        definition = registry.require(DefinitionKind.TOOL, tool_id)
        assert definition.model_visible is False
        assert definition.side_effect == "pure_read"


def test_full_report_declares_the_complete_root_topology() -> None:
    workflow = _workflow("full-report")
    action_ids = [action["id"] for action in workflow.actions]
    assert action_ids == [
        "initialize-full-report",
        "project-preparation-for-full-report",
        "run-reporting-preparation",
        "attach-reporting-preparation",
        "project-preparation-for-readiness",
        "run-evidence-readiness",
        "attach-readiness-preparation",
        "attach-readiness-to-context",
        "build-reporting-state",
        "run-module-cohort",
        "attach-module-results",
        "run-reporting-tail",
        "publish-full-report",
        "finish-full-report",
    ]
    subworkflows = [
        action["workflow"]
        for action in workflow.actions
        if action["kind"] == "subworkflow"
    ]
    assert subworkflows == [
        "distribution-reporting-preparation",
        "distribution-evidence-readiness",
        "distribution-module-cohort",
        "distribution-reporting-tail",
    ]


def test_module_report_is_module_scoped_and_has_no_tail_stages() -> None:
    workflow = _workflow("module-report")
    subworkflows = [
        action["workflow"]
        for action in workflow.actions
        if action["kind"] == "subworkflow"
    ]
    assert subworkflows == [
        "distribution-reporting-preparation",
        "distribution-evidence-readiness",
        "distribution-module-cohort",
    ]
    assert not any(
        any(stage in workflow_id for stage in ("tail", "cross", "chief", "final", "delivery"))
        for workflow_id in subworkflows
    )
    assert "run-reporting-tail" not in {
        action["id"] for action in workflow.actions
    }


def test_module_cohort_definition_is_specialized_before_compilation() -> None:
    _capability, registry = load_distribution_reporting_capability()
    template = registry.require(DefinitionKind.WORKFLOW, "distribution-module-cohort")
    assert isinstance(template, WorkflowDefinition)

    specialized = specialize_module_cohort_workflow(template, ("2.4", "2.5"))
    action_by_id = {action["id"]: action for action in specialized.actions}
    assert set(action_by_id["module-cohort"]["branches"]) == {"2.4", "2.5"}
    assert set(action_by_id["join-module-cohort"]["inputs"]) == {"2.4", "2.5"}
    assert set(action_by_id["reduce-module-cohort"]["input_variables"]) == {
        "2.4",
        "2.5",
    }
    assert not any("2.1" in action_id for action_id in action_by_id)
    assert "module-id-2.1" not in specialized.state
    assert specialized.id == "distribution-module-cohort-selected-2-4-2-5"


def test_module_result_projection_keeps_typed_submissions() -> None:
    submission = ModuleSubmission(
        module_id="2.4",
        submodule_narratives={
            submodule_id: f"内容 {submodule_id}"
            for submodule_id in REPORT_TAXONOMY["2.4"].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    attached = attach_module_results(
        {
            "reporting_state": {"run_id": "public-module"},
            "module_results": {"2.4": {"module": submission}},
        }
    )
    assert attached["module_submissions"] == {"2.4": submission}


def _registry_with_existing_workflow_specializers() -> DefinitionRegistry:
    _capability, source = load_distribution_reporting_capability()
    registry = DefinitionRegistry()
    for definition in source.all():
        if (
            definition.kind == DefinitionKind.WORKFLOW
            and definition.id == "distribution-module-cohort"
        ):
            continue
        registry.register(definition)
    template = source.require(DefinitionKind.WORKFLOW, "distribution-module-cohort")
    assert isinstance(template, WorkflowDefinition)
    registry.register(specialize_workflow(template, {"max_concurrency": 4}))
    register_module_runtime_lane_specializations(registry)
    register_cross_owner_pipeline_specializations(registry)
    register_chief_chapter_lane_specializations(registry)
    register_final_chapter_lane_specializations(registry)
    register_final_review_lane_specializations(registry)
    return registry


@pytest.mark.parametrize("workflow_id", ("full-report", "module-report"))
def test_public_roots_are_accepted_with_existing_definition_specializers(
    workflow_id: str,
) -> None:
    registry = _registry_with_existing_workflow_specializers()
    workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
    assert isinstance(workflow, WorkflowDefinition)

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert plan.workflow_id == workflow_id
    assert "distribution-reporting-preparation" in plan.workflow_ids
    assert "distribution-evidence-readiness" in plan.workflow_ids
    assert "distribution-module-cohort" in plan.workflow_ids
    if workflow_id == "full-report":
        assert "distribution-reporting-tail" in plan.workflow_ids
    else:
        assert "distribution-reporting-tail" not in plan.workflow_ids


@pytest.mark.parametrize("workflow_id", ("full-report", "module-report"))
def test_public_root_compile_boundary_is_existing_child_specialization(
    workflow_id: str,
) -> None:
    """Do not claim a complete plan while the packaged child is parameterized."""

    workflow = _workflow(workflow_id)
    with pytest.raises(CompilerError, match="max_concurrency"):
        WorkflowCompiler(build_builtin_executor_registry()).compile(
            workflow,
            load_distribution_reporting_capability()[1],
        )
