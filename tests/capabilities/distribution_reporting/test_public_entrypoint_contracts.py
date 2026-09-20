"""Characterization for the public Distribution Reporting input boundary."""

from manyselves.capabilities.distribution_reporting.runtime.public_entrypoints import (
    load_public_entrypoint_definitions,
    project_public_entrypoint_input,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler
from manyselves.kernel.workflow.models import InvokeToolAction, SubworkflowAction

PUBLIC_ENTRYPOINTS = (
    "full-report",
    "module-report",
    "revise-report",
    "aggregate-existing",
    "render-existing",
    "distill-template-skill",
)


def test_public_entrypoints_are_file_defined_and_compile() -> None:
    capability, registry = load_public_entrypoint_definitions()

    assert capability.entrypoints == list(PUBLIC_ENTRYPOINTS)
    contracts = build_contract_catalog(registry)
    compiler = WorkflowCompiler(build_builtin_executor_registry())
    plans = {}
    for workflow_id in PUBLIC_ENTRYPOINTS:
        workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        assert isinstance(workflow, WorkflowDefinition)
        assert workflow.id == workflow_id
        assert workflow.input_contract is not None
        schema = contracts[workflow.input_contract].json_schema()
        properties = schema.get("properties", {})
        assert "run_id" not in properties
        assert "request" not in properties
        assert "required_part_ids" not in properties
        plans[workflow_id] = compiler.compile(workflow, registry)

    aggregate_project = plans["aggregate-existing"].actions[0]
    assert isinstance(aggregate_project, InvokeToolAction)
    assert aggregate_project.tool == "project-public-aggregate-existing-input"
    aggregate_tail = plans["aggregate-existing"].actions[1]
    assert isinstance(aggregate_tail, SubworkflowAction)
    assert aggregate_tail.workflow == "distribution-aggregate-existing-tail"
    template_project = plans["distill-template-skill"].actions[0]
    assert isinstance(template_project, InvokeToolAction)
    assert template_project.tool == "project-public-template-distillation-input"


def test_public_input_projection_injects_host_run_identity_and_fixed_defaults() -> None:
    full = project_public_entrypoint_input(
        "full-report",
        "run-full",
        {"instruction": "形成完整报告"},
    )
    assert full.operation == "full_report"
    assert full.target_modules == ["2.1", "2.2", "2.3", "2.4", "2.5"]

    aggregate = project_public_entrypoint_input(
        "aggregate-existing",
        "run-aggregate",
        {"instruction": "聚合五个模块"},
    )
    assert aggregate.run_id == "run-aggregate"
    assert aggregate.request.operation == "aggregate_existing"

    template = project_public_entrypoint_input(
        "distill-template-skill",
        "run-template",
        {"template_ref": "Inputs/report-template.docx"},
    )
    assert template.run_id == "run-template"
    assert len(template.required_part_ids) == 14
