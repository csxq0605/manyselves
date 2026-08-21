import json
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.agentic_models import (
    AgentRunStatus,
    EditedReportSubmission,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.declarative_cross_owner_cohort import (
    compile_cross_owner_workflows,
)
from manyselves.core.reporting.declarative_module_runtime_lane import (
    DeclarativeModuleAuthoringPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.core.reporting.declarative_reporting_runner import (
    DeclarativeReportWorkflowRunner,
    _assemble_reporting_capability_tools,
    _compile_reporting_runtime,
    _CurrentModuleAuthorInvoker,
    _CurrentModuleStages,
    _reporting_agent_invokers,
    execute_declarative_module_stage,
    resume_declarative_reporting_input,
)
from manyselves.core.reporting.declarative_reporting_tail import (
    build_reporting_tail_definition,
)
from manyselves.core.reporting.declarative_task_binding import bind_declared_task
from manyselves.core.reporting.input_contracts import (
    ModuleRevisionInput,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from manyselves.core.reporting.models import REPORT_MODULE_IDS, ReportRequest
from manyselves.core.reporting.parallel_runtime import LaneCompletion, LaneTaskSpec
from manyselves.core.reporting.review_lifecycle import (
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleReviewPreflightProgress,
    ModuleReviewProgress,
    ModuleRevisionPreparation,
)
from manyselves.core.reporting.service import ReportingRunResult, ReportingService
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.reporting.workflow import (
    ReportingNeedsDecisionError,
    ReportingRunBudget,
    ReportWorkflowRunner,
    _DeliveryContext,
)
from manyselves.core.tools.task_board import TaskBoard
from manyselves.kernel.contracts import ContractValidationError, build_contract_catalog
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    RecoveryPolicyDefinition,
    TaskDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.workflow import (
    ActionExecutionStatus,
    WorkflowCompiler,
    WorkflowState,
    WorkflowStatus,
)
from manyselves.kernel.workflow import (
    apply_action_result as apply_kernel_action_result,
)
from manyselves.runtime.state_store import FileWorkflowStateStore
from manyselves.runtime.tool_adapter import CapabilityToolAdapter
from manyselves.runtime.workflow_host import (
    FileWorkflowEventSink,
    InMemoryWorkflowEventSink,
    WorkflowRuntimeHost,
)


def test_one_parent_runtime_routes_cross_local_review_by_declared_task() -> None:
    module_auditor = SimpleNamespace(name="module-auditor-adapter")
    cross_local_auditor = SimpleNamespace(name="cross-local-auditor-adapter")

    combined = _reporting_agent_invokers(
        {"evidence-auditor": module_auditor},
        {"evidence-auditor": cross_local_auditor},
    )

    routed = combined["evidence-auditor"]
    assert routed._default is module_auditor
    assert routed._task_routes == {
        "cross-owner-runtime-local-review": cross_local_auditor,
    }


def test_reporting_runtime_binds_declared_capability_tools_through_factory() -> None:
    compiled = _compile_reporting_runtime(
        {"run_id": "reporting-tool-binding"},
        full_report=False,
    )
    implementations = {
        "prepare-module-cohort": lambda value: value,
        "reduce-module-cohort": lambda value: value,
    }

    bound = _assemble_reporting_capability_tools(
        compiled.definitions,
        compiled.contracts,
        implementations,
        compiled.cohort_plan,
    )

    assert isinstance(bound["prepare-module-cohort"], CapabilityToolAdapter)
    assert isinstance(bound["reduce-module-cohort"], CapabilityToolAdapter)


def test_reporting_runtime_binds_saved_capability_implementation_before_host() -> None:
    compiled = _compile_reporting_runtime(
        {"run_id": "reporting-invalid-tool-binding"},
        full_report=False,
    )
    original = compiled.definitions.require(
        DefinitionKind.TOOL,
        "prepare-module-cohort",
    )
    compiled.definitions._definitions[DefinitionKind.TOOL][
        "prepare-module-cohort"
    ] = original.model_copy(
        update={
            "implementation": (
                "capability:distribution-reporting:changed-prepare-module-cohort"
            )
        }
    )

    bound = _assemble_reporting_capability_tools(
        compiled.definitions,
        compiled.contracts,
        {
            "prepare-module-cohort": lambda value: value,
            "reduce-module-cohort": lambda value: value,
        },
        compiled.cohort_plan,
    )

    assert bound["prepare-module-cohort"].definition.implementation == (
        "capability:distribution-reporting:prepare-module-cohort"
    )


@pytest.mark.asyncio
async def test_reporting_capability_tool_validates_typed_input_and_output_contracts() -> None:
    compiled = _compile_reporting_runtime(
        {"run_id": "reporting-contract-binding"},
        full_report=False,
    )
    received: list[object] = []

    def invalid_result(value: object) -> str:
        received.append(value)
        return "not-a-boolean"

    bound = _assemble_reporting_capability_tools(
        compiled.definitions,
        compiled.contracts,
        {"module-authoring-requires-agent": invalid_result},
        compiled.subworkflows[
            "distribution-module-2.1-runtime-lane"
        ].model_copy(update={"tool_ids": ["module-authoring-requires-agent"]}),
    )
    adapter = bound["module-authoring-requires-agent"]
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.1",
        workflow_id="distribution-module-2.1-runtime-lane",
        reporting_state={},
        status="ready",
    )

    with pytest.raises(ContractValidationError):
        await adapter.invoke({"module_id": "2.1"}, task_id="invalid-input")
    with pytest.raises(ContractValidationError):
        await adapter.invoke(context, task_id="invalid-output")

    assert received == [context]


@pytest.mark.asyncio
async def test_declarative_runner_does_not_invent_task_recovery_policy(
    tmp_path: Path,
) -> None:
    class SpyTaskBoard:
        def create_task(self, **_kwargs):
            return SimpleNamespace(task_id="task-1")

        def start_task(self, *_args, **_kwargs):
            return None

        def complete_task(self, *_args, **_kwargs):
            return None

        def fail_task(self, *_args, **_kwargs):
            return None

        def block_task(self, *_args, **_kwargs):
            return None

        def cancel_task(self, *_args, **_kwargs):
            return None

    class SpyAgentRunner:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        async def run(self, *_args, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(
                status=AgentRunStatus.COMPLETED,
                payload={"policy_injected": True},
            )

    agent_runner = SpyAgentRunner()
    service = SimpleNamespace(
        workspace=tmp_path,
        agents=load_packaged_agents(),
        task_board=SpyTaskBoard(),
    )
    runner = DeclarativeReportWorkflowRunner(service, agent_runner)
    envelope = TaskEnvelope(
        task_id="module-2.1",
        run_id="run-declarative-policy-boundary",
        agent_id="module-2.1-specialist",
        objective="验证声明式 Task recovery 注入",
        allowed_outputs=["module_submission"],
    )

    payload = await runner._agent(
        envelope.agent_id,
        envelope,
        [],
        "wf-declarative-policy-boundary",
    )

    assert payload == {"policy_injected": True}
    assert len(agent_runner.calls) == 1
    policy = agent_runner.calls[0]["recovery_policy"]
    assert policy is None


@pytest.mark.asyncio
async def test_production_module_agent_wrapper_forwards_recovery_and_session(
    tmp_path: Path,
) -> None:
    _capability, definitions = load_distribution_reporting_capability()
    agent = definitions.require(DefinitionKind.AGENT, "module-2.1-specialist")
    task = definitions.require(DefinitionKind.TASK, "module-2.1-authoring")
    recovery = definitions.require(
        DefinitionKind.RECOVERY,
        "current-reporting-recovery",
    )
    envelope = TaskEnvelope(
        task_id=task.id,
        run_id="run-production-wrapper",
        agent_id=agent.id,
        objective=task.objective,
        allowed_outputs=["module_submission"],
    )
    lane_context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.1",
        workflow_id="workflow-production-wrapper",
        reporting_state={},
        status="author_ready",
        authoring=DeclarativeModuleAuthoringPreparation(
            specialist_id=agent.id,
            envelope=envelope,
            revision=0,
            review=False,
            checkpoint=False,
        ),
    )

    class SpyDeclarativeRunner:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []

        async def _agent(
            self,
            agent_id: str,
            envelope: TaskEnvelope,
            artifacts: list[str],
            workflow_id: str,
            *,
            session_key: str | None = None,
            recovery_policy=None,
            definition_override=None,
        ) -> dict[str, object]:
            self.calls.append(
                {
                    "agent_id": agent_id,
                    "envelope": envelope,
                    "artifacts": artifacts,
                    "workflow_id": workflow_id,
                    "session_key": session_key,
                    "recovery_policy": recovery_policy,
                    "definition_override": definition_override,
                }
            )
            return {"accepted": True}

    spy_runner = SpyDeclarativeRunner()
    failures: list[tuple[str, BaseException]] = []
    wrapper = _CurrentModuleAuthorInvoker(
        spy_runner,
        lambda module_id, error: failures.append((module_id, error)),
    )
    workflow = WorkflowDefinition(
        id="production-wrapper-recovery",
        version="1.0.0",
        description="Exercise a packaged recovery Task through the production wrapper.",
        tasks=[task.id],
        output_contract=task.output_contract,
        state={"input": lane_context.model_dump(mode="json")},
        actions=[
            {
                "id": "create-conversation",
                "kind": "create_conversation",
                "agent": agent.id,
                "conversation_key": "module-auditor-2.1",
                "mode": "run",
                "output_variable": "conversation",
            },
            {
                "id": "invoke-agent",
                "kind": "invoke_agent",
                "agent": agent.id,
                "task": task.id,
                "conversation_variable": "conversation",
                "input_variable": "input",
                "output_variable": "agent-output",
            },
            {
                "id": "finish",
                "kind": "end_workflow",
                "output_variable": "agent-output",
                "output_name": "result",
            },
        ],
    )
    executors = build_builtin_executor_registry()
    plan = WorkflowCompiler(executors).compile(workflow, definitions)
    completed = await WorkflowRuntimeHost(
        executors,
        FileWorkflowStateStore(tmp_path),
        InMemoryWorkflowEventSink(),
    ).execute(
        plan,
        WorkflowState.for_plan("run-production-wrapper", plan),
        RuntimeContext(
            agents={agent.id: wrapper},
            contracts=build_contract_catalog(definitions),
            definitions=definitions,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert failures == []
    assert len(spy_runner.calls) == 1
    assert spy_runner.calls[0]["agent_id"] == agent.id
    assert spy_runner.calls[0]["workflow_id"] == lane_context.workflow_id
    assert spy_runner.calls[0]["session_key"] == "module-auditor-2.1"
    assert spy_runner.calls[0]["recovery_policy"] == recovery
    assert spy_runner.calls[0]["definition_override"].instructions == agent.instructions
    bound_envelope = spy_runner.calls[0]["envelope"]
    assert isinstance(bound_envelope, TaskEnvelope)
    assert bound_envelope.objective == envelope.objective
    assert bound_envelope.allowed_tools == task.tools
    assert completed.conversations["conversation"].key.value == "module-auditor-2.1"


def test_generic_reporting_input_resumes_persisted_plan_without_overwriting_projection(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    definitions, _contracts, _tail = build_reporting_tail_definition()
    _cohort, pipelines = compile_cross_owner_workflows(
        definitions,
        build_builtin_executor_registry(),
    )
    plan = pipelines["distribution-cross-owner-2.1-pipeline"]
    run_id = "report-declarative-generic-input"
    input_id = "request-cross-owner-author-user-input"
    waiting = WorkflowState.for_plan(run_id, plan)
    waiting.status = WorkflowStatus.WAITING
    waiting.actions[input_id].status = ActionExecutionStatus.WAITING
    waiting.waiting_input = {
        "input_id": input_id,
        "interaction_id": "cross-owner-main-exception-decision",
        "contract_id": "declarative_main_exception_user_input",
    }
    store = FileWorkflowStateStore(tmp_path)
    store.save_plan(run_id, plan)
    store.save(waiting)
    projection_path = tmp_path / "Work" / "runs" / run_id / "workflow-state.json"
    projection_path.write_text(
        json.dumps({"run_id": run_id, "status": "waiting_user"}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "manyselves.core.reporting.declarative_reporting_runner._compile_reporting_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("saved input resume must not compile current definitions")
        ),
    )

    resumed = resume_declarative_reporting_input(
        workspace=tmp_path,
        run_id=run_id,
        input_id=input_id,
        values={
            "decision": "accept_dispute",
            "rationale": "User accepted the explicit Cross dispute.",
        },
    )

    assert resumed.status is WorkflowStatus.RUNNING
    assert resumed.next_action_id == input_id
    assert resumed.variables["cross-owner-main-exception-user-input"].decision == (
        "accept_dispute"
    )
    assert FileWorkflowStateStore(tmp_path).load(run_id).model_dump(mode="json") == (
        resumed.model_dump(mode="json")
    )
    assert json.loads(projection_path.read_text(encoding="utf-8")) == {
        "run_id": run_id,
        "status": "waiting_user",
    }


def test_declared_task_policy_supplements_dynamic_envelope() -> None:
    envelope = TaskEnvelope(
        task_id="dynamic-task",
        run_id="run-task-policy",
        agent_id="module-2.1-specialist",
        objective="Keep the runtime-computed module and revision objective.",
        constraints=["dynamic constraint"],
        allowed_outputs=["module_submission"],
        allowed_tools=["legacy-tool"],
    )
    task = TaskDefinition(
        id="saved-task",
        version="1.0.0",
        description="Saved declarative Task policy.",
        agent="module-2.1-specialist",
        objective="Follow the file-declared authoring policy.",
        input_contract="declarative_module_runtime_lane_context",
        output_contract="declarative_module_authoring_agent_result",
        constraints=["declared constraint"],
        tools=["search_text"],
        completion={"required_fields": ["module_id", "submodule_narratives"]},
    )

    bound = bind_declared_task(envelope, task)

    assert bound.objective == envelope.objective
    assert bound.allowed_tools == ["search_text"]
    assert bound.constraints == [
        "Declared task objective: Follow the file-declared authoring policy.",
        (
            "Declared completion contract: "
            '{"required_fields":["module_id","submodule_narratives"]}'
        ),
        "declared constraint",
        "dynamic constraint",
    ]


@pytest.mark.asyncio
async def test_module_invoker_forwards_saved_agent_definition_override() -> None:
    _capability, definitions = load_distribution_reporting_capability()
    current_agent = definitions.require(
        DefinitionKind.AGENT,
        "module-2.1-specialist",
    )
    assert isinstance(current_agent, AgentDefinition)
    saved_agent = current_agent.model_copy(
        update={
            "instructions": "Instructions frozen in the saved plan.",
            "model": "saved-model",
            "limits": {
                **current_agent.limits,
                "max_turns": 13,
                "max_tokens": 4096,
                "effort": "high",
            },
        }
    )
    task = definitions.require(DefinitionKind.TASK, "module-2.1-authoring")
    assert isinstance(task, TaskDefinition)
    envelope = TaskEnvelope(
        task_id=task.id,
        run_id="run-saved-agent",
        agent_id=saved_agent.id,
        objective="Dynamic authoring objective.",
        allowed_outputs=["module_submission"],
    )
    lane_context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.1",
        workflow_id="workflow-saved-agent",
        reporting_state={},
        status="author_ready",
        authoring=DeclarativeModuleAuthoringPreparation(
            specialist_id=saved_agent.id,
            envelope=envelope,
            revision=0,
            review=False,
            checkpoint=False,
        ),
    )

    class SpyDeclarativeRunner:
        def __init__(self) -> None:
            self.definition_override = None

        async def _agent(self, *_args, definition_override=None, **_kwargs):
            self.definition_override = definition_override
            return {"accepted": True}

    runner = SpyDeclarativeRunner()
    outcome = await _CurrentModuleAuthorInvoker(runner, lambda *_args: None).invoke(
        saved_agent,
        task,
        lane_context,
        SimpleNamespace(key=SimpleNamespace(value="module-2.1")),
        task_id=task.id,
    )

    assert outcome.status == "ok"
    override = runner.definition_override
    assert override is not None
    assert override.instructions == "Instructions frozen in the saved plan."
    assert override.model == "saved-model"
    assert override.max_turns == 13
    assert override.max_tokens == 4096
    assert override.effort == "high"


@pytest.mark.asyncio
async def test_declarative_module_stage_runs_the_current_complete_cohort_as_an_adapter(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-module", "completed": []}
    calls: list[tuple[str, ...]] = []

    async def execute_current(requested_modules, current_state, workflow_id) -> None:
        calls.append(tuple(requested_modules))
        current_state["completed"] = list(requested_modules)
        current_state["workflow_id"] = workflow_id

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1", "2.2"),
        state=state,
        workflow_id="full-power-distribution-report:report-declarative-module",
        state_store=FileWorkflowStateStore(tmp_path),
    )

    assert calls == [("2.1", "2.2")]
    assert state["completed"] == ["2.1", "2.2"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.outputs["result"]["run_id"] == "report-declarative-module"
    assert completed.subworkflow_states["run-module-cohort"]["status"] == "completed"
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [
        "report-declarative-module"
    ]


@pytest.mark.asyncio
async def test_declarative_module_stage_restores_saved_plan_without_fresh_compile(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    state = {"run_id": "report-saved-definition-closure", "completed": []}

    async def execute_current(requested_modules, current_state, _workflow_id) -> None:
        current_state["completed"] = list(requested_modules)

    store = FileWorkflowStateStore(tmp_path)
    first = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1",),
        state=state,
        workflow_id="workflow-saved-definition-closure",
        state_store=store,
    )
    assert first.status is WorkflowStatus.COMPLETED
    monkeypatch.setattr(
        "manyselves.core.reporting.declarative_reporting_runner._compile_reporting_runtime",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("saved run must not compile current definitions")
        ),
    )

    resumed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1",),
        state=state,
        workflow_id="workflow-saved-definition-closure",
        state_store=store,
    )

    assert resumed.status is WorkflowStatus.COMPLETED
    assert resumed.run_id == first.run_id


@pytest.mark.asyncio
async def test_declarative_module_stage_resumes_the_same_failed_action(
    tmp_path: Path,
) -> None:
    state = {"run_id": "report-declarative-resume"}
    calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("injected module failure")
        current_state["module_stage"] = "completed"

    store = FileWorkflowStateStore(tmp_path)
    with pytest.raises(RuntimeError, match="injected module failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=("2.1",),
            state=state,
            workflow_id="workflow-resume",
            state_store=store,
        )

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=("2.1",),
        state=state,
        workflow_id="workflow-resume",
        state_store=store,
    )

    assert calls == 2
    assert state["module_stage"] == "completed"
    assert completed.status is WorkflowStatus.COMPLETED


class _EmptyLaneRecovery:
    def load_completed_lanes(self, _stage, _module_ids):
        return {}


class _TestArtifactStore:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace

    def write_json(self, relative: str, payload: object) -> Path:
        path = self.workspace / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path


class _CurrentLaneRunner:
    def __init__(
        self,
        workspace: Path,
        *,
        author_exception_action: str | None = None,
        recheck_escalated: bool = False,
        main_decision: str = "accept_dispute",
        machine_preflight_invalid_once: bool = False,
        machine_preflight_correction_failure_once: bool = False,
        recheck_machine_preflight_invalid_once: bool = False,
    ) -> None:
        self.service = SimpleNamespace(
            workspace=workspace,
            store=_TestArtifactStore(workspace),
        )
        self.calls = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.lifecycle = {module_id: [] for module_id in REPORT_MODULE_IDS}
        self.fail_once = "2.2"
        self.fail_review_once = ""
        self.author_exception_action = author_exception_action
        self.author_exception_calls = 0
        self.recheck_escalated = recheck_escalated
        self.recheck_exception_calls = 0
        self.main_decision = main_decision
        self.main_exception_calls = 0
        self.main_exception_module_id = ""
        self.main_exception_finding_ids: list[str] = []
        self.machine_preflight_invalid_once = machine_preflight_invalid_once
        self.machine_preflight_correction_failure_once = (
            machine_preflight_correction_failure_once
        )
        self.recheck_machine_preflight_invalid_once = (
            recheck_machine_preflight_invalid_once
        )
        self.machine_preflight_attempts = 0
        self.execute_module_lane_calls = 0
        self.review_findings: set[str] = set()
        self.initial_review_preparations: dict[
            str, ModuleInitialReviewPreparation
        ] = {}
        self.recheck_preparations: dict[str, ModuleRecheckPreparation] = {}
        self.module_review_loop_calls = 0
        self.recheck_open_once = False
        self.recheck_rounds = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.revision_numbers = {module_id: 0 for module_id in REPORT_MODULE_IDS}
        self.recheck_verdicts: dict[str, list[str]] = {
            module_id: [] for module_id in REPORT_MODULE_IDS
        }

    def _raise_if_cancel_requested(self, _run_id: str) -> None:
        return None

    def _recovery_store(self, _state: dict) -> _EmptyLaneRecovery:
        return _EmptyLaneRecovery()

    def _recovery_stage_name(self, stage: str) -> str:
        return stage

    def _load_recovery_module_lane(self, *_args):
        return None

    def _start_module_lane_attempt(
        self,
        module_id: str,
        lane_state: dict,
        workflow_id: str,
        _defer_main_exceptions: bool,
        _lane_state_override,
    ) -> SimpleNamespace:
        self.lifecycle[module_id].append("start")
        return SimpleNamespace(
            lane_state=lane_state,
            spec=LaneTaskSpec(
                lane_id=f"module-{module_id}",
                run_id=lane_state["run_id"],
                stage="module",
                module_id=module_id,
            ),
            spec_ref=f"lanes/{module_id}/spec.json",
            lane_attempt_id=f"attempt-{module_id}",
            started_at_ns=1,
            attempt_ref=f"lanes/{module_id}/attempt.json",
        )

    def _prepare_module_authoring(
        self,
        module_id: str,
        lane_state: dict,
        workflow_id: str,
        *,
        review: bool,
        checkpoint: bool,
    ) -> SimpleNamespace:
        self.lifecycle[module_id].append("prepare")
        return SimpleNamespace(
            module_id=module_id,
            state=lane_state,
            workflow_id=workflow_id,
            specialist_id=f"module-{module_id}-specialist",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}",
                run_id=lane_state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"author module {module_id}",
                allowed_outputs=["module_submission"],
            ),
            resumed_payload=lane_state.get("specialist_submissions", {}).get(
                module_id
            ),
            revision=0,
            review=review,
            checkpoint=checkpoint,
        )

    async def _resume_module_authoring(self, context) -> ModuleSubmission | None:
        if context.resumed_payload is not None:
            self.lifecycle[context.module_id].append("resume")
        return context.resumed_payload

    async def _agent(
        self,
        agent_id: str,
        _envelope: TaskEnvelope,
        _artifacts,
        _workflow_id: str,
        *,
        session_key: str | None = None,
        recovery_policy: RecoveryPolicyDefinition | None = None,
        definition_override=None,
    ) -> (
        ModuleSubmission
        | ModuleReviewFindingSubmission
        | ModuleRevisionSubmission
        | WorkflowDecisionSubmission
    ):
        if agent_id == "main-agent":
            self.main_exception_calls += 1
            module_id = self.main_exception_module_id or "2.1"
            self.lifecycle[module_id].append("main:main-module-exception")
            return WorkflowDecisionSubmission(
                decision=self.main_decision,
                rationale="Main accepts the explicit module exception and continues the declared path.",
                finding_ids=list(self.main_exception_finding_ids),
            )
        if agent_id == "evidence-auditor":
            module_id = str(session_key).removeprefix("module-auditor-")
            if "module_review_verdict_submission" in _envelope.allowed_outputs:
                self.lifecycle[module_id].append(f"rechecker:{session_key}")
                target = next(iter(REPORT_TAXONOMY[module_id].submodules))
                finding_id = f"M-{module_id}-initial-r0-1"
                if self.recheck_escalated and self.recheck_exception_calls == 0:
                    self.recheck_exception_calls += 1
                    self.main_exception_module_id = module_id
                    self.main_exception_finding_ids = [finding_id]
                    verdict = "escalate"
                else:
                    verdict = (
                        "open"
                        if self.recheck_open_once and _envelope.revision == 1
                        else "resolved"
                    )
                self.recheck_verdicts[module_id].append(verdict)
                return ModuleReviewVerdictSubmission(
                    coverage={
                        "submodule_ids": [
                            next(iter(REPORT_TAXONOMY[module_id].submodules))
                        ]
                    },
                    verdicts=[
                        ResolutionVerdict(
                            finding_id=f"M-{module_id}-initial-r0-1",
                            verdict=verdict,
                            reason=(
                                "The revised narrative now states the requested operational "
                                "consequence within the assigned scope."
                            ),
                            evidence_refs=[f"modules/{module_id}-r1.json"],
                        )
                    ],
                    new_findings=[],
                )
            self.lifecycle[module_id].append(f"reviewer:{session_key}")
            if module_id == self.fail_review_once:
                self.fail_review_once = ""
                raise RuntimeError("injected declarative review failure")
            findings = []
            if module_id in self.review_findings:
                target = next(iter(REPORT_TAXONOMY[module_id].submodules))
                findings = [
                    ModuleReviewFinding(
                        id=f"M-{module_id}-initial-r0-1",
                        target_submodule_id=target,
                        category="analysis_depth",
                        impact="advisory",
                        observation=(
                            "The current narrative does not explain the operational "
                            "consequence of the observed condition."
                        ),
                        evidence_refs=[f"modules/{module_id}.json"],
                        required_change=(
                            "Add a concise operational consequence within the assigned "
                            "submodule and keep the evidence boundary explicit."
                        ),
                        reviewer_checks=[
                            "The revised submodule states the operational consequence."
                        ],
                    )
                ]
            return ModuleReviewFindingSubmission(
                coverage={
                    "submodule_ids": list(REPORT_TAXONOMY[module_id].submodules)
                },
                findings=findings,
            )
        if "module_revision_submission" in _envelope.allowed_outputs:
            module_id = str(session_key).removeprefix("module-")
            target = next(iter(REPORT_TAXONOMY[module_id].submodules))
            finding_id = f"M-{module_id}-initial-r0-1"
            revision = self.revision_numbers[module_id]
            self.lifecycle[module_id].append(f"revision:{session_key}")
            if self.machine_preflight_correction_failure_once:
                self.machine_preflight_correction_failure_once = False
                raise RuntimeError("injected machine preflight correction failure")
            self.author_exception_calls += 1
            exceptional = (
                self.author_exception_action
                if self.author_exception_calls == 1
                else None
            )
            if exceptional in {"disputed", "needs_input"}:
                self.main_exception_module_id = module_id
                self.main_exception_finding_ids = [finding_id]
            return ModuleRevisionSubmission(
                module_id=module_id,
                base_revision=revision - 1,
                revision=revision,
                submodule_narratives=(
                    {}
                    if exceptional in {"disputed", "needs_input"}
                    else {target: f"{target} revised body with operational consequence"}
                ),
                claims_upsert=[],
                claim_ids_remove=[],
                source_ids=[],
                unresolved_questions=[],
                revision_responses=[
                    RevisionResponse(
                        finding_id=finding_id,
                        action=exceptional or "implemented",
                        summary=(
                            "Added the requested operational consequence while preserving "
                            "the existing evidence boundary."
                            if exceptional not in {"disputed", "needs_input"}
                            else "The author disputes the requested change at the existing evidence boundary."
                        ),
                        changed_target_ids=(
                            [target]
                            if exceptional not in {"disputed", "needs_input"}
                            else []
                        ),
                    )
                ],
            )
        module_id = agent_id.removeprefix("module-").removesuffix("-specialist")
        self.calls[module_id] += 1
        self.lifecycle[module_id].append(f"author:{session_key}")
        if module_id == self.fail_once:
            self.fail_once = ""
            raise RuntimeError("injected declarative lane failure")
        narratives = {
            submodule_id: f"{submodule_id} body"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        }
        if (
            self.machine_preflight_invalid_once
            and self.calls[module_id] == 1
        ):
            target = next(iter(narratives))
            narratives[target] += "\n\n[[APPROVED_MODULE:2.1]]"
        return ModuleSubmission(
            module_id=module_id,
            submodule_narratives=narratives,
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )

    def _accept_module_authoring(
        self,
        context,
        submission: ModuleSubmission,
    ) -> ModuleSubmission:
        self.lifecycle[context.module_id].append("accept")
        context.state.setdefault("specialist_submissions", {})[
            context.module_id
        ] = submission
        return submission

    async def _module_pipeline(
        self,
        module_id: str,
        lane_state: dict,
        _workflow_id: str,
        **_kwargs,
    ) -> ModuleSubmission:
        self.calls[module_id] += 1
        self.lifecycle[module_id].append("author")
        if module_id == self.fail_once:
            self.fail_once = ""
            raise RuntimeError("injected declarative lane failure")
        submission = ModuleSubmission(
            module_id=module_id,
            submodule_narratives={
                submodule_id: f"{submodule_id} body"
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            },
            claims=[],
            source_ids=[],
            unresolved_questions=[],
            revision=0,
        )
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        return submission

    async def _module_review_loop(
        self,
        module_id: str,
        submission: ModuleSubmission,
        lane_state: dict,
        _workflow_id: str,
        **_kwargs,
    ) -> ModuleSubmission:
        self.module_review_loop_calls += 1
        self.lifecycle[module_id].append("review")
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            f"reviews/{module_id}.json"
        )
        return submission

    async def _execute_module_lane(self, *_args, **_kwargs):
        self.execute_module_lane_calls += 1
        raise AssertionError("module exception entered full lane replay")

    async def _prepare_module_initial_review(
        self,
        module_id: str,
        submission: ModuleSubmission,
        lane_state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
    ) -> ModuleInitialReviewPreparation:
        self.lifecycle[module_id].append("review-prepare")
        persisted = self.initial_review_preparations.get(module_id)
        if persisted is not None:
            return persisted
        subject_ref = f"modules/{module_id}.json"
        return ModuleInitialReviewPreparation(
            mode="invoke_agent",
            run_id=lane_state["run_id"],
            module_id=module_id,
            lifecycle_id="initial",
            workflow_id=workflow_id,
            reviewer_session_key=f"module-auditor-{module_id}",
            review_root=f"reviews/{module_id}",
            progress_ref=f"reviews/{module_id}/progress.json",
            review_round=0,
            scope=sorted(initial_scope),
            current=submission,
            subject_ref=subject_ref,
            review_input_ref=f"reviews/{module_id}/input-r0.json",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}-initial-review-r0",
                run_id=lane_state["run_id"],
                agent_id="evidence-auditor",
                objective=f"review module {module_id}",
                allowed_outputs=["module_review_finding_submission"],
            ),
        )

    async def _prepare_module_initial_review_step(
        self,
        module_id: str,
        payload: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
        lifecycle_id: str = "initial",
        preflight_progress: ModuleReviewPreflightProgress | None = None,
    ) -> ModuleInitialReviewPreparation:
        """Expose the one-step preflight boundary used by the next Lane graph."""

        self.lifecycle[module_id].append("preflight-prepare")
        has_machine_marker = any(
            "[[APPROVED_MODULE:" in narrative
            for narrative in payload.submodule_narratives.values()
        )
        if has_machine_marker and preflight_progress is None:
            self.machine_preflight_attempts += 1
            target = next(iter(initial_scope))
            progress = ModuleReviewPreflightProgress(
                current=payload,
                attempts=1,
            )
            return ModuleInitialReviewPreparation(
                mode="preflight_revision",
                run_id=state["run_id"],
                module_id=module_id,
                lifecycle_id=lifecycle_id,
                workflow_id=workflow_id,
                reviewer_session_key=f"module-auditor-{module_id}",
                review_root=f"reviews/{module_id}",
                progress_ref=f"reviews/{module_id}/progress.json",
                review_round=0,
                scope=sorted(initial_scope),
                current=payload,
                subject_ref=f"modules/{module_id}-r{payload.revision}.json",
                validation_ref=f"reviews/{module_id}/preflight.json",
                validation_target_submodule_ids=[target],
                preflight_progress=progress,
                review_input=None,
                envelope=None,
            )
        return await self._prepare_module_initial_review(
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=initial_scope,
        )

    async def _prepare_module_initial_review_preflight_revision(
        self,
        preparation: ModuleInitialReviewPreparation,
        state: dict,
    ) -> ModuleRevisionPreparation:
        module_id = preparation.module_id
        targets = list(preparation.validation_target_submodule_ids)
        revision = preparation.current.revision + 1
        self.revision_numbers[module_id] = revision
        validation_report = ValidationReport(
            validation_protocol_version=2,
            run_id=state["run_id"],
            subject_ref=f"modules/{module_id}-r{preparation.current.revision}.json",
            subject_revision=preparation.current.revision,
            validator="machine-preflight-characterization",
            check_ids=["runtime-control-marker"],
            failures=[
                ValidationFailure(
                    check_id="runtime-control-marker",
                    target_path=targets[0],
                    message="Runtime control marker remains in module narrative.",
                )
            ],
            passed=False,
        )
        revision_input = ModuleRevisionInput(
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=f"modules/{module_id}-r{preparation.current.revision}.json",
            subject=module_content_view(preparation.current, set(targets)),
            target_submodule_ids=targets,
            validation_report_ref=preparation.validation_ref,
            validation_report=validation_report,
        )
        return ModuleRevisionPreparation(
            run_id=state["run_id"],
            module_id=module_id,
            workflow_id=preparation.workflow_id,
            specialist_id=f"module-{module_id}-specialist",
            session_key=f"module-{module_id}",
            subject=preparation.current,
            revision_input=revision_input,
            input_ref=f"reviews/{module_id}/preflight-revision-input.json",
            subject_ref=revision_input.subject_ref,
            revision=revision,
            target_submodule_ids=targets,
            required_finding_ids=[],
            envelope=TaskEnvelope(
                task_id=f"module-revision-r{revision}-{module_id}",
                run_id=state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"correct module {module_id} machine preflight",
                allowed_outputs=["module_revision_submission"],
            ),
        )

    def _accept_module_initial_review_preflight_revision(
        self,
        preparation,
        revision_preparation,
        result: ModuleRevisionSubmission,
        state: dict | None = None,
    ) -> tuple[ModuleSubmission, str]:
        del state
        return self._accept_module_revision(revision_preparation, result)

    def _accept_module_initial_review(
        self,
        preparation: ModuleInitialReviewPreparation,
        result: ModuleReviewFindingSubmission,
        lane_state: dict,
    ) -> ModuleInitialReviewAcceptance:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("review-accept")
        submission = preparation.current
        if result.findings:
            return ModuleInitialReviewAcceptance(
                run_id=preparation.run_id,
                module_id=module_id,
                lifecycle_id="initial",
                reviewer_session_key=preparation.reviewer_session_key,
                subject_ref=str(preparation.subject_ref),
                current=submission,
                findings=list(result.findings),
                finding_refs=[f"reviews/{module_id}/findings-r0.json"],
                next_action="revise",
                progress_ref=preparation.progress_ref,
            )
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            f"reviews/{module_id}.json"
        )
        return ModuleInitialReviewAcceptance(
            run_id=preparation.run_id,
            module_id=module_id,
            lifecycle_id="initial",
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=submission,
            findings=[],
            finding_refs=[f"reviews/{module_id}/findings-r0.json"],
            next_action="completed",
            progress_ref=preparation.progress_ref,
            completion_ref=f"reviews/{module_id}.json",
        )

    def _resume_module_initial_review(
        self,
        preparation: ModuleInitialReviewPreparation,
        state: dict,
    ) -> ModuleInitialReviewAcceptance:
        self.lifecycle[preparation.module_id].append("review-resume")
        progress = preparation.progress
        if progress is None:
            raise AssertionError("fake persisted initial review is not resumable")
        if progress.next_action == "review":
            raise AssertionError(
                "persisted review progress must return to the declared Agent path"
            )
        if progress.next_action not in {"completed", "revise"}:
            raise AssertionError("fake persisted initial review is not resumable")
        completion_ref = None
        if progress.next_action == "completed":
            completion_ref = state.get("module_review_completion_refs", {}).get(
                preparation.module_id
            )
        return ModuleInitialReviewAcceptance(
            run_id=preparation.run_id,
            module_id=preparation.module_id,
            lifecycle_id=preparation.lifecycle_id,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=progress.current,
            findings=list(progress.pending),
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=list(progress.resolved_ids),
            next_action=(
                "completed" if progress.next_action == "completed" else "revise"
            ),
            progress_ref=preparation.progress_ref,
            completion_ref=completion_ref,
        )

    async def _prepare_module_revision(
        self,
        subject: ModuleSubmission,
        state: dict,
        workflow_id: str,
        findings: list[ModuleReviewFinding],
    ) -> ModuleRevisionPreparation:
        module_id = subject.module_id
        targets = sorted({finding.target_submodule_id for finding in findings})
        revision = subject.revision + 1
        self.revision_numbers[module_id] = revision
        revision_input = ModuleRevisionInput(
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=f"modules/{module_id}-r{subject.revision}.json",
            subject=module_content_view(subject, set(targets)),
            target_submodule_ids=targets,
            module_findings=findings,
        )
        return ModuleRevisionPreparation(
            run_id=state["run_id"],
            module_id=module_id,
            workflow_id=workflow_id,
            specialist_id=f"module-{module_id}-specialist",
            session_key=f"module-{module_id}",
            subject=subject,
            revision_input=revision_input,
            input_ref=f"reviews/{module_id}/revision-input.json",
            subject_ref=f"modules/{module_id}-r{subject.revision}.json",
            revision=revision,
            target_submodule_ids=targets,
            required_finding_ids=[finding.id for finding in findings],
            envelope=TaskEnvelope(
                task_id=f"module-revision-r{revision}-{module_id}",
                run_id=state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"revise module {module_id}",
                allowed_outputs=["module_revision_submission"],
            ),
        )

    def _accept_module_revision(
        self,
        preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
    ) -> tuple[ModuleSubmission, str]:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("revision-accept")
        narratives = dict(preparation.subject.submodule_narratives)
        narratives.update(result.submodule_narratives)
        revised = preparation.subject.model_copy(
            update={
                "submodule_narratives": narratives,
                "revision": result.revision,
                "revision_responses": result.revision_responses,
            }
        )
        return revised, f"modules/{module_id}-r{result.revision}.json"

    async def _prepare_module_recheck(
        self,
        module_id: str,
        current: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
        lifecycle_id: str = "initial",
        preflight_progress: ModuleReviewPreflightProgress | None = None,
        author_exception_acceptance=None,
    ) -> ModuleRecheckPreparation:
        del author_exception_acceptance
        self.lifecycle[module_id].append("recheck-prepare")
        persisted = self.recheck_preparations.get(module_id)
        if persisted is not None:
            return persisted
        if preflight_progress is None:
            review_round = self.recheck_rounds[module_id] + 1
            self.recheck_rounds[module_id] = review_round
        else:
            review_round = self.recheck_rounds[module_id]
        target = next(iter(REPORT_TAXONOMY[module_id].submodules))
        finding = ModuleReviewFinding(
            id=f"M-{module_id}-initial-r0-1",
            target_submodule_id=target,
            category="analysis_depth",
            impact="advisory",
            observation=(
                "The current narrative does not explain the operational "
                "consequence of the observed condition."
            ),
            evidence_refs=[f"modules/{module_id}.json"],
            required_change=(
                "Add a concise operational consequence within the assigned "
                "submodule and keep the evidence boundary explicit."
            ),
            reviewer_checks=[
                "The revised submodule states the operational consequence."
            ],
        )
        if self.recheck_machine_preflight_invalid_once and preflight_progress is None:
            return ModuleRecheckPreparation(
                mode="preflight_revision",
                run_id=state["run_id"],
                module_id=module_id,
                lifecycle_id=lifecycle_id,
                workflow_id=workflow_id,
                reviewer_session_key=f"module-auditor-{module_id}",
                review_root=f"reviews/{module_id}",
                progress_ref=f"reviews/{module_id}/progress.json",
                review_round=review_round,
                scope=[target],
                current=current,
                pending=[finding],
                responses=current.revision_responses,
                finding_refs=[f"reviews/{module_id}/findings-r0.json"],
                subject_ref=f"modules/{module_id}-r{current.revision}.json",
                validation_ref=f"reviews/{module_id}/recheck-preflight.json",
                validation_target_submodule_ids=[target],
                preflight_progress=ModuleReviewPreflightProgress(
                    current=current,
                    attempts=1,
                ),
            )
        return ModuleRecheckPreparation(
            mode="invoke_agent",
            run_id=state["run_id"],
            module_id=module_id,
            lifecycle_id=lifecycle_id,
            workflow_id=workflow_id,
            reviewer_session_key=f"module-auditor-{module_id}",
            review_root=f"reviews/{module_id}",
            progress_ref=f"reviews/{module_id}/progress.json",
            review_round=review_round,
            scope=[target],
            current=current,
            pending=[finding],
            responses=current.revision_responses,
            finding_refs=[f"reviews/{module_id}/findings-r0.json"],
            subject_ref=f"modules/{module_id}-r{current.revision}.json",
            envelope=TaskEnvelope(
                task_id=f"module-{module_id}-initial-review-r{review_round}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=f"recheck module {module_id}",
                allowed_outputs=["module_review_verdict_submission"],
                revision=review_round,
            ),
        )

    async def _prepare_module_recheck_preflight_revision(
        self,
        preparation: ModuleRecheckPreparation,
        state: dict,
    ) -> ModuleRevisionPreparation:
        return await self._prepare_module_initial_review_preflight_revision(
            preparation,
            state,
        )

    def _accept_module_recheck_preflight_revision(
        self,
        _preparation: ModuleRecheckPreparation,
        revision_preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
        _state: dict,
    ) -> tuple[ModuleSubmission, str]:
        return self._accept_module_revision(revision_preparation, result)

    async def _accept_module_recheck(
        self,
        preparation: ModuleRecheckPreparation,
        _result: ModuleReviewVerdictSubmission,
        _state: dict,
    ) -> ModuleRecheckAcceptance:
        module_id = preparation.module_id
        self.lifecycle[module_id].append("recheck-accept")
        if self.recheck_open_once and preparation.review_round == 1:
            return ModuleRecheckAcceptance(
                run_id=preparation.run_id,
                module_id=module_id,
                lifecycle_id=preparation.lifecycle_id,
                reviewer_session_key=preparation.reviewer_session_key,
                subject_ref=str(preparation.subject_ref),
                current=preparation.current,
                findings=list(preparation.pending),
                finding_refs=preparation.finding_refs,
                verdict_refs=[f"reviews/{module_id}/verdicts-r1.json"],
                resolved_ids=[],
                next_action="continue_existing",
                progress_ref=preparation.progress_ref,
            )
        return ModuleRecheckAcceptance(
            run_id=preparation.run_id,
            module_id=module_id,
            lifecycle_id=preparation.lifecycle_id,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=preparation.current,
            findings=[],
            finding_refs=preparation.finding_refs,
            verdict_refs=[
                f"reviews/{module_id}/verdicts-r{preparation.review_round}.json"
            ],
            resolved_ids=[f"M-{module_id}-initial-r0-1"],
            next_action="completed",
            progress_ref=preparation.progress_ref,
            completion_ref=f"reviews/{module_id}/completion-r1.json",
        )

    def _resume_module_recheck(
        self,
        preparation: ModuleRecheckPreparation,
        state: dict,
    ) -> ModuleRecheckAcceptance:
        progress = preparation.progress
        if progress is None or progress.next_action != "completed":
            raise AssertionError("fake persisted recheck is not completed")
        completion_ref = state.get("module_review_completion_refs", {}).get(
            preparation.module_id
        )
        if completion_ref is None:
            raise AssertionError("fake persisted recheck has no completion ref")
        return ModuleRecheckAcceptance(
            run_id=preparation.run_id,
            module_id=preparation.module_id,
            lifecycle_id=preparation.lifecycle_id,
            reviewer_session_key=preparation.reviewer_session_key,
            subject_ref=str(preparation.subject_ref),
            current=progress.current,
            findings=[],
            finding_refs=list(progress.finding_refs),
            verdict_refs=list(progress.verdict_refs),
            resolved_ids=list(progress.resolved_ids),
            next_action="completed",
            progress_ref=preparation.progress_ref,
            completion_ref=completion_ref,
        )

    def _complete_module_lane_attempt(
        self,
        context,
        submission: ModuleSubmission,
    ):
        self.lifecycle[context.module_id].append("complete")
        context.lane_state.setdefault("module_submissions", {})[context.module_id] = (
            submission
        )
        context.lane_state.setdefault("specialist_submissions", {})[context.module_id] = (
            submission
        )
        completion = LaneCompletion(
            lane_id=f"module-{context.module_id}",
            run_id=context.lane_state["run_id"],
            stage="module",
            module_id=context.module_id,
            result_ref=f"modules/{context.module_id}.json",
        )
        return (
            submission,
            f"lanes/{context.module_id}.json",
            completion,
            context.lane_state,
        )

    def _fail_module_lane_attempt(self, context, _exc: BaseException) -> None:
        self.lifecycle[context.module_id].append("fail")
        return None

    def _finalize_module_lanes(
        self,
        requested_modules,
        state,
        _workflow_id,
        _results,
        failures,
        _failures_by_module,
    ) -> None:
        if failures:
            raise failures[0]
        state["cohort_finalized"] = list(requested_modules)


def _persisted_module_submission(module_id: str, *, revision: int = 0) -> ModuleSubmission:
    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"{submodule_id} body r{revision}"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=revision,
    )


def _persisted_initial_review_preparation(
    run_id: str,
    module_id: str,
    *,
    next_action: str,
) -> ModuleInitialReviewPreparation:
    target = next(iter(REPORT_TAXONOMY[module_id].submodules))
    current = _persisted_module_submission(module_id)
    pending = (
        [
            ModuleReviewFinding(
                id=f"M-{module_id}-initial-r0-resume",
                target_submodule_id=target,
                category="analysis_depth",
                impact="advisory",
                observation="Persisted finding requires the original author revision.",
                evidence_refs=[f"modules/{module_id}-r0.json"],
                required_change="Add the missing operational consequence.",
                reviewer_checks=["The revised text states the consequence."],
            )
        ]
        if next_action == "revise"
        else []
    )
    progress = ModuleReviewProgress(
        run_id=run_id,
        module_id=module_id,
        next_action=next_action,
        current=current,
        pending=pending,
        responses=[],
        finding_refs=[f"reviews/{module_id}/findings-r0.json"] if pending else [],
        verdict_refs=[],
        resolved_ids=[],
        review_round=0,
        phase="initial",
        scope=[target],
        reviewer_session_key=f"module-auditor-{module_id}",
        last_reviewed_subject_ref=f"modules/{module_id}-r0.json",
    )
    return ModuleInitialReviewPreparation(
        mode="continue_existing",
        run_id=run_id,
        module_id=module_id,
        lifecycle_id="initial",
        workflow_id=f"full-power-distribution-report:{run_id}",
        reviewer_session_key=f"module-auditor-{module_id}",
        review_root=f"reviews/{module_id}",
        progress_ref=f"reviews/{module_id}/progress.json",
        review_round=0,
        scope=[target],
        current=current,
        subject_ref=f"modules/{module_id}-r0.json",
        review_input_ref=None,
        envelope=TaskEnvelope(
            task_id=f"module-{module_id}-initial-review-r0",
            run_id=run_id,
            agent_id="evidence-auditor",
            objective=f"resume review module {module_id}",
            allowed_outputs=["module_review_finding_submission"],
        ),
        progress=progress,
    )


def _persisted_recheck_preparation(
    run_id: str,
    module_id: str,
    *,
    next_action: str,
) -> ModuleRecheckPreparation:
    target = next(iter(REPORT_TAXONOMY[module_id].submodules))
    finding_id = f"M-{module_id}-initial-r0-1"
    finding = ModuleReviewFinding(
        id=finding_id,
        target_submodule_id=target,
        category="analysis_depth",
        impact="advisory",
        observation="Persisted finding requires the original Auditor recheck.",
        evidence_refs=[f"modules/{module_id}-r0.json"],
        required_change="Add the missing operational consequence.",
        reviewer_checks=["The revised text states the consequence."],
    )
    current = _persisted_module_submission(module_id, revision=1)
    pending = [finding] if next_action == "review" else []
    finding_refs = [f"reviews/{module_id}/findings-r0.json"]
    verdict_refs = (
        [f"reviews/{module_id}/verdicts-r1.json"]
        if next_action == "completed"
        else []
    )
    resolved_ids = [finding_id] if next_action == "completed" else []
    progress = ModuleReviewProgress(
        run_id=run_id,
        module_id=module_id,
        next_action=next_action,
        current=current,
        pending=pending,
        responses=list(current.revision_responses),
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=resolved_ids,
        review_round=1,
        phase="recheck",
        scope=[target],
        reviewer_session_key=f"module-auditor-{module_id}",
        last_reviewed_subject_ref=f"modules/{module_id}-r0.json",
    )
    return ModuleRecheckPreparation(
        mode="continue_existing",
        run_id=run_id,
        module_id=module_id,
        lifecycle_id="initial",
        workflow_id=f"full-power-distribution-report:{run_id}",
        reviewer_session_key=f"module-auditor-{module_id}",
        review_root=f"reviews/{module_id}",
        progress_ref=f"reviews/{module_id}/progress.json",
        review_round=1,
        scope=[target],
        current=current,
        pending=pending,
        responses=list(current.revision_responses),
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_ids=resolved_ids,
        last_reviewed_subject_ref=f"modules/{module_id}-r0.json",
        subject_ref=f"modules/{module_id}-r1.json",
        envelope=TaskEnvelope(
            task_id=f"module-{module_id}-initial-review-r1",
            run_id=run_id,
            agent_id="evidence-auditor",
            objective=f"resume recheck module {module_id}",
            allowed_outputs=["module_review_verdict_submission"],
            revision=1,
        ),
        progress=progress,
    )


@pytest.mark.asyncio
async def test_file_defined_completed_initial_review_resume_preserves_result_without_legacy_loop(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-review-resume-completed"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    submission = _persisted_module_submission(module_id)
    completion_ref = f"reviews/{module_id}/completion-r0.json"
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
        "module_review_completion_refs": {module_id: completion_ref},
    }
    runner = _CurrentLaneRunner(tmp_path)
    runner.initial_review_preparations[module_id] = (
        _persisted_initial_review_preparation(
            run_id,
            module_id,
            next_action="completed",
        )
    )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "review-resume",
        "complete",
    ]
    assert state["module_submissions"][module_id] == submission
    assert state["module_review_completion_refs"][module_id] == completion_ref
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_findings_resume_returns_to_declared_revision_and_recheck(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-review-resume-revise"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    submission = _persisted_module_submission(module_id)
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
    }
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.initial_review_preparations[module_id] = (
        _persisted_initial_review_preparation(
            run_id,
            module_id,
            next_action="revise",
        )
    )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "review-resume",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.revision_numbers[module_id] == 1
    assert runner.recheck_verdicts[module_id] == ["resolved"]
    assert state["module_submissions"][module_id].revision == 1
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_review_resume_reuses_reviewer_session_and_declared_agent_path(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-review-resume-review"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    submission = _persisted_module_submission(module_id)
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
    }
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.initial_review_preparations[module_id] = (
        _persisted_initial_review_preparation(
            run_id,
            module_id,
            next_action="review",
        )
    )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert state["module_submissions"][module_id] == submission
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_recheck_resume_returns_to_declared_agent_without_legacy_loop(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-recheck-resume-review"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    submission = _persisted_module_submission(module_id)
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
    }
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.initial_review_preparations[module_id] = (
        _persisted_initial_review_preparation(
            run_id,
            module_id,
            next_action="revise",
        )
    )
    runner.recheck_preparations[module_id] = _persisted_recheck_preparation(
        run_id,
        module_id,
        next_action="review",
    )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "review-resume",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.recheck_verdicts[module_id] == ["resolved"]
    assert state["module_submissions"][module_id].revision == 1
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_file_defined_completed_recheck_resume_preserves_result_refs_without_agent_or_legacy_loop(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-recheck-resume-completed"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    submission = _persisted_module_submission(module_id)
    completion_ref = f"reviews/{module_id}/completion-r1.json"
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
        "module_review_completion_refs": {module_id: completion_ref},
    }
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.initial_review_preparations[module_id] = (
        _persisted_initial_review_preparation(
            run_id,
            module_id,
            next_action="revise",
        )
    )
    runner.recheck_preparations[module_id] = _persisted_recheck_preparation(
        run_id,
        module_id,
        next_action="completed",
    )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0
    assert runner.recheck_verdicts[module_id] == []
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "review-resume",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "complete",
    ]
    assert state["module_submissions"][module_id].revision == 1
    assert state["module_review_completion_refs"][module_id] == completion_ref
    assert completed.status is WorkflowStatus.COMPLETED


@pytest.mark.asyncio
async def test_top_level_runtime_retries_only_the_failed_file_defined_module_branch(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-cohort-retry"
    workflow_id = f"full-power-distribution-report:{run_id}"
    requested = ("2.1", "2.2")
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    store = FileWorkflowStateStore(tmp_path)

    with pytest.raises(RuntimeError, match="injected declarative lane failure"):
        await execute_declarative_module_stage(
            requested_modules=requested,
            state=state,
            workflow_id=workflow_id,
            state_store=store,
            module_runtime=_CurrentModuleStages(
                runner,
                requested,
                state,
                workflow_id,
            ),
        )

    completed = await execute_declarative_module_stage(
        requested_modules=requested,
        state=state,
        workflow_id=workflow_id,
        state_store=store,
        module_runtime=_CurrentModuleStages(
            runner,
            requested,
            state,
            workflow_id,
        ),
    )

    assert runner.calls["2.1"] == 1
    assert runner.calls["2.2"] == 2
    assert runner.lifecycle["2.1"] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert runner.lifecycle["2.2"] == [
        "start",
        "prepare",
        "author:specialist-2.2",
        "fail",
        "start",
        "prepare",
        "author:specialist-2.2",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.2",
        "review-accept",
        "complete",
    ]
    assert all(runner.calls[module_id] == 0 for module_id in REPORT_MODULE_IDS[2:])
    assert state["cohort_finalized"] == ["2.1", "2.2"]
    assert completed.subworkflow_states["run-module-cohort"]["status"] == "completed"
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_module_author_reuses_same_run_submission_without_agent(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-author-resume"
    module_id = "2.1"
    submission = ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"{submodule_id} body"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )
    state = {
        "run_id": run_id,
        "resume": True,
        "specialist_submissions": {module_id: submission},
    }
    runner = _CurrentLaneRunner(tmp_path)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            f"full-power-distribution-report:{run_id}",
        ),
    )

    assert runner.calls[module_id] == 0
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "resume",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert state["module_submissions"][module_id] == submission
    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_initial_reviewer_failure_drains_and_retries_its_lane(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-review-retry"
    requested = ("2.1", "2.2")
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.fail_review_once = "2.2"
    store = FileWorkflowStateStore(tmp_path)

    with pytest.raises(RuntimeError, match="injected declarative review failure"):
        await execute_declarative_module_stage(
            requested_modules=requested,
            state=state,
            workflow_id=workflow_id,
            state_store=store,
            module_runtime=_CurrentModuleStages(
                runner,
                requested,
                state,
                workflow_id,
            ),
        )

    completed = await execute_declarative_module_stage(
        requested_modules=requested,
        state=state,
        workflow_id=workflow_id,
        state_store=store,
        module_runtime=_CurrentModuleStages(
            runner,
            requested,
            state,
            workflow_id,
        ),
    )

    assert runner.lifecycle["2.1"].count("complete") == 1
    assert runner.lifecycle["2.1"].count("reviewer:module-auditor-2.1") == 1
    assert runner.lifecycle["2.2"].count("fail") == 1
    assert runner.lifecycle["2.2"].count("reviewer:module-auditor-2.2") == 2
    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_initial_finding_invokes_original_author_revision_once(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-first-revision"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.review_findings.add(module_id)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.calls[module_id] == 1
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.revision_numbers[module_id] == 1
    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_module_recheck_revises_again_before_completion(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-recheck-revision"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(tmp_path)
    runner.fail_once = ""
    runner.review_findings.add(module_id)
    runner.recheck_open_once = True

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.lifecycle[module_id].count("revision:module-2.1") == 2
    assert runner.lifecycle[module_id].count("rechecker:module-auditor-2.1") == 2
    assert runner.lifecycle[module_id].count("reviewer:module-auditor-2.1") == 1
    assert runner.recheck_verdicts[module_id] == ["open", "resolved"]
    assert "review" not in runner.lifecycle[module_id]
    assert runner.revision_numbers[module_id] == 2
    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_module_author_exception_main_accepts_then_original_auditor_rechecks_without_lane_replay(
    tmp_path: Path,
) -> None:
    """A disputed Author response enters declared Main, then the same Auditor."""

    run_id = "report-declarative-module-author-exception"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(
        tmp_path,
        author_exception_action="disputed",
    )
    runner.fail_once = ""
    runner.review_findings.add(module_id)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.main_exception_calls == 1
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "main:main-module-exception",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "complete",
    ]
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_module_recheck_escalation_main_accepts_then_completes_without_lane_replay(
    tmp_path: Path,
) -> None:
    """An escalated module recheck enters declared Main and closes the Lane."""

    run_id = "report-declarative-module-recheck-escalation"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(
        tmp_path,
        recheck_escalated=True,
    )
    runner.fail_once = ""
    runner.review_findings.add(module_id)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.main_exception_calls == 1
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "revision:module-2.1",
        "revision-accept",
        "recheck-prepare",
        "rechecker:module-auditor-2.1",
        "recheck-accept",
        "main:main-module-exception",
        "complete",
    ]
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


def test_file_defined_module_machine_preflight_routes_correction_before_auditor(
) -> None:
    """The 2.1 Lane must declare machine correction before paid review."""

    compiled = _compile_reporting_runtime(
        {"run_id": "report-declarative-module-preflight-definition"},
        full_report=True,
    )
    plan = compiled.subworkflows["distribution-module-2.1-runtime-lane"]
    actions = [action.model_dump(mode="json") for action in plan.actions]
    action_ids = [action["id"] for action in actions]

    def position(action_id: str) -> int:
        return action_ids.index(action_id)

    assert "module-review-preflight-needs-revision" in action_ids
    assert position("prepare-current-module-review") < position(
        "module-review-preflight-needs-revision"
    )
    assert "prepare-current-module-preflight-revision" in action_ids
    assert "create-module-preflight-revision-conversation" in action_ids
    assert "invoke-current-module-preflight-revision" in action_ids
    assert "accept-current-module-preflight-revision" in action_ids
    assert position("module-review-preflight-needs-revision") < position(
        "prepare-current-module-preflight-revision"
    )
    assert position("prepare-current-module-preflight-revision") < position(
        "invoke-current-module-preflight-revision"
    )
    assert position("prepare-current-module-review") < position(
        "accept-current-module-preflight-revision"
    )
    assert position("accept-current-module-preflight-revision") < position(
        "invoke-current-module-reviewer"
    )
    assert any(
        action.get("kind") == "goto"
        and action.get("target") == "prepare-current-module-review"
        for action in actions
    )

    revision_agent = next(
        action
        for action in actions
        if action["id"] == "invoke-current-module-preflight-revision"
    )
    assert revision_agent["agent"] == "module-2.1-specialist"
    assert revision_agent["task"] == "module-2.1-runtime-revision"
    revision_conversation = next(
        action
        for action in actions
        if action["id"] == "create-module-preflight-revision-conversation"
    )
    assert revision_conversation["conversation_key"] == "module-2.1"

    reviewer_agent = next(
        action
        for action in actions
        if action["id"] == "invoke-current-module-reviewer"
    )
    assert reviewer_agent["agent"] == "evidence-auditor"
    assert reviewer_agent["task"] == "module-runtime-initial-review"
    reviewer_conversation = next(
        action
        for action in actions
        if action["id"] == "create-module-reviewer-conversation"
    )
    assert reviewer_conversation["conversation_key"] == "module-auditor-2.1"
    assert "continue-current-module-recheck" not in action_ids
    assert "continue-after-current-module-recheck" not in action_ids
    assert "continue-current-module-recheck" not in {
        action.get("tool") for action in actions
    }
    assert "review-current-module-lane" not in {
        action.get("tool") for action in actions
    }


@pytest.mark.asyncio
async def test_file_defined_machine_preflight_correction_calls_declared_author_before_auditor(
    tmp_path: Path,
) -> None:
    """A failed machine check is corrected before the original Auditor runs."""

    run_id = "report-declarative-module-preflight"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(
        tmp_path,
        machine_preflight_invalid_once=True,
    )
    runner.fail_once = ""

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.machine_preflight_attempts == 1
    assert runner.calls[module_id] == 1
    assert runner.lifecycle[module_id] == [
        "start",
        "prepare",
        "author:specialist-2.1",
        "accept",
        "preflight-prepare",
        "revision:module-2.1",
        "revision-accept",
        "preflight-prepare",
        "review-prepare",
        "reviewer:module-auditor-2.1",
        "review-accept",
        "complete",
    ]
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_recheck_preflight_correction_returns_to_original_auditor(
    tmp_path: Path,
) -> None:
    """A recheck machine failure uses the original Author, then original Auditor."""

    run_id = "report-declarative-module-recheck-preflight"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(
        tmp_path,
        recheck_machine_preflight_invalid_once=True,
    )
    runner.fail_once = ""
    runner.review_findings.add(module_id)

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=FileWorkflowStateStore(tmp_path),
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.calls[module_id] == 1
    assert runner.lifecycle[module_id].count("reviewer:module-auditor-2.1") == 1
    assert runner.lifecycle[module_id].count("rechecker:module-auditor-2.1") == 1
    assert runner.lifecycle[module_id].count("revision:module-2.1") == 2
    assert runner.lifecycle[module_id].count("recheck-prepare") == 2
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


@pytest.mark.asyncio
async def test_file_defined_machine_preflight_correction_retry_reuses_initial_author(
    tmp_path: Path,
) -> None:
    """A failed correction retries that declared Author turn only."""

    run_id = "report-declarative-module-preflight-retry"
    module_id = "2.1"
    workflow_id = f"full-power-distribution-report:{run_id}"
    state = {"run_id": run_id}
    runner = _CurrentLaneRunner(
        tmp_path,
        machine_preflight_invalid_once=True,
        machine_preflight_correction_failure_once=True,
    )
    runner.fail_once = ""
    store = FileWorkflowStateStore(tmp_path)
    with pytest.raises(
        RuntimeError,
        match="injected machine preflight correction failure",
    ):
        await execute_declarative_module_stage(
            requested_modules=(module_id,),
            state=state,
            workflow_id=workflow_id,
            state_store=store,
            module_runtime=_CurrentModuleStages(
                runner,
                (module_id,),
                state,
                workflow_id,
            ),
        )

    completed = await execute_declarative_module_stage(
        requested_modules=(module_id,),
        state=state,
        workflow_id=workflow_id,
        state_store=store,
        module_runtime=_CurrentModuleStages(
            runner,
            (module_id,),
            state,
            workflow_id,
        ),
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert runner.calls[module_id] == 1
    assert runner.lifecycle[module_id].count("author:specialist-2.1") == 1
    assert runner.lifecycle[module_id].count("revision:module-2.1") == 2
    assert runner.lifecycle[module_id].count("reviewer:module-auditor-2.1") == 1
    assert runner.module_review_loop_calls == 0
    assert runner.execute_module_lane_calls == 0


class _TopLevelTailRunner:
    def __init__(
        self,
        *,
        fail_stage: str | None = None,
        trace: list[str] | None = None,
    ) -> None:
        self.calls: list[str] = []
        self.fail_stage = fail_stage
        self.trace = trace

    def _trace(self, event: str) -> None:
        if self.trace is not None:
            self.trace.append(event)

    def _fail(self, stage: str) -> None:
        if self.fail_stage == stage:
            raise RuntimeError(f"injected {stage} failure")

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        self.calls.append("cross")
        self._trace("cross-effect")
        self._fail("cross")
        state["cross_review_completion_ref"] = f"{workflow_id}/cross.json"

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        self.calls.append("chief")
        self._trace("chief-effect")
        self._fail("chief")
        state["chief_candidate_ref"] = f"{workflow_id}/chief.json"
        state["chief_editor_session_key"] = "chief-editor"
        state["approved_module_text"] = {"2.1": "approved"}
        state["edited_report"] = EditedReportSubmission(
            title="Report",
            assessment_background="background",
            findings_overview="overview",
            regional_executive_summary="summary",
            module_narratives={
                module_id: f"module {module_id}"
                for module_id in REPORT_TAXONOMY
            },
            risk_panorama="panorama",
            dimension_risk_analysis="risk analysis",
            data_gap_analysis="gaps",
            improvement_action_plan="actions",
        )

    def _prepare_and_render_delivery(self, state: dict) -> _DeliveryContext:
        self.calls.append("prepare")
        self._trace("delivery-prepare")
        self._fail("prepare")
        return _top_level_delivery_context(state)

    def _publish_and_materialize_delivery(
        self,
        context: _DeliveryContext,
    ) -> _DeliveryContext:
        self.calls.append("publish")
        self._trace("delivery-publish")
        self._fail("publish")
        return context

    def _complete_delivery(self, context: _DeliveryContext) -> None:
        self.calls.append("complete")
        self._trace("delivery-complete")
        self._fail("complete")
        context.state["delivery_completion_ref"] = "delivery.json"


def _top_level_delivery_context(state: dict) -> _DeliveryContext:
    root = Path("Work") / "runs" / str(state["run_id"])
    return _DeliveryContext(
        state=state,
        final_audit_snapshot_ref=f"{root}/final-audit.json",
        claim_ledger_path=root / "claims.json",
        source_ledger_path=root / "sources.json",
        evidence_snapshot_path=root / "evidence.jsonl",
        approved_module_paths={},
        edited_submission_path=root / "edited.json",
        request_snapshot_path=root / "request.json",
        photo_manifest_path=root / "photos.json",
        delivery_markdown="# Report",
        report_state_path=root / "report-state.json",
        markdown_path=root / "report.md",
        source_index_markdown="# Sources",
        source_index_path=root / "sources.md",
        source_index_docx_path=root / "sources.docx",
        template_snapshot=root / "template.docx",
        template_provenance_path=root / "template.json",
        output=root / "report.docx",
        render_result_ref=root / "render-result.json",
    )


@pytest.mark.asyncio
async def test_top_level_runtime_nests_the_file_defined_tail_in_one_run(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-one-state"
    state = {"run_id": run_id}
    tail = _TopLevelTailRunner()

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        tail_runner=tail,
        event_sink=FileWorkflowEventSink(tmp_path),
    )

    assert tail.calls == ["cross", "chief", "prepare", "publish", "complete"]
    assert state["delivery_completion_ref"] == "delivery.json"
    assert completed.status is WorkflowStatus.COMPLETED
    assert completed.subworkflow_states["run-reporting-tail"]["status"] == "completed"
    assert [path.name for path in (tmp_path / "Work" / "runs").iterdir()] == [run_id]
    events = [
        json.loads(line)
        for line in (
            tmp_path / "Work" / "runs" / run_id / "workflow-events.jsonl"
        ).read_text(encoding="utf-8").splitlines()
    ]
    assert [
        (event["kind"], event["action_id"])
        for event in events
        if event["workflow_id"] == "distribution-reporting-tail"
    ] == [
        ("workflow.started", None),
        ("action.started", "run-cross"),
        ("action.completed", "run-cross"),
        ("action.started", "run-chief"),
        ("action.completed", "run-chief"),
        ("action.started", "run-final"),
        ("action.completed", "run-final"),
        ("action.started", "run-delivery"),
        ("action.completed", "run-delivery"),
        ("action.started", "finish-reporting-tail"),
        ("action.completed", "finish-reporting-tail"),
        ("workflow.completed", None),
    ]
    assert [
        (event["kind"], event["action_id"])
        for event in events
        if event["workflow_id"] == "distribution-report-delivery"
    ] == [
        ("workflow.started", None),
        ("action.started", "prepare-render-delivery"),
        ("tool.invoked", "prepare-render-delivery"),
        ("action.completed", "prepare-render-delivery"),
        ("action.started", "publish-materialize-delivery"),
        ("tool.invoked", "publish-materialize-delivery"),
        ("action.completed", "publish-materialize-delivery"),
        ("action.started", "complete-delivery"),
        ("tool.invoked", "complete-delivery"),
        ("action.completed", "complete-delivery"),
        ("action.started", "finish-report-delivery"),
        ("action.completed", "finish-report-delivery"),
        ("workflow.completed", None),
    ]


@pytest.mark.asyncio
async def test_declarative_stage_boundaries_precede_next_stage_effects(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-stage-boundaries"
    trace: list[str] = []
    state = {"run_id": run_id}
    tail = _TopLevelTailRunner(trace=trace)

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        trace.append("module-effect")
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    async def stage_boundary(
        _current_state: dict,
        completed_stage: str,
        next_stage: str,
    ) -> None:
        trace.append(f"boundary:{completed_stage}->{next_stage}")

    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=FileWorkflowStateStore(tmp_path),
        tail_runner=tail,
        stage_boundary=stage_boundary,
    )

    assert completed.status is WorkflowStatus.COMPLETED
    assert trace == [
        "module-effect",
        "boundary:module-work->cross-module-review",
        "cross-effect",
        "boundary:cross-module-review->chief-edit",
        "chief-effect",
        "boundary:chief-edit->chief-editor-audit",
        "boundary:chief-editor-audit->delivery",
        "delivery-prepare",
        "delivery-publish",
        "delivery-complete",
    ]


@pytest.mark.asyncio
async def test_declarative_stage_failure_does_not_emit_future_boundaries(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-boundary-failure"
    trace: list[str] = []
    state = {"run_id": run_id}
    tail = _TopLevelTailRunner(fail_stage="cross", trace=trace)

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        trace.append("module-effect")
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    async def stage_boundary(
        _current_state: dict,
        completed_stage: str,
        next_stage: str,
    ) -> None:
        trace.append(f"boundary:{completed_stage}->{next_stage}")

    with pytest.raises(RuntimeError, match="injected cross failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=tuple(REPORT_MODULE_IDS),
            state=state,
            workflow_id=f"full-power-distribution-report:{run_id}",
            state_store=FileWorkflowStateStore(tmp_path),
            tail_runner=tail,
            stage_boundary=stage_boundary,
        )

    assert trace == [
        "module-effect",
        "boundary:module-work->cross-module-review",
        "cross-effect",
    ]


@pytest.mark.asyncio
async def test_declarative_runner_deduplicates_parent_stage_boundary() -> None:
    runner = DeclarativeReportWorkflowRunner.__new__(DeclarativeReportWorkflowRunner)
    runner._budget = None
    checkpoints: list[tuple[str, str]] = []
    runner._checkpoint = lambda _state, activity, status: checkpoints.append(
        (activity, status)
    )
    runner._raise_if_cancel_requested = lambda _run_id: None
    state = {"run_id": "report-declarative-boundary-dedup"}

    await runner._declarative_stage_boundary(
        state,
        "module-work",
        "cross-module-review",
    )
    await runner._checkpoint_then_cost_boundary(
        state,
        "module-work",
        "completed",
        "module-work",
        "cross-module-review",
    )

    assert checkpoints == [("module-work", "completed")]
    assert state[runner._BOUNDARIES_KEY] == [
        "module-work->cross-module-review"
    ]


@pytest.mark.asyncio
async def test_declarative_boundary_pause_keeps_durable_marker_for_same_run_resume(
    tmp_path: Path,
) -> None:
    run_id = "report-declarative-boundary-pause"
    runner = DeclarativeReportWorkflowRunner.__new__(DeclarativeReportWorkflowRunner)
    runner._budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=0,
        max_tokens=0,
        mode="pause_at_boundary",
    )
    checkpoints: list[dict] = []
    runner._checkpoint = lambda current, *_args: checkpoints.append(
        deepcopy(current)
    )
    runner._raise_if_cancel_requested = lambda _run_id: None
    state = {"run_id": run_id}

    with pytest.raises(ReportingNeedsDecisionError):
        await runner._declarative_stage_boundary(
            state,
            "module-work",
            "cross-module-review",
        )

    marker = "module-work->cross-module-review"
    assert checkpoints[0][runner._BOUNDARIES_KEY] == [marker]
    assert checkpoints[0]["pending_cost_boundary_id"]
    assert runner._budget.pending_boundary() is None
    assert runner._budget.snapshot()["cost_control"]["pending_decision"][
        "completed_stage"
    ] == "module-work"

    resumed_budget = ReportingRunBudget(
        tmp_path,
        run_id,
        max_attempts=1,
        max_tokens=1,
        mode="pause_at_boundary",
    )
    assert resumed_budget.resolve_resume() is True
    runner._budget = resumed_budget
    await runner._declarative_stage_boundary(
        checkpoints[0],
        "module-work",
        "cross-module-review",
    )

    assert len(checkpoints) == 1


@pytest.mark.asyncio
async def test_top_level_runtime_resumes_inside_the_failed_tail_subworkflow(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_id = "report-declarative-tail-resume"
    state = {"run_id": run_id}
    module_calls = 0

    async def execute_current(_modules, current_state, _workflow_id) -> None:
        nonlocal module_calls
        module_calls += 1
        current_state["module_submissions"] = {
            module_id: ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: f"{submodule_id} body"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[],
                source_ids=[],
                unresolved_questions=[],
                revision=0,
            )
            for module_id in REPORT_MODULE_IDS
        }

    store = FileWorkflowStateStore(tmp_path)
    failing_tail = _TopLevelTailRunner(fail_stage="chief")
    with pytest.raises(RuntimeError, match="injected chief failure"):
        await execute_declarative_module_stage(
            execute_current=execute_current,
            requested_modules=tuple(REPORT_MODULE_IDS),
            state=state,
            workflow_id=f"full-power-distribution-report:{run_id}",
            state_store=store,
            tail_runner=failing_tail,
        )

    failed = store.load(run_id)
    child = failed.subworkflow_states["run-reporting-tail"]
    assert child["actions"]["run-cross"]["status"] == "completed"
    assert child["actions"]["run-chief"]["status"] == "failed"

    patched_sources: list[tuple[WorkflowState, dict]] = []

    def apply_action_result_spy(current: WorkflowState, result) -> WorkflowState:
        patched_sources.append((current, current.model_dump(mode="json")))
        return apply_kernel_action_result(current, result)

    monkeypatch.setattr(
        "manyselves.core.reporting.declarative_reporting_runner.apply_action_result",
        apply_action_result_spy,
    )

    resumed_tail = _TopLevelTailRunner()
    completed = await execute_declarative_module_stage(
        execute_current=execute_current,
        requested_modules=tuple(REPORT_MODULE_IDS),
        state=state,
        workflow_id=f"full-power-distribution-report:{run_id}",
        state_store=store,
        tail_runner=resumed_tail,
    )

    assert module_calls == 1
    assert failing_tail.calls == ["cross", "chief"]
    assert resumed_tail.calls == ["chief", "prepare", "publish", "complete"]
    assert completed.status is WorkflowStatus.COMPLETED
    assert patched_sources
    assert all(
        source.model_dump(mode="json") == snapshot
        for source, snapshot in patched_sources
    )


class _NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("runner selection must not call the Provider")


@pytest.mark.asyncio
async def test_reporting_service_selects_runner_from_run_identity_and_keeps_legacy_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    selected: list[str] = []

    async def run_legacy(_runner, _state) -> None:
        selected.append("legacy")

    async def run_declarative(_runner, _state) -> None:
        selected.append("declarative")

    monkeypatch.setattr(ReportWorkflowRunner, "run", run_legacy)
    monkeypatch.setattr(DeclarativeReportWorkflowRunner, "run", run_declarative)
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=_NoCallProvider(),
    )
    monkeypatch.setattr(
        service,
        "_finalize_completed_run",
        lambda result: result,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="Select the requested runtime",
        target_modules=["2.1"],
    )

    legacy = await service._execute_locked(request, "report-legacy-selection")
    declarative = await service._execute_locked(
        request,
        "report-declarative-selection",
    )

    assert legacy == ReportingRunResult(
        run_id="report-legacy-selection",
        status="completed",
    )
    assert declarative == ReportingRunResult(
        run_id="report-declarative-selection",
        status="completed",
    )
    assert selected == ["legacy", "declarative"]
