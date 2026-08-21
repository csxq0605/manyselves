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
        "distribution-cross-owner-cohort",
        "distribution-cross-owner-pipeline",
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
    assert registry.all(DefinitionKind.RECOVERY)
    assert registry.all(DefinitionKind.GATE) == ()
    assert {
        definition.id for definition in registry.all(DefinitionKind.INTERACTION)
    } == {"cross-owner-main-exception-decision"}


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
    assert (
        next(action for action in cohort.actions if action["id"] == "execute-module-2.1")["kind"]
        == "subworkflow"
    )
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
    assert (
        next(
            action for action in plan.actions if action.id == "continue-after-module-recheck"
        ).target
        == "module-review-needs-revision"
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
    assert "continue-current-cross-owner-pipeline" not in {
        action.id for action in plan.actions
    }


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
        action
        for action in plan.actions
        if action.id == "choose-cross-owner-recheck-source"
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
    assert {action.conversation_key for action in main_conversations} == {
        "main-cross-exception"
    }
    assert len(main_invocations) == 2
    assert {action.task for action in main_invocations} == {
        "cross-owner-runtime-main-exception"
    }
    assert "continue-current-cross-owner-pipeline" not in plan.tool_ids
    assert pipeline.gates == []
    assert plan.max_iterations is None
