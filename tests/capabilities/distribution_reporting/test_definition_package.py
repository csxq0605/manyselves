from pathlib import Path

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
    load_reporting_agents,
)
from manyselves.capabilities.distribution_reporting.adapters import (
    build_module_cohort_definition,
    build_module_lane_definitions,
    build_reporting_tail_definition,
)
from manyselves.core.reporting.config import (
    AgentDefinition as ReportingAgentDefinition,
)
from manyselves.core.reporting.config import load_agent_definitions, load_packaged_agents
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler


def test_distribution_reporting_capability_loads_all_definition_indexes() -> None:
    capability, registry = load_distribution_reporting_capability()

    assert capability.id == "distribution-reporting"
    assert capability.entrypoints == ["distribution-reporting"]
    assert capability.runtime == (
        "manyselves.capabilities.distribution_reporting.adapters.runtime:"
        "build_runtime_binding"
    )
    assert {definition.id for definition in registry.all(DefinitionKind.AGENT)} == {
        "chief-editor-auditor",
        "chief-editor",
        "citation-builder",
        "coverage-evaluator",
        "cross-module-reviewer",
        "docx-renderer",
        "evidence-auditor",
        "evidence-normalizer",
        "intake-parser",
        "main-agent",
        "manifest-builder",
        "module-2.1-specialist",
        "module-2.2-specialist",
        "module-2.3-specialist",
        "module-2.4-specialist",
        "module-2.5-specialist",
        "product-skill-maintainer",
        "project-delivery",
        "template-distiller",
    }
    assert {
        definition.id for definition in registry.all(DefinitionKind.WORKFLOW)
    } == {
        "distribution-module-review-lane",
        "distribution-module-runtime-lane",
        "distribution-module-cohort",
        "distribution-reporting-tail",
        "distribution-reporting",
    }
    assert registry.all(DefinitionKind.TASK)
    assert registry.all(DefinitionKind.CONTRACT)
    assert registry.all(DefinitionKind.TOOL)
    assert registry.all(DefinitionKind.RECOVERY)
    assert registry.all(DefinitionKind.GATE) == ()


def test_capability_agents_project_to_the_current_reporting_contract() -> None:
    agents = load_reporting_agents()

    assert agents
    assert all(isinstance(agent, ReportingAgentDefinition) for agent in agents.values())
    assert agents["module-2.1-specialist"].tools == [
        "search_project_evidence",
        "open_project_source",
        "search_reference_library",
        "open_reference",
        "web_search",
        "open_web_source",
        "inspect_document",
        "inspect_image",
        "calculate",
        "publish_research_note",
        "query_peer",
        "reply_peer",
        "report_gap",
        "write_result_part",
        "list_result_parts",
        "report_blocked",
        "submit_result",
    ]
    assert "<role_and_perspective>" in agents["evidence-auditor"].instructions


def test_legacy_packaged_agent_loader_remains_a_compatibility_import() -> None:
    current = load_packaged_agents()
    projected = load_reporting_agents()

    assert current.keys() == projected.keys()
    for agent_id in current:
        assert current[agent_id].model_dump(exclude={"source_path"}) == projected[
            agent_id
        ].model_dump(exclude={"source_path"})


def test_capability_agent_projection_matches_the_legacy_template_corpus() -> None:
    legacy = load_agent_definitions(
        Path(__file__).parents[3] / "manyselves/templates/reporting/agents"
    )
    projected = load_reporting_agents()

    assert legacy.keys() == projected.keys()
    for agent_id in legacy:
        assert legacy[agent_id].model_dump(exclude={"source_path"}) == projected[
            agent_id
        ].model_dump(exclude={"source_path"})


def test_capability_adapters_expose_the_executable_reporting_definitions() -> None:
    _, packaged = load_distribution_reporting_capability()
    packaged_lane = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-review-lane",
    )
    packaged_cohort = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-cohort",
    )
    packaged_tail = packaged.require(
        DefinitionKind.WORKFLOW,
        "distribution-reporting-tail",
    )
    _, _, lane = build_module_lane_definitions("2.1")
    _, _, cohort = build_module_cohort_definition(max_concurrency=2)
    _, _, tail = build_reporting_tail_definition()

    assert packaged_lane.actions
    assert packaged_cohort.actions
    assert packaged_tail.actions
    assert lane.id == "distribution-module-2.1-review-lane"
    assert lane.actions[0]["tool"] == "build-module-initial-review-input"
    assert lane.actions[2]["id"] == "module-2.1-initial-review-r0"
    assert cohort.id == "distribution-module-cohort"
    assert next(
        action for action in cohort.actions if action["id"] == "module-cohort"
    )["max_concurrency"] == 2
    assert next(
        action for action in cohort.actions if action["id"] == "execute-module-2.1"
    )["kind"] == "subworkflow"
    assert tail.id == "distribution-reporting-tail"
    assert tail.actions == packaged_tail.actions


def test_top_level_reporting_workflow_is_an_executable_capability_definition() -> None:
    _, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distribution-reporting")
    workflow = workflow.model_copy(
        deep=True,
        update={
            "state": {
                "reporting-state": {"run_id": "report-characterized"},
                "full-report": True,
            }
        },
    )

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert [action.kind for action in plan.actions] == [
        "subworkflow",
        "if",
        "subworkflow",
        "end_workflow",
    ]
    assert plan.workflow_ids == [
        "distribution-module-cohort",
        "distribution-reporting-tail",
    ]


def test_production_module_runtime_lane_declares_its_lifecycle_steps() -> None:
    registry, _, _ = build_module_cohort_definition(max_concurrency=2)
    workflow = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-module-2.1-runtime-lane",
    )

    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        workflow,
        registry,
    )

    assert [action.kind for action in plan.actions] == [
        "invoke_tool",
        "invoke_tool",
        "invoke_tool",
        "if",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
        "invoke_tool",
        "invoke_tool",
        "if",
        "invoke_tool",
        "invoke_tool",
        "if",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
        "invoke_tool",
        "if",
        "invoke_tool",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
        "invoke_tool",
        "invoke_tool",
        "if",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
        "invoke_tool",
        "invoke_tool",
        "end_workflow",
    ]
    assert plan.tool_ids == [
        "start-current-module-lane",
        "prepare-current-module-authoring",
        "module-authoring-requires-agent",
        "accept-current-module-authoring",
        "resume-current-module-authoring",
        "module-lane-can-review",
        "prepare-current-module-review",
        "module-review-requires-agent",
        "accept-current-module-review",
        "module-review-needs-revision",
        "prepare-current-module-revision",
        "accept-current-module-revision",
        "prepare-current-module-recheck",
        "module-recheck-requires-agent",
        "accept-current-module-recheck",
        "continue-current-module-review",
        "complete-current-module-lane",
    ]
    assert plan.agent_ids == ["module-2.1-specialist", "evidence-auditor"]
    assert plan.task_ids == [
        "module-2.1-authoring",
        "module-runtime-initial-review",
        "module-2.1-runtime-revision",
        "module-runtime-recheck",
    ]
    assert "execute-current-module-lane" not in plan.tool_ids
    assert "review-current-module-lane" not in plan.tool_ids
