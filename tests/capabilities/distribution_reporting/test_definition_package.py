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
from manyselves.core.reporting.config import (
    load_agent_definitions,
    load_packaged_agents,
)
from manyselves.core.reporting.models import REPORT_MODULE_IDS
from manyselves.kernel.definitions import DefinitionKind
from manyselves.kernel.executors import build_builtin_executor_registry
from manyselves.kernel.workflow import WorkflowCompiler


def test_distribution_reporting_capability_loads_all_definition_indexes() -> None:
    capability, registry = load_distribution_reporting_capability()

    assert capability.id == "distribution-reporting"
    assert capability.entrypoints == ["distribution-reporting"]
    assert capability.runtime == (
        "manyselves.capabilities.distribution_reporting.adapters.runtime:build_runtime_binding"
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
    assert {definition.id for definition in registry.all(DefinitionKind.WORKFLOW)} == {
        "distribution-chief-chapter-1-lane",
        "distribution-chief-chapter-3-lane",
        "distribution-chief-chapter-4-lane",
        "distribution-chief-chapter-cohort",
        "distribution-chief-chapter-lane",
        "distribution-cross-owner-cohort",
        "distribution-cross-owner-pipeline",
        "distribution-final-chapter-1-lane",
        "distribution-final-chapter-3-lane",
        "distribution-final-chapter-4-lane",
        "distribution-final-chapter-cohort",
        "distribution-final-chapter-lane",
        "distribution-final-chief-revision-1-lane",
        "distribution-final-chief-revision-3-lane",
        "distribution-final-chief-revision-4-lane",
        "distribution-final-chief-revision-cohort",
        "distribution-final-chief-revision-lane",
        "distribution-final-recheck-1-lane",
        "distribution-final-recheck-3-lane",
        "distribution-final-recheck-4-lane",
        "distribution-final-recheck-cohort",
        "distribution-final-recheck-lane",
        "distribution-final-review-cycle",
        "distribution-report-delivery",
        "distribution-module-review-lane",
        "distribution-module-runtime-lane",
        "distribution-module-cohort",
        "distribution-reporting-tail",
        "distribution-reporting",
    }
    assert registry.all(DefinitionKind.TASK)
    assert registry.all(DefinitionKind.CONTRACT)
    assert registry.all(DefinitionKind.TOOL)
    assert "continue-current-cross-owner-pipeline" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert "continue-current-final-review" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert "run-reporting-delivery" not in {
        definition.id for definition in registry.all(DefinitionKind.TOOL)
    }
    assert registry.all(DefinitionKind.RECOVERY)
    assert registry.all(DefinitionKind.GATE) == ()
    assert {definition.id for definition in registry.all(DefinitionKind.INTERACTION)} == {
        "cross-owner-main-exception-decision",
        "module-main-exception-decision",
    }


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
    assert (
        next(action for action in cohort.actions if action["id"] == "module-cohort")[
            "max_concurrency"
        ]
        == 2
    )
    initial_lane = next(action for action in cohort.actions if action["id"] == "execute-module-2.1")
    assert initial_lane["kind"] == "subworkflow"
    assert initial_lane["input_variables"] == {
        "reporting-state": "prepared-module-inputs",
        "module-id": "module-id-2.1",
        "lane-outcomes": "lane-outcomes",
    }
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


def test_production_delivery_is_a_typed_render_publish_completion_subworkflow() -> None:
    registry, _, tail = build_reporting_tail_definition()
    run_delivery = next(action for action in tail.actions if action["id"] == "run-delivery")
    assert run_delivery == {
        "id": "run-delivery",
        "kind": "subworkflow",
        "workflow": "distribution-report-delivery",
        "input_variables": {"reporting-state": "reporting-state"},
        "child_output_name": "result",
        "output_variable": "reporting-state",
    }

    delivery = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-report-delivery",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        delivery,
        registry,
    )

    assert delivery.gates == []
    assert [action.kind for action in plan.actions] == [
        "invoke_tool",
        "invoke_tool",
        "invoke_tool",
        "end_workflow",
    ]
    assert [action.id for action in plan.actions] == [
        "prepare-render-delivery",
        "publish-materialize-delivery",
        "complete-delivery",
        "finish-report-delivery",
    ]
    assert plan.workflow_ids == []
    assert plan.tool_ids == [
        "prepare-render-delivery",
        "publish-materialize-delivery",
        "complete-delivery",
    ]
    assert all(action.kind not in {"if", "parallel", "join", "goto"} for action in plan.actions)

    for tool_id in plan.tool_ids:
        tool = registry.require(DefinitionKind.TOOL, tool_id)
        assert tool.input_contract == "reporting_tail_state"
        assert tool.output_contract == "reporting_tail_state"
        assert tool.side_effect == "ordered_state"
        assert tool.model_visible is False


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
        action["kind"] for action in workflow.actions
    ]
    action_ids = [action.id for action in plan.actions]
    assert action_ids == [action["id"] for action in workflow.actions]
    recheck_start = action_ids.index("prepare-current-module-recheck")
    assert action_ids[recheck_start : recheck_start + 9] == [
        "prepare-current-module-recheck",
        "module-recheck-preflight-needs-revision",
        "choose-module-recheck-preflight",
        "prepare-current-module-recheck-preflight-revision",
        "create-module-recheck-preflight-revision-conversation",
        "invoke-current-module-recheck-preflight-revision",
        "accept-current-module-recheck-preflight-revision",
        "continue-after-module-recheck-preflight-revision",
        "module-recheck-requires-agent",
    ]
    assert plan.tool_ids == [
        "start-current-module-lane",
        "module-lane-retries-preflight-revision",
        "accept-current-module-preflight-revision",
        "module-preflight-revision-needs-recheck",
        "module-lane-has-deferred-main-exception",
        "prepare-current-module-main-exception",
        "module-main-exception-requires-agent",
        "accept-current-module-main-exception",
        "module-main-exception-requests-user",
        "apply-current-module-main-exception-user-input",
        "route-current-module-after-main-exception",
        "prepare-current-module-authoring",
        "module-authoring-requires-agent",
        "accept-current-module-authoring",
        "resume-current-module-authoring",
        "module-lane-can-review",
        "prepare-current-module-review",
        "module-review-preflight-needs-revision",
        "prepare-current-module-preflight-revision",
        "module-review-requires-agent",
        "resume-current-module-review",
        "accept-current-module-review",
        "module-review-needs-recheck",
        "module-review-needs-revision",
        "prepare-current-module-revision",
        "accept-current-module-revision",
        "prepare-current-module-author-exception",
        "prepare-current-module-recheck",
        "module-recheck-requires-agent",
        "resume-current-module-recheck",
        "accept-current-module-recheck",
        "complete-current-module-lane",
    ]
    assert plan.agent_ids == [
        "module-2.1-specialist",
        "main-agent",
        "evidence-auditor",
    ]
    assert plan.task_ids == [
        "module-2.1-runtime-revision",
        "module-runtime-main-exception",
        "module-2.1-authoring",
        "module-runtime-initial-review",
        "module-runtime-recheck",
    ]
    assert plan.interaction_ids == ["module-main-exception-decision"]
    assert (
        next(
            action for action in plan.actions if action.id == "continue-after-module-recheck"
        ).target
        == "module-recheck-exception-is-deferred"
    )
    assert "execute-current-module-lane" not in plan.tool_ids
    assert "review-current-module-lane" not in plan.tool_ids


def test_production_cross_is_an_owner_cohort_subworkflow() -> None:
    """Characterize the next Cross boundary before its implementation exists."""

    registry, _, tail = build_reporting_tail_definition()
    run_cross = next(action for action in tail.actions if action["id"] == "run-cross")
    assert run_cross["kind"] == "subworkflow"
    assert run_cross["workflow"] == "distribution-cross-owner-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == set(REPORT_MODULE_IDS)

    branch_workflows = {}
    for module_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        expected_workflow_id = f"distribution-cross-owner-{module_id}-pipeline"
        assert branch.workflow == expected_workflow_id
        branch_workflows[module_id] = registry.require(
            DefinitionKind.WORKFLOW,
            expected_workflow_id,
        )

    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == set(REPORT_MODULE_IDS)

    for module_id, pipeline in branch_workflows.items():
        pipeline_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            pipeline,
            registry,
        )
        assert pipeline_plan.agent_ids == [
            "cross-module-reviewer",
            f"module-{module_id}-specialist",
            "main-agent",
            "evidence-auditor",
        ]
        assert pipeline_plan.task_ids == [
            "cross-owner-runtime-initial-review",
            f"cross-owner-module-{module_id}-revision-r1",
            "cross-owner-runtime-main-exception",
            "cross-owner-runtime-local-review",
            "cross-owner-runtime-recheck",
        ]
        assert pipeline_plan.tool_ids == [
            "prepare-current-cross-owner-initial",
            "cross-owner-initial-requires-agent",
            "accept-current-cross-owner-initial",
            "cross-owner-initial-has-findings",
            "prepare-current-cross-owner-revision",
            "cross-owner-revision-requires-agent",
            "accept-current-cross-owner-revision",
            "prepare-current-cross-owner-author-exception",
            "cross-owner-main-exception-requires-agent",
            "accept-current-cross-owner-main-exception",
            "cross-owner-main-exception-requests-user",
            "apply-current-cross-owner-main-exception-user-input",
            "cross-owner-author-exception-returns-to-author",
            "prepare-current-cross-owner-local-review",
            "cross-owner-local-review-requires-agent",
            "accept-current-cross-owner-local-review",
            "prepare-current-cross-owner-recheck",
            "cross-owner-recheck-requires-agent",
            "accept-current-cross-owner-recheck",
            "prepare-current-cross-owner-reviewer-exception",
            "advance-current-cross-owner-round",
            "cross-owner-round-needs-revision",
            "complete-current-cross-owner-pipeline",
            "complete-current-cross-owner-without-findings",
        ]
        conversation = next(
            action
            for action in pipeline_plan.actions
            if action.id == "create-cross-owner-conversation"
        )
        assert conversation.conversation_key == f"cross-owner-{module_id}"


def test_production_chief_is_a_chapter_cohort_subworkflow() -> None:
    """Chief chapter Agents and their Join must be owned by files."""

    registry, _, tail = build_reporting_tail_definition()
    run_chief = next(action for action in tail.actions if action["id"] == "run-chief")
    assert run_chief["kind"] == "subworkflow"
    assert run_chief["workflow"] == "distribution-chief-chapter-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-chief-chapter-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == {"1", "3", "4"}
    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == {"1", "3", "4"}

    for chapter_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        workflow_id = f"distribution-chief-chapter-{chapter_id}-lane"
        assert branch.workflow == workflow_id
        lane = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        lane_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            lane,
            registry,
        )
        assert lane_plan.agent_ids == ["chief-editor"]
        assert lane_plan.task_ids == ["chief-chapter-edit"]
        conversation = next(
            action for action in lane_plan.actions if action.kind == "create_conversation"
        )
        assert conversation.conversation_key == f"chief-chapter-{chapter_id}"
        assert "run-reporting-chief" not in lane_plan.tool_ids


def test_production_final_is_a_chapter_auditor_cohort_subworkflow() -> None:
    """Characterize the file-owned Final initial chapter wave."""

    registry, _, tail = build_reporting_tail_definition()
    run_final = next(action for action in tail.actions if action["id"] == "run-final")
    assert run_final["kind"] == "subworkflow"
    assert run_final["workflow"] == "distribution-final-chapter-cohort"

    cohort = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-chapter-cohort",
    )
    cohort_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        cohort,
        registry,
    )
    parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
    assert set(parallel.branches) == {"1", "3", "4"}
    join = next(action for action in cohort_plan.actions if action.kind == "join")
    assert join.parallel == parallel.id
    assert set(join.inputs) == {"1", "3", "4"}

    for chapter_id, branch_id in parallel.branches.items():
        branch = next(action for action in cohort_plan.actions if action.id == branch_id)
        assert branch.kind == "subworkflow"
        workflow_id = f"distribution-final-chapter-{chapter_id}-lane"
        assert branch.workflow == workflow_id
        lane = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        lane_plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            lane,
            registry,
        )
        assert lane_plan.agent_ids == ["chief-editor-auditor"]
        assert lane_plan.task_ids == ["final-chapter-review"]
        conversation = next(
            action for action in lane_plan.actions if action.kind == "create_conversation"
        )
        assert conversation.agent == "chief-editor-auditor"
        assert conversation.conversation_key == f"final-chapter-{chapter_id}"
        invoke = next(action for action in lane_plan.actions if action.kind == "invoke_agent")
        assert invoke.agent == "chief-editor-auditor"
        assert invoke.task == "final-chapter-review"
        assert "run-reporting-final" not in lane_plan.tool_ids

    cycle = next(action for action in cohort_plan.actions if action.id == "run-final-review-cycle")
    assert cycle.kind == "subworkflow"
    assert cycle.workflow == "distribution-final-review-cycle"
    assert cycle.input_variables == {
        "reporting-state": "completed-final-state",
        "initial-outcomes": "final-chapter-outcomes",
    }
    assert all(
        action.kind != "invoke_tool" or action.tool != "continue-current-final-review"
        for action in cohort_plan.actions
    )


def test_production_final_review_is_an_affected_only_revision_recheck_cycle() -> None:
    """Characterize the file-owned Final revision and recheck loop."""

    registry, _, _ = build_reporting_tail_definition()
    compiler = WorkflowCompiler(build_builtin_executor_registry())
    cycle = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-final-review-cycle",
    )
    cycle_plan = compiler.compile(cycle, registry)

    assert cycle.gates == []
    assert cycle.max_iterations is None
    assert cycle_plan.workflow_ids == [
        "distribution-final-chief-revision-cohort",
        "distribution-final-recheck-cohort",
    ]
    assert cycle_plan.tool_ids == [
        "start-final-review-cycle",
        "final-review-needs-round",
        "advance-final-review-round",
        "complete-final-review",
    ]
    entry = next(
        action for action in cycle_plan.actions if action.id == "choose-final-review-entry"
    )
    assert entry.then == "advance-final-review-round"
    assert entry.otherwise == "complete-final-review"
    after_recheck = next(
        action for action in cycle_plan.actions if action.id == "choose-final-review-next-step"
    )
    assert after_recheck.then == "advance-final-review-round"
    assert after_recheck.otherwise == "complete-final-review"

    for cohort_id, parallel_id, lane_prefix, expected_task, expected_agent in [
        (
            "distribution-final-chief-revision-cohort",
            "final-chief-revision-cohort",
            "distribution-final-chief-revision-",
            "final-chief-chapter-revision",
            "chief-editor",
        ),
        (
            "distribution-final-recheck-cohort",
            "final-recheck-cohort",
            "distribution-final-recheck-",
            "final-chapter-recheck",
            "chief-editor-auditor",
        ),
    ]:
        cohort = registry.require(DefinitionKind.WORKFLOW, cohort_id)
        cohort_plan = compiler.compile(cohort, registry)
        parallel = next(action for action in cohort_plan.actions if action.kind == "parallel")
        assert parallel.id == parallel_id
        assert set(parallel.branches) == {"1", "3", "4"}
        join = next(action for action in cohort_plan.actions if action.kind == "join")
        assert join.parallel == parallel.id
        assert set(join.inputs) == {"1", "3", "4"}
        for chapter_id, branch_id in parallel.branches.items():
            branch = next(action for action in cohort_plan.actions if action.id == branch_id)
            assert branch.workflow == f"{lane_prefix}{chapter_id}-lane"
            lane = registry.require(DefinitionKind.WORKFLOW, branch.workflow)
            lane_plan = compiler.compile(lane, registry)
            assert lane_plan.agent_ids == [expected_agent]
            assert lane_plan.task_ids == [expected_task]
            conversation = next(
                action for action in lane_plan.actions if action.kind == "create_conversation"
            )
            expected_conversation = (
                f"chief-chapter-{chapter_id}"
                if expected_agent == "chief-editor"
                else f"final-chapter-{chapter_id}"
            )
            assert conversation.conversation_key == expected_conversation
            assert conversation.agent == expected_agent

        reducer = next(action for action in cohort_plan.actions if action.kind == "invoke_tool")
        assert reducer.tool in {
            "reduce-final-chief-revision-cohort",
            "reduce-final-recheck-cohort",
        }


def test_cross_owner_21_pipeline_declares_initial_reviewer_agent_boundary() -> None:
    _, registry = load_distribution_reporting_capability()
    # The owner specializations are registered by the same loader used by the
    # production Cross compiler; this keeps the characterization on the
    # packaged definition rather than a hand-built test workflow.
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    assert [action.kind for action in plan.actions[:7]] == [
        "invoke_tool",
        "invoke_tool",
        "if",
        "create_conversation",
        "invoke_agent",
        "invoke_tool",
        "goto",
    ]
    assert plan.tool_ids[:3] == [
        "prepare-current-cross-owner-initial",
        "cross-owner-initial-requires-agent",
        "accept-current-cross-owner-initial",
    ]
    assert plan.agent_ids[0] == "cross-module-reviewer"
    assert plan.task_ids[0] == "cross-owner-runtime-initial-review"
    assert "execute-current-cross-owner-pipeline" not in plan.tool_ids

    prepare = plan.actions[0]
    assert prepare.tool == "prepare-current-cross-owner-initial"
    assert prepare.input_variables == {
        "state": "reporting-state",
        "owner_module_id": "owner-module-id",
    }
    route = plan.actions[1]
    assert route.tool == "cross-owner-initial-requires-agent"
    choose = plan.actions[2]
    create = plan.actions[3]
    assert choose.then == create.id
    assert choose.otherwise == "cross-owner-initial-has-findings"
    assert create.agent == "cross-module-reviewer"
    assert create.conversation_key == "cross-owner-2.1"

    invoke = plan.actions[4]
    assert invoke.agent == "cross-module-reviewer"
    assert invoke.task == "cross-owner-runtime-initial-review"
    assert invoke.conversation_variable == "cross-owner-conversation"
    assert invoke.input_variable == "owner-context"
    accept = plan.actions[5]
    assert accept.tool == "accept-current-cross-owner-initial"
    assert accept.input_variables == {
        "context": "owner-context",
        "result": "cross-owner-initial-agent-result",
    }
    assert "continue-current-cross-owner-pipeline" not in {action.id for action in plan.actions}


def test_cross_owner_21_pipeline_declares_owner_finding_revision_boundary() -> None:
    """Characterize the next Cross owner slice before its implementation exists."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "cross-owner-initial-has-findings" in action_ids
    assert "prepare-current-cross-owner-revision" in action_ids
    assert "accept-current-cross-owner-revision" in action_ids
    assert "continue-current-cross-owner-pipeline" not in action_ids

    finding_route = next(
        action for action in plan.actions if action.id == "cross-owner-initial-has-findings"
    )
    assert finding_route.kind == "invoke_tool"
    revision_prepare = next(
        action for action in plan.actions if action.id == "prepare-current-cross-owner-revision"
    )
    assert revision_prepare.kind == "invoke_tool"
    revision_accept = next(
        action for action in plan.actions if action.id == "accept-current-cross-owner-revision"
    )
    assert revision_accept.kind == "invoke_tool"

    revision_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent" and action.agent == "module-2.1-specialist"
    ]
    assert len(revision_agents) == 1
    revision_agent = revision_agents[0]
    assert "cross" in revision_agent.task
    assert "2.1" in revision_agent.task
    revision_conversation = next(
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "module-2.1-specialist"
    )
    assert revision_conversation.conversation_key == "module-2.1"
    assert revision_agent.conversation_variable == revision_conversation.output_variable


def test_cross_owner_21_pipeline_declares_original_auditor_local_regression() -> None:
    """Characterize the Cross revision-to-original-Auditor boundary."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "prepare-current-cross-owner-local-review" in action_ids
    assert "accept-current-cross-owner-local-review" in action_ids
    assert "continue-current-cross-owner-pipeline" not in action_ids

    local_review_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent"
        and action.agent == "evidence-auditor"
        and action.task == "cross-owner-runtime-local-review"
    ]
    assert len(local_review_agents) == 1
    local_review_conversation = next(
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "evidence-auditor"
    )
    assert local_review_conversation.conversation_key == "module-auditor-2.1"
    assert local_review_agents[0].conversation_variable == local_review_conversation.output_variable


def test_cross_owner_21_pipeline_declares_original_reviewer_recheck() -> None:
    """Characterize the first Cross owner recheck Agent boundary."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    action_ids = [action.id for action in plan.actions]
    assert "prepare-current-cross-owner-recheck" in action_ids
    assert "accept-current-cross-owner-recheck" in action_ids
    recheck_agents = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent"
        and action.agent == "cross-module-reviewer"
        and action.task == "cross-owner-runtime-recheck"
    ]
    assert len(recheck_agents) == 1
    recheck_conversation = next(
        action for action in plan.actions if action.id == "create-cross-owner-recheck-conversation"
    )
    assert recheck_conversation.conversation_key == "cross-owner-2.1"
    assert recheck_agents[0].conversation_variable == (recheck_conversation.output_variable)
    local_review_route = next(
        action for action in plan.actions if action.id == "choose-cross-owner-local-review-source"
    )
    assert local_review_route.otherwise == "prepare-current-cross-owner-recheck"


def test_cross_owner_21_pipeline_declares_repeated_recheck_round_route() -> None:
    """Characterize a declarative Cross regression-round loop before implementation."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    assert pipeline.gates == []
    assert plan.max_iterations is None
    assert all(action.kind != "gate" for action in plan.actions)

    action_ids = [action.id for action in plan.actions]
    recheck_continue = next(
        action for action in plan.actions if action.id == "continue-after-cross-owner-recheck"
    )
    assert recheck_continue.kind == "goto"
    assert recheck_continue.target == "prepare-current-cross-owner-reviewer-exception"

    round_advance = next(
        action for action in plan.actions if action.id == "advance-current-cross-owner-round"
    )
    assert round_advance.kind == "invoke_tool"
    assert round_advance.tool == "advance-current-cross-owner-round"
    round_needs_revision = next(
        action for action in plan.actions if action.id == "cross-owner-round-needs-revision"
    )
    assert round_needs_revision.kind == "invoke_tool"
    assert round_needs_revision.tool == "cross-owner-round-needs-revision"
    choose_next_step = next(
        action for action in plan.actions if action.id == "choose-cross-owner-next-step"
    )
    assert choose_next_step.kind == "if"
    assert choose_next_step.then == "prepare-current-cross-owner-revision"
    assert choose_next_step.otherwise == "complete-current-cross-owner-pipeline"
    assert "finish-cross-owner-pipeline" not in {
        choose_next_step.then,
        choose_next_step.otherwise,
    }
    assert action_ids.index(round_advance.id) < action_ids.index(choose_next_step.id)
    assert action_ids.index("prepare-current-cross-owner-revision") < action_ids.index(
        choose_next_step.id
    )


def test_cross_owner_21_pipeline_declares_no_finding_completion() -> None:
    """An ordinary no-finding owner must not enter the compatibility closure."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    choose_revision = next(
        action for action in plan.actions if action.id == "choose-cross-owner-revision"
    )
    assert choose_revision.otherwise == "complete-current-cross-owner-without-findings"
    completion = next(
        action
        for action in plan.actions
        if action.id == "complete-current-cross-owner-without-findings"
    )
    assert completion.kind == "invoke_tool"
    assert completion.tool == "complete-current-cross-owner-without-findings"
    assert completion.output_variable == "owner-outcome"
    assert all(action.kind != "gate" for action in plan.actions)


def test_cross_owner_21_pipeline_advances_recovered_recheck_verdict() -> None:
    """A persisted verdict must rejoin typed round advancement."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    choose_recheck = next(
        action for action in plan.actions if action.id == "choose-cross-owner-recheck-source"
    )
    assert choose_recheck.otherwise == "prepare-current-cross-owner-reviewer-exception"
    assert all(action.kind != "gate" for action in plan.actions)


def test_cross_owner_21_pipeline_declares_main_exception_agent_routes() -> None:
    """Author and reviewer exceptions use Main without a compatibility Tool."""

    _, registry = load_distribution_reporting_capability()
    from manyselves.core.reporting.declarative_cross_owner_cohort import (
        register_cross_owner_pipeline_specializations,
    )

    register_cross_owner_pipeline_specializations(registry)
    pipeline = registry.require(
        DefinitionKind.WORKFLOW,
        "distribution-cross-owner-2.1-pipeline",
    )
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
        pipeline,
        registry,
    )

    main_conversations = [
        action
        for action in plan.actions
        if action.kind == "create_conversation" and action.agent == "main-agent"
    ]
    main_invocations = [
        action
        for action in plan.actions
        if action.kind == "invoke_agent" and action.agent == "main-agent"
    ]
    assert len(main_conversations) == 2
    assert {action.conversation_key for action in main_conversations} == {"main-cross-exception"}
    assert len(main_invocations) == 2
    assert {action.task for action in main_invocations} == {"cross-owner-runtime-main-exception"}
    assert "continue-current-cross-owner-pipeline" not in plan.tool_ids
    assert pipeline.gates == []
    assert plan.max_iterations is None
