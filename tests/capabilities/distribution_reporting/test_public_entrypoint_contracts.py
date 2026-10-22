"""Characterization for the public Distribution Reporting input boundary."""

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.public_entrypoints import (
    project_public_entrypoint_input,
)
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.definitions import DefinitionKind, WorkflowDefinition
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler

PUBLIC_ENTRYPOINTS = (
    "full-report",
    "render-existing",
    "extract-template-skills",
    "triage-writing-lessons",
)


def test_public_entrypoints_are_file_defined_and_compile() -> None:
    capability, registry = load_distribution_reporting_capability()

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


def test_public_input_projection_injects_host_run_identity_and_fixed_defaults() -> None:
    full = project_public_entrypoint_input(
        "full-report",
        "run-full",
        {"instruction": "形成完整报告"},
    )
    assert full.operation == "full_report"
    assert full.target_modules == ["2.1", "2.2", "2.3", "2.4", "2.5"]
