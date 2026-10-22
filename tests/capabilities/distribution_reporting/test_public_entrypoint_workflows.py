"""Characterization for the public file-defined Reporting workflow roots."""

from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.entrypoint_tools import (
    build_public_entrypoint_tool_implementations,
)
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import ResolvedPlan, WorkflowCompiler

WORKFLOW_ROOT = (
    Path(__file__).parents[3]
    / "manyselves"
    / "capabilities"
    / "distribution_reporting"
    / "workflows"
)
ENTRYPOINTS = (
    "full-report",
    "render-existing",
    "extract-template-skills",
    "triage-writing-lessons",
)


def _workflow(workflow_id: str) -> WorkflowDefinition:
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
    assert isinstance(workflow, WorkflowDefinition)
    return workflow


def test_the_capability_declares_only_the_ledger_era_entrypoints() -> None:
    capability, _registry = load_distribution_reporting_capability()
    assert tuple(capability.entrypoints) == ENTRYPOINTS
    for workflow_id in ENTRYPOINTS:
        assert (WORKFLOW_ROOT / f"{workflow_id}.yaml").is_file()


def test_every_workflow_file_is_reached_from_an_entrypoint() -> None:
    """No workflow is left behind once its entrypoint is gone."""

    from manyselves.kernel.workflow import restore_plan_definition_registry

    _capability, registry = load_distribution_reporting_capability()
    reached: set[str] = set()
    for workflow_id in ENTRYPOINTS:
        plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            _workflow(workflow_id), registry
        )
        reached |= {
            definition.id
            for definition in restore_plan_definition_registry(plan).all(
                DefinitionKind.WORKFLOW
            )
        }
    declared = {definition.id for definition in registry.all(DefinitionKind.WORKFLOW)}
    assert declared - reached == set()


def test_public_entrypoint_tools_are_pure_and_file_declared() -> None:
    _capability, registry = load_distribution_reporting_capability()
    implementations = build_public_entrypoint_tool_implementations()

    assert set(implementations) == {
        "initialize-reporting-run",
        "attach-reporting-preparation",
        "build-reporting-state",
        "project-preparation-context",
        "attach-readiness-preparation",
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
        "run-photo-reading",
        "attach-readiness-to-context",
        "build-reporting-state",
        "run-report-ledger",
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
        "distribution-photo-reading",
        "report-ledger",
    ]


@pytest.mark.parametrize("workflow_id", ENTRYPOINTS)
def test_public_root_plans_survive_a_serialization_round_trip(workflow_id: str) -> None:
    """A persisted plan is validated again on start; every action must re-load."""

    _capability, registry = load_distribution_reporting_capability()
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        _workflow(workflow_id), registry
    )

    assert ResolvedPlan.model_validate(plan.model_dump(mode="json")) == plan
    assert ResolvedPlan.model_validate_json(plan.model_dump_json()) == plan
