"""Characterization for the production public Reporting runtime binding."""

import asyncio
import hashlib
import json
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting.runtime import (
    module_cohort_tools,
    preparation_tools,
)
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
    RunInputSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TEMPLATE_ROLE_SKILL_IDS,
    AgentResult,
    AgentRunStatus,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleSubmission,
    TaskEnvelope,
    TemplateSkillBoundaryManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleAuthoringPreparation,
    DeclarativeModuleRecheckPreparation,
    DeclarativeModuleReviewAgentResult,
    DeclarativeModuleReviewPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckPreparation,
    ModuleReviewPreflightProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_cohort_tools import (
    complete_current_module_lane,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_tools import (
    accept_current_module_authoring,
    accept_current_module_review,
    module_recheck_requires_agent,
    module_review_needs_recheck,
    module_review_needs_revision,
    module_review_preflight_needs_revision,
    module_review_requires_agent,
)
from manyselves.capabilities.distribution_reporting.runtime.module_recheck_tools import (
    accept_current_module_recheck,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_acceptance import (
    _validate_findings,
)
from manyselves.capabilities.distribution_reporting.runtime.module_runtime import (
    CapabilityModuleRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.workflow import ResolvedPlan, WorkflowState, WorkflowStatus
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink


def test_public_reporting_runtime_binding_module_exists() -> None:
    """Characterize the missing Capability-owned root runtime before implementation."""

    assert find_spec(
        "manyselves.capabilities.distribution_reporting.runtime.public_reporting"
    ) is not None


def test_module_authoring_agent_bridge_module_exists() -> None:
    """Characterize the missing neutral Agent bridge before implementation."""

    assert find_spec(
        "manyselves.capabilities.distribution_reporting.runtime.module_agent_bridge"
    ) is not None


@pytest.mark.asyncio
async def test_public_runtime_start_projects_user_schema_and_host_run_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=object(),
        snapshot_content=lambda source, target: (target, "", source),
        runtime_photo_ids=lambda _evidence, _photos: [],
    )
    observed: dict[str, Any] = {}

    async def execute(request, run_id: str, *, workflow_id: str | None = None):
        observed.update(request=request, run_id=run_id, workflow_id=workflow_id)

    monkeypatch.setattr(runtime, "execute", execute)
    command_id = UUID("61000000-0000-4000-8000-000000000001")

    accepted = await runtime.start(
        command_id,
        "module-report",
        {"instruction": "只生成模块 2.4", "target_modules": ["2.4"]},
    )

    assert accepted == {
        "run_id": f"module-report-{command_id.hex}",
        "task_id": None,
    }
    assert observed["run_id"] == accepted["run_id"]
    assert observed["workflow_id"] == "module-report"
    assert observed["request"].operation == "module_report"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("workflow_id", "values"),
    (
        ("full-report", {"instruction": "生成完整报告"}),
        (
            "module-report",
            {"instruction": "只生成模块 2.4", "target_modules": ["2.4"]},
        ),
    ),
)
async def test_public_runtime_start_freezes_inputs_before_execute_and_preparation_loads_it(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workflow_id: str,
    values: dict[str, Any],
) -> None:
    (tmp_path / "Inputs").mkdir()
    (tmp_path / "Knowledge").mkdir()
    (tmp_path / "Templates").mkdir()
    (tmp_path / "Inputs" / "S4-6.xlsx").write_bytes(b"s4-6")
    (tmp_path / "Knowledge" / "notes.md").write_text("knowledge", encoding="utf-8")
    (tmp_path / "Templates" / "report.md").write_text("template", encoding="utf-8")
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda run_id: RunInputSnapshotStore(tmp_path).load(run_id),
        snapshot_content=lambda source, target: (target, "", source),
        runtime_photo_ids=lambda _evidence, _photos: [],
    )
    executed: dict[str, Any] = {}
    freeze_calls: list[tuple[Path, ...]] = []
    original_freeze = RunInputSnapshotStore.freeze

    def freeze(store, run_id: str, *, extra_refs=()):
        freeze_calls.append(tuple(extra_refs))
        return original_freeze(store, run_id, extra_refs=extra_refs)

    monkeypatch.setattr(RunInputSnapshotStore, "freeze", freeze)

    async def execute(request, run_id: str, *, workflow_id: str | None = None):
        executed.update(
            request=request,
            run_id=run_id,
            workflow_id=workflow_id,
            snapshot_exists=(
                tmp_path / "Work" / "runs" / run_id / "input-snapshot.json"
            ).is_file(),
        )

    monkeypatch.setattr(runtime, "execute", execute)

    command_id = UUID("61000000-0000-4000-8000-000000000011")
    accepted = await runtime.start(command_id, workflow_id, values)

    assert accepted == {
        "run_id": f"{workflow_id}-{command_id.hex}",
        "task_id": None,
    }
    assert executed["run_id"] == accepted["run_id"]
    assert executed["snapshot_exists"] is True
    assert freeze_calls == [()]
    snapshot = RunInputSnapshotStore(tmp_path).load(accepted["run_id"])
    assert {item.logical_ref.as_posix() for item in snapshot.files} == {
        "Inputs/S4-6.xlsx",
        "Knowledge/notes.md",
        "Templates/report.md",
    }
    assert {
        item.logical_ref.as_posix()
        for item in runtime.input_snapshot(accepted["run_id"]).files
    } == {item.logical_ref.as_posix() for item in snapshot.files}


def test_public_runtime_projects_its_persisted_run_and_outputs(tmp_path: Path) -> None:
    store = InMemoryWorkflowStateStore()
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=object(),
        snapshot_content=lambda source, target: (target, "", source),
        runtime_photo_ids=lambda _evidence, _photos: [],
        state_store=store,
    )
    state = WorkflowState.for_plan(
        "module-report-query",
        ResolvedPlan(
            workflow_id="module-report",
            workflow_version="1.0.0",
            actions=[],
        ),
    )
    state.status = WorkflowStatus.COMPLETED
    state.outputs = {"result": {"run_id": state.run_id, "module": "2.4"}}
    store.save(state)

    assert runtime.get_run(state.run_id)["run"] == {
        "run_id": state.run_id,
        "capability_id": "distribution-reporting",
        "workflow_id": "module-report",
        "status": "completed",
        "active": False,
        "task_id": None,
    }
    assert runtime.get_outputs(state.run_id) == {
        "run_id": state.run_id,
        "outputs": [
            {
                "id": "result",
                "kind": "value",
                "value": {"run_id": state.run_id, "module": "2.4"},
            }
        ],
    }


@pytest.mark.asyncio
async def test_public_runtime_resumes_waiting_input_through_generic_host(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = InMemoryWorkflowStateStore()
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=object(),
        snapshot_content=lambda source, target: (target, "", source),
        runtime_photo_ids=lambda _evidence, _photos: [],
        state_store=store,
    )
    child = ResolvedPlan(
        workflow_id="child-input",
        workflow_version="1.0.0",
        actions=[],
    )
    plan = ResolvedPlan(
        workflow_id="module-report",
        workflow_version="1.0.0",
        actions=[],
        subworkflow_plans={"child-input": child},
    )
    state = WorkflowState.for_plan("module-report-waiting", plan)
    state.status = WorkflowStatus.WAITING
    state.waiting_input = {
        "input_id": "request-evidence-decision",
        "path": [{"kind": "subworkflow", "action_id": "readiness"}],
    }
    store.save_plan(state.run_id, plan)
    store.save(state)
    observed: dict[str, Any] = {}

    def resume(plan, state, **kwargs):
        observed.update(plan=plan, state=state, resume=kwargs)
        resumed = state.model_copy(deep=True)
        resumed.status = WorkflowStatus.RUNNING
        resumed.waiting_input = None
        return resumed

    async def execute_state(plan, state, definitions, contracts):
        observed.update(executed=(plan, state, definitions, contracts))
        state.status = WorkflowStatus.COMPLETED
        store.save(state)
        return state

    monkeypatch.setattr(
        "manyselves.capabilities.distribution_reporting.runtime.public_reporting.resume_waiting_input",
        resume,
    )
    monkeypatch.setattr(runtime, "_execute_state", execute_state)

    accepted = await runtime.provide_input(
        UUID("61000000-0000-4000-8000-000000000002"),
        state.run_id,
        input_id="request-evidence-decision",
        values={"action": "draft"},
    )

    assert accepted == {"run_id": state.run_id, "task_id": None}
    assert observed["resume"]["subworkflows"] == plan.subworkflow_plans
    assert observed["resume"]["values"] == {"action": "draft"}
    assert observed["executed"][1].run_id == state.run_id


def test_author_accept_projects_typed_submission_to_lane_and_reporting_state(
    tmp_path: Path,
) -> None:
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": "run-2-4"},
        status="author_ready",
        authoring=DeclarativeModuleAuthoringPreparation(
            specialist_id="module-2.4-specialist",
            envelope=None,
            revision=0,
            review=False,
            checkpoint=False,
        ),
    )
    result = DeclarativeModuleAuthoringAgentResult(
        status="completed",
        module=_module_submission(),
    )

    accepted = accept_current_module_authoring(
        {"context": context, "result": result},
        store=ReportingStore(tmp_path),
    )

    assert isinstance(accepted.module, ModuleSubmission)
    assert accepted.module.module_id == "2.4"
    assert accepted.reporting_state["specialist_submissions"]["2.4"] == accepted.module
    persisted = tmp_path / "Work/runs/run-2-4/modules/2.4-r0.json"
    assert persisted.is_file()
    assert json.loads(persisted.read_text()) == accepted.module.model_dump(mode="json")


@pytest.mark.parametrize("status", ("reviewed", "completed"))
def test_complete_current_module_lane_requires_completion_ref(status: str) -> None:
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={
            "run_id": "run-2-4",
            "module_review_completion_refs": {
                "2.4": "Work/runs/run-2-4/reviews/module/initial/2.4/completion-r0.json"
            },
        },
        status=status,
        module=_module_submission(),
    )

    completed = complete_current_module_lane(context)

    assert completed.status == "completed"
    assert completed.module is not None
    assert completed.completion_ref is not None

    without_ref = context.model_copy(
        update={"reporting_state": {"run_id": "run-2-4"}},
    )
    incomplete = complete_current_module_lane(without_ref)

    assert incomplete.status == "failed"
    assert incomplete.module is None
    assert incomplete.completion_ref is None


def test_complete_current_module_lane_does_not_complete_pending_lane() -> None:
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={
            "run_id": "run-2-4",
            "module_review_completion_refs": {
                "2.4": "Work/runs/run-2-4/reviews/module/initial/2.4/completion-r0.json"
            },
        },
        status="review_ready",
        module=_module_submission(),
    )

    incomplete = complete_current_module_lane(context)

    assert incomplete.status == "failed"
    assert incomplete.module is None
    assert incomplete.completion_ref is None


def _module_submission(module_id: str = "2.4") -> ModuleSubmission:
    from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
        REPORT_TAXONOMY,
    )

    return ModuleSubmission(
        module_id=module_id,
        submodule_narratives={
            submodule_id: f"内容 {submodule_id}"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        claims=[],
        source_ids=[],
        unresolved_questions=[],
        revision=0,
    )


def _agent_result_json(
    *,
    task_id: str,
    run_id: str,
    agent_id: str,
    payload: Any,
) -> str:
    return AgentResult(
        task_id=task_id,
        run_id=run_id,
        agent_id=agent_id,
        session_id="scripted-module-session",
        status=AgentRunStatus.COMPLETED,
        payload=payload,
    ).model_dump_json()


class _ScriptedModuleAgentLoop:
    def __init__(
        self,
        bus: MessageBus,
        runtime_id: str,
        result_ref: str,
        reviewer_result_ref: str,
        revision_result_ref: str | None = None,
        recheck_result_ref: str | None = None,
        author_task_id: str = "module-2.4",
    ) -> None:
        self.bus = bus
        self.runtime_id = runtime_id
        self.result_ref = result_ref
        self.reviewer_result_ref = reviewer_result_ref
        self.revision_result_ref = revision_result_ref
        self.recheck_result_ref = recheck_result_ref
        self.author_task_id = author_task_id
        self.received: list[UserMessage] = []
        self._callback = None

    def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
        del messages, task_boundaries, handoff_summary

    async def start(self) -> None:
        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.runtime_id:
                return
            self.received.append(message)
            if message.task_id == "invoke-current-module-reviewer":
                result_ref = self.reviewer_result_ref
                terminal_task_id = "module-2.4-initial-review-r0"
            elif message.task_id == "invoke-current-module-recheck":
                result_ref = self.recheck_result_ref or self.reviewer_result_ref
                terminal_task_id = "module-2.4-initial-review-r1"
            elif message.task_id == "invoke-current-module-revision":
                result_ref = self.revision_result_ref or self.result_ref
                terminal_task_id = "module-revision-r1-2.4"
            else:
                result_ref = self.result_ref
                terminal_task_id = self.author_task_id
            await self.bus.publish(
                AgentResultMessage(
                    sender=self.runtime_id.split(":", 2)[1],
                    workflow_id=message.workflow_id,
                    task_id=terminal_task_id,
                    run_id=message.run_id,
                    result_path=result_ref,
                    task_attempt_id="",
                    session_id=message.session_id,
                )
            )

        self._callback = respond
        self.bus.subscribe(UserMessage, respond)

    async def stop(self) -> None:
        if self._callback is not None:
            self.bus.unsubscribe(UserMessage, self._callback)

    async def wait_until_turn_complete(self) -> None:
        return None


class _BoundaryModuleRuntime:
    def __init__(self, execution: AgentExecutionService, session_factory) -> None:
        self.agent_invokers = {"module-2.4-specialist": object()}
        self.agent_execution = execution
        self.agent_session_factory = session_factory

    async def prepare_lanes(self, state: dict[str, Any]) -> dict[str, Any]:
        return state

    async def start_lane(
        self,
        module_id: str,
        state: dict[str, Any],
        workflow_id: str,
        _lane_outcome=None,
    ) -> DeclarativeModuleRuntimeLaneContext:
        return DeclarativeModuleRuntimeLaneContext(
            module_id=module_id,
            workflow_id=workflow_id,
            reporting_state=dict(state),
            status="ready",
        )

    async def prepare_author_lane(
        self,
        context: DeclarativeModuleRuntimeLaneContext,
    ) -> DeclarativeModuleRuntimeLaneContext:
        envelope = TaskEnvelope(
            task_id="module-2.4-authoring",
            task_attempt_id="attempt-module-2.4-authoring",
            run_id=str(context.reporting_state["run_id"]),
            agent_id="module-2.4-specialist",
            objective="author module 2.4",
            inline_context="author-skill: preserve evidence references",
        )
        return context.model_copy(
            update={
                "status": "author_ready",
                "authoring": DeclarativeModuleAuthoringPreparation(
                    specialist_id="module-2.4-specialist",
                    envelope=envelope,
                    revision=0,
                    review=False,
                    checkpoint=False,
                ),
            }
        )

    async def author_requires_agent(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return True

    async def lane_retries_preflight_revision(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False

    async def lane_has_deferred_main_exception(
        self,
        _context: DeclarativeModuleRuntimeLaneContext,
    ) -> bool:
        return False


def _patch_minimal_preparation(monkeypatch: pytest.MonkeyPatch) -> None:
    snapshot_digest = "a" * 64

    monkeypatch.setattr(
        preparation_tools,
        "parse_report_taxonomy_workbook",
        lambda *args, **kwargs: {"schema_version": 1, "modules": []},
    )
    monkeypatch.setattr(preparation_tools, "activate_report_taxonomy", lambda _value: None)
    monkeypatch.setattr(
        preparation_tools,
        "prepare_manifest_file",
        lambda workspace, run_id, manifest_file, order: preparation_tools.FilePreparationResult(
            manifest_order=order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=snapshot_digest,
            purpose=manifest_file.purpose,
            status="parsed",
        ),
    )


def _write_complete_template_skill_fixture(workspace: Path) -> None:
    root = workspace / "Work/report-template-role-skills"
    boundary = TemplateSkillBoundaryManifest(
        transferred_categories=[
            "analysis_method",
            "synthesis_method",
            "visual_method",
            "quality_check",
        ],
        excluded_categories=[
            "domain_knowledge",
            "domain_standard_or_threshold",
            "project_fact_or_number",
            "customer_identity",
            "project_finding_or_risk",
            "project_conclusion_or_recommendation",
            "evidence_or_claim_identifier",
        ],
        boundary_statement=(
            "Only reusable reporting methods and fact-free structural examples may be "
            "transferred; every project fact, conclusion, threshold, and evidence identity "
            "must remain scoped to the current run inputs."
        ),
    )
    artifact_sha256: dict[str, str] = {}
    for skill_id in TEMPLATE_ROLE_SKILL_IDS:
        path = root / skill_id / "SKILL.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        content = (
            "auditor-skill: preserve the review envelope\n"
            if skill_id == "auditor-2.4"
            else f"{skill_id}: preserve typed evidence boundaries.\n"
        )
        path.write_text(content, encoding="utf-8")
        artifact_sha256[f"{skill_id}/SKILL.md"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
    boundary_path = root / "boundary.json"
    boundary_path.write_text(boundary.model_dump_json(), encoding="utf-8")
    artifact_sha256["boundary.json"] = hashlib.sha256(
        boundary_path.read_bytes()
    ).hexdigest()
    (root / "source.json").write_text(
        json.dumps(
            {
                "boundary_policy_version": boundary.policy_version,
                "boundary_ref": "Work/report-template-role-skills/boundary.json",
                "artifact_sha256": artifact_sha256,
            }
        ),
        encoding="utf-8",
    )


@pytest.mark.asyncio
async def test_module_report_host_reaches_next_tool_after_selected_module_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The neutral Agent bridge completes before the next unbound Tool gap."""

    _patch_minimal_preparation(monkeypatch)
    _write_complete_template_skill_fixture(tmp_path)
    snapshot = {
        "inventory_digest": "inventory",
        "files": [
            {
                "logical_ref": "Inputs/S4-6.xlsx",
                "snapshot_ref": "Inputs/S4-6.xlsx",
                "sha256": "a" * 64,
            }
        ],
    }
    run_id = "public-module-boundary"
    result_ref = "Work/runs/public-module-boundary/results/module-2.4.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        _agent_result_json(
            task_id="module-2.4-authoring",
            run_id=run_id,
            agent_id="module-2.4-specialist",
            payload=_module_submission(),
        ),
        encoding="utf-8",
    )
    reviewer_result_ref = (
        "Work/runs/public-module-boundary/results/module-review-2.4.json"
    )
    reviewer_result_path = tmp_path / reviewer_result_ref
    reviewer_result_path.write_text(
        _agent_result_json(
            task_id="module-2.4-initial-review-r0",
            run_id=run_id,
            agent_id="evidence-auditor",
            payload={
                "kind": "module_review_finding_submission",
                "coverage": {
                    "submodule_ids": sorted(_module_submission().submodule_narratives)
                },
                "findings": [],
            },
        ),
        encoding="utf-8",
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=1)
    loops: list[_ScriptedModuleAgentLoop] = []

    def session_factory(runtime_id: str) -> _ScriptedModuleAgentLoop:
        loop = _ScriptedModuleAgentLoop(
            bus,
            runtime_id,
            result_ref,
            reviewer_result_ref,
        )
        loops.append(loop)
        return loop

    module_runtime = CapabilityModuleRuntime(
        tmp_path,
        store=ReportingStore(tmp_path),
        agent_execution=execution,
        agent_session_factory=session_factory,
        agent_invokers={"module-2.4-specialist": object()},
    )
    store = InMemoryWorkflowStateStore()
    events = InMemoryWorkflowEventSink()
    captured_lane_contexts: list[DeclarativeModuleRuntimeLaneContext] = []
    original_complete_lane = module_cohort_tools.complete_current_module_lane

    def capture_complete_lane(value: Any) -> Any:
        context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
        captured_lane_contexts.append(context)
        return original_complete_lane(context)

    monkeypatch.setattr(
        module_cohort_tools,
        "complete_current_module_lane",
        capture_complete_lane,
    )
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: snapshot,
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=module_runtime,
        state_store=store,
        events=events,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="run only module 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    _definitions, _contracts, plan = runtime.compile_plan(request)
    assert set(plan.subworkflow_plans) >= {
        "distribution-reporting-preparation",
        "distribution-evidence-readiness",
        "distribution-module-cohort-selected-2-4",
        "distribution-module-2.4-runtime-lane",
    }
    assert not any(
        module_id in subworkflow_id
        for subworkflow_id in plan.subworkflow_plans
        for module_id in ("2.1", "2.2", "2.3", "2.5")
    )

    run_id = "public-module-boundary"
    try:
        completed = await runtime.execute(request, run_id)
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    state = store.load(run_id)
    assert completed.status is WorkflowStatus.COMPLETED
    assert state.status is WorkflowStatus.COMPLETED
    assert state.actions["run-reporting-preparation"].status.value == "completed"
    assert state.actions["run-evidence-readiness"].status.value == "completed"
    assert state.actions["build-reporting-state"].status.value == "completed"
    assert len(loops) == 2
    author_loop = next(loop for loop in loops if "module-2.4-specialist" in loop.runtime_id)
    reviewer_loop = next(loop for loop in loops if "evidence-auditor" in loop.runtime_id)
    assert [message.turn_kind for message in author_loop.received] == ["task_initial"]
    assert "author-2.4: preserve typed evidence boundaries" in (
        author_loop.received[0].content
    )
    assert '"open_project_source"' in author_loop.received[0].content
    assert [message.turn_kind for message in reviewer_loop.received] == ["task_initial"]
    assert reviewer_loop.runtime_id == (
        "public-reporting:evidence-auditor:module-auditor-2.4"
    )
    assert reviewer_loop.received[0].session_id == "public-reporting:module-auditor-2.4"
    assert "auditor-skill: preserve the review envelope" in reviewer_loop.received[0].content
    assert '"module_id": "2.4"' in reviewer_loop.received[0].content
    assert any(
        event.kind == "action.completed"
        and event.action_id == "invoke-current-module-author"
        for event in events.events
    )
    assert any(
        event.kind == "action.completed"
        and event.action_id == "complete-current-module-lane"
        for event in events.events
    )
    assert any(
        event.kind == "action.completed"
        and event.action_id == "accept-current-module-review"
        for event in events.events
    )
    assert len(captured_lane_contexts) == 1
    lane_context = captured_lane_contexts[0]
    assert lane_context.status == "reviewed"
    assert module_review_preflight_needs_revision(lane_context) is False
    assert module_review_requires_agent(lane_context) is False
    assert module_review_needs_recheck(lane_context) is False
    assert module_review_needs_revision(lane_context) is False
    assert lane_context.module is not None
    assert lane_context.module.module_id == "2.4"
    assert ModuleSubmission.model_validate(
        lane_context.reporting_state["specialist_submissions"]["2.4"]
    ) == lane_context.module
    assert lane_context.reporting_state["module_review_completion_refs"]["2.4"].endswith(
        "reviews/module/initial/2.4/completion-r0.json"
    )
    assert lane_context.review is not None
    review = lane_context.review
    assert review.reviewer_session_key == "module-auditor-2.4"
    assert review.prepared.mode == "invoke_agent"
    assert review.prepared.review_input is not None
    assert review.prepared.review_input.phase == "initial"
    assert review.prepared.review_input.module_id == "2.4"
    assert review.envelope is not None
    assert review.envelope.task_id == "module-2.4-initial-review-r0"
    assert review.envelope.agent_id == "evidence-auditor"
    assert review.envelope.input_contract_kind == "module_review_input"
    assert review.envelope.input_contract_ref is not None
    assert review.envelope.input_contract_ref.endswith(
        "reviews/module/initial/2.4/input-r0.json"
    )
    assert {
        "coverage 记录实际检查范围，不是批准状态",
        "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
        "不得拆成独立小节级审查任务或会话",
        "finding 首次提出后不可改写；复审不得复述旧 finding",
        "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
        "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
        "首轮必须覆盖 input 中全部 required_submodule_ids",
    }.issubset(review.envelope.constraints)
    assert "auditor-skill: preserve the review envelope" in (
        review.envelope.inline_context or ""
    )
    finding_result = AgentResult.model_validate_json(
        reviewer_result_path.read_text(encoding="utf-8")
    )
    assert finding_result.payload is not None
    finding_submission = finding_result.payload.model_dump(mode="json")
    assert finding_submission["kind"] == "module_review_finding_submission"
    assert finding_submission["findings"] == []
    assert finding_submission["coverage"]["submodule_ids"] == sorted(
        _module_submission().submodule_narratives
    )
    cohort_state = state.subworkflow_states["run-module-cohort"]
    cohort_output = cohort_state["outputs"]["result"]
    assert set(cohort_output) == {"2.4"}
    assert cohort_output["2.4"]["module"]["module_id"] == "2.4"
    assert (
        tmp_path / "Work/runs/public-module-boundary/modules/2.4-r0.json"
    ).is_file()
    assert (
        tmp_path / "Work/runs/public-module-boundary/reviews/module-quality-2.4-r0-review-r0.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/public-module-boundary/reviews/module/initial/2.4/preflight-subject-r0-review-r0.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/public-module-boundary/reviews/module/initial/2.4/input-r0.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/public-module-boundary/reviews/module/initial/2.4/findings-r0.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/public-module-boundary/reviews/module/initial/2.4/completion-r0.json"
    ).is_file()
    assert (
        tmp_path
        / "Work/runs/public-module-boundary/reviews/module/initial/2.4/progress.json"
    ).is_file()
    assert (tmp_path / "Outputs/Modules/2.4.md").read_text() == _module_submission().markdown
    assert state.actions["run-module-cohort"].status.value == "completed"
    assert state.actions["attach-module-results"].status.value == "completed"
    assert state.actions["publish-module-report"].status.value == "completed"
    assert state.actions["finish-module-report"].status.value == "completed"
    output = state.outputs["result"]
    assert set(output["module_submissions"]) == {"2.4"}
    assert output["module_submissions"]["2.4"].module_id == "2.4"
    assert set(output["specialist_submissions"]) == {"2.4"}
    assert set(output["module_review_completion_refs"]) == {"2.4"}
    assert output["module_review_completion_refs"]["2.4"].endswith(
        "reviews/module/initial/2.4/completion-r0.json"
    )


@pytest.mark.asyncio
async def test_module_report_finding_revision_recheck_completes_without_replay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Drive one finding through Author revision and Auditor recheck."""

    _patch_minimal_preparation(monkeypatch)
    snapshot = {
        "inventory_digest": "inventory",
        "files": [
            {
                "logical_ref": "Inputs/S4-6.xlsx",
                "snapshot_ref": "Inputs/S4-6.xlsx",
                "sha256": "a" * 64,
            }
        ],
    }
    run_id = "public-module-finding"
    result_ref = f"Work/runs/{run_id}/results/module-2.4.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        _agent_result_json(
            task_id="module-2.4-authoring",
            run_id=run_id,
            agent_id="module-2.4-specialist",
            payload=_module_submission(),
        ),
        encoding="utf-8",
    )
    finding_id = "M-2.4-initial-r0-1"
    reviewer_result_ref = f"Work/runs/{run_id}/results/module-review-2.4.json"
    reviewer_result_path = tmp_path / reviewer_result_ref
    reviewer_result_path.write_text(
        _agent_result_json(
            task_id="module-2.4-initial-review-r0",
            run_id=run_id,
            agent_id="evidence-auditor",
            payload={
                "kind": "module_review_finding_submission",
                "coverage": {
                    "submodule_ids": sorted(_module_submission().submodule_narratives)
                },
                "findings": [
                    {
                        "id": finding_id,
                        "target_submodule_id": "2.4.1.1",
                        "category": "evidence_boundary",
                        "impact": "blocking",
                        "observation": "当前正文没有把客户证据边界写清楚，读者无法复核该结论。",
                        "evidence_refs": ["module-2.4-r0"],
                        "required_change": "补充可复核的证据边界说明并明确待核实限制。",
                        "reviewer_checks": ["确认正文明确说明证据边界和待核实限制"],
                    }
                ],
            },
        ),
        encoding="utf-8",
    )
    revision_result_ref = f"Work/runs/{run_id}/results/module-revision-2.4.json"
    revision_result_path = tmp_path / revision_result_ref
    revision_result_path.write_text(
        _agent_result_json(
            task_id="module-revision-r1-2.4",
            run_id=run_id,
            agent_id="module-2.4-specialist",
            payload={
                "kind": "module_revision_submission",
                "module_id": "2.4",
                "base_revision": 0,
                "revision": 1,
                "submodule_narratives": {
                    "2.4.1.1": "修订后的正文明确说明证据边界、适用条件和可复核的后续验证方式。"
                },
                "claims_upsert": [],
                "claim_ids_remove": [],
                "source_ids": [],
                "unresolved_questions": [],
                "revision_responses": [
                    {
                        "finding_id": finding_id,
                        "action": "implemented",
                        "summary": "已按要求补充证据边界和待核实限制，并保留可复核的验证步骤。",
                        "changed_target_ids": ["2.4.1.1"],
                    }
                ],
            },
        ),
        encoding="utf-8",
    )
    recheck_result_ref = f"Work/runs/{run_id}/results/module-recheck-2.4.json"
    recheck_result_path = tmp_path / recheck_result_ref
    recheck_result_path.write_text(
        _agent_result_json(
            task_id="module-2.4-initial-review-r1",
            run_id=run_id,
            agent_id="evidence-auditor",
            payload={
                "kind": "module_review_verdict_submission",
                "coverage": {"submodule_ids": ["2.4.1.1"]},
                "verdicts": [
                    {
                        "finding_id": finding_id,
                        "verdict": "resolved",
                        "reason": "修订内容已经满足原 finding 的证据边界与验证要求。",
                        "evidence_refs": ["module-2.4-r1"],
                    }
                ],
                "new_findings": [],
            },
        ),
        encoding="utf-8",
    )
    auditor_skill = tmp_path / "Work/report-template-role-skills/auditor-2.4/SKILL.md"
    auditor_skill.parent.mkdir(parents=True)
    auditor_skill.write_text("auditor-skill: preserve the review envelope", encoding="utf-8")
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=1)
    loops: list[_ScriptedModuleAgentLoop] = []
    events = InMemoryWorkflowEventSink()

    def session_factory(runtime_id: str) -> _ScriptedModuleAgentLoop:
        loop = _ScriptedModuleAgentLoop(
            bus,
            runtime_id,
            result_ref,
            reviewer_result_ref,
            revision_result_ref,
            recheck_result_ref,
            author_task_id="module-2.4-authoring",
        )
        loops.append(loop)
        return loop

    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: snapshot,
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=_BoundaryModuleRuntime(execution, session_factory),
        state_store=InMemoryWorkflowStateStore(),
        events=events,
    )
    request = ReportRequest(
        operation="module_report",
        instruction="run only module 2.4",
        target_modules=["2.4"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )

    try:
        completed = await runtime.execute(request, run_id)
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    state = runtime.state_store.load(run_id)
    assert completed.status is WorkflowStatus.COMPLETED
    assert state.status is WorkflowStatus.COMPLETED
    author_initial = next(
        loop for loop in loops if "specialist-2.4" in loop.runtime_id
    )
    author_revision = next(
        loop for loop in loops if loop.runtime_id.endswith(":module-2.4")
    )
    reviewer = next(loop for loop in loops if "evidence-auditor" in loop.runtime_id)
    assert len(author_initial.received) == 1
    assert len(author_revision.received) == 1
    assert len(reviewer.received) == 2
    assert author_revision.received[0].session_id == "public-reporting:module-2.4"
    assert reviewer.received[0].session_id == reviewer.received[1].session_id
    assert finding_id in author_revision.received[0].content
    assert finding_id in reviewer.received[1].content
    assert "module_revision_submission" in author_revision.received[0].content
    assert '"kind": "module_review_input"' in reviewer.received[1].content
    assert (tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/findings-r0.json").is_file()
    assert (tmp_path / f"Work/runs/{run_id}/modules/2.4-r1.json").is_file()
    assert (tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/verdicts-r1.json").is_file()
    assert (tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/completion-r1.json").is_file()
    recheck_input = json.loads(
        (tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/input-r1.json").read_text()
    )
    assert recheck_input["kind"] == "module_review_input"
    assert recheck_input["phase"] == "recheck"
    assert recheck_input["revision_diff"]["changed_submodule_narratives"] == ["2.4.1.1"]
    # The compact recheck boundary only covers submodules assigned by pending
    # findings.  Unassigned module content must not be pulled into the Auditor
    # scope merely to produce a digest.
    assert recheck_input["unchanged_submodule_sha256"] == {}
    assert recheck_input["required_findings"][0]["id"] == finding_id
    assert any(
        event.kind == "action.completed"
        and event.action_id == "invoke-current-module-revision"
        for event in events.events
    )
    assert any(
        event.kind == "action.completed"
        and event.action_id == "invoke-current-module-recheck"
        for event in events.events
    )
    assert state.outputs["result"]["module_submissions"]["2.4"].revision == 1


def test_module_recheck_open_and_new_finding_routes_to_next_revision(
    tmp_path: Path,
) -> None:
    """An unresolved prior or genuinely new finding cannot complete the lane."""

    run_id = "run-module-recheck-open"
    module = _module_submission().model_copy(
        update={
            "revision": 1,
            "submodule_narratives": {
                **_module_submission().submodule_narratives,
                "2.4.1.1": "已完成一次定向修订，但仍需由同一审计员复核证据边界。",
            },
            "revision_responses": [
                {
                    "finding_id": "M-2.4-initial-r0-1",
                    "action": "implemented",
                    "summary": "已补充证据边界说明和对应的可复核验证步骤。",
                    "changed_target_ids": ["2.4.1.1"],
                }
            ],
        }
    )
    finding = ModuleReviewFinding(
        id="M-2.4-initial-r0-1",
        target_submodule_id="2.4.1.1",
        category="evidence_boundary",
        impact="blocking",
        observation="当前正文仍没有完整说明证据边界，读者无法复核该结论。",
        evidence_refs=["module-2.4-r0"],
        required_change="补充可复核的证据边界说明并明确待核实限制。",
        reviewer_checks=["复审确认正文明确说明证据边界和待核实限制。"],
    )
    new_finding = finding.model_copy(
        update={
            "id": "M-2.4-initial-r1-2",
            "impact": "advisory",
            "observation": "修订后新增一处行动闭环缺口，仍需要作者补充责任和验证方式。",
            "required_change": "补充责任人、完成时序和可复核的行动验证方式。",
        }
    )
    review_root = f"Work/runs/{run_id}/reviews/module/initial/2.4"
    prepared = ModuleRecheckPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=review_root,
        progress_ref=f"{review_root}/progress.json",
        review_round=1,
        scope=["2.4.1.1"],
        current=module,
        pending=[finding],
        responses=list(module.revision_responses),
        finding_refs=[f"{review_root}/findings-r0.json"],
        last_reviewed_subject_ref=f"Work/runs/{run_id}/modules/2.4-r0.json",
        subject_ref=f"Work/runs/{run_id}/modules/2.4-r1.json",
    )
    initial_prepared = ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root=review_root,
        progress_ref=prepared.progress_ref,
        review_round=0,
        scope=["2.4.1.1"],
        current=_module_submission(),
        subject_ref=prepared.last_reviewed_subject_ref,
    )
    initial_acceptance = ModuleInitialReviewAcceptance(
        run_id=run_id,
        module_id="2.4",
        lifecycle_id="initial",
        reviewer_session_key="module-auditor-2.4",
        subject_ref=prepared.last_reviewed_subject_ref or "",
        current=_module_submission(),
        findings=[finding],
        finding_refs=prepared.finding_refs,
        next_action="revise",
        progress_ref=prepared.progress_ref,
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": run_id},
        status="recheck_ready",
        module=module,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key="module-auditor-2.4",
            prepared=initial_prepared,
            acceptance=initial_acceptance,
        ),
        recheck=DeclarativeModuleRecheckPreparation(prepared=prepared),
    )
    result = {
        "status": "completed",
        "submission": {
            "kind": "module_review_verdict_submission",
            "coverage": {"submodule_ids": ["2.4.1.1"]},
            "verdicts": [
                {
                    "finding_id": finding.id,
                    "verdict": "open",
                    "reason": "原 finding 尚未完全满足 reviewer_checks，需要继续修订。",
                    "evidence_refs": [],
                }
            ],
            "new_findings": [new_finding.model_dump(mode="json")],
        },
    }

    accepted = accept_current_module_recheck(
        {"context": context, "result": result},
        store=ReportingStore(tmp_path),
    )

    assert accepted.status == "reviewed"
    assert accepted.review is not None
    assert accepted.review.acceptance is not None
    assert accepted.review.acceptance.next_action == "continue_existing"
    assert {item.id for item in accepted.review.acceptance.findings} == {
        finding.id,
        new_finding.id,
    }
    assert module_review_needs_recheck(accepted) is False
    assert module_review_needs_revision(accepted) is True
    assert module_recheck_requires_agent(accepted) is False
    assert not (
        tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/completion-r1.json"
    ).is_file()
    progress = json.loads(
        (tmp_path / f"Work/runs/{run_id}/reviews/module/initial/2.4/progress.json").read_text()
    )
    assert progress["next_action"] == "revise"
    assert {item["id"] for item in progress["pending"]} == {
        finding.id,
        new_finding.id,
    }


def test_module_review_preflight_failure_routes_to_author_correction_gap() -> None:
    module = _module_submission()
    prepared = ModuleInitialReviewPreparation(
        mode="preflight_revision",
        run_id="run-preflight-failure",
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root="Work/runs/run-preflight-failure/reviews/module/initial/2.4",
        progress_ref="Work/runs/run-preflight-failure/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=["2.4.1"],
        current=module,
        validation_ref="Work/runs/run-preflight-failure/reviews/module/initial/2.4/preflight.json",
        validation_target_submodule_ids=["2.4.1"],
        preflight_progress=ModuleReviewPreflightProgress(current=module, attempts=1),
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": "run-preflight-failure"},
        status="preflight_revision_pending",
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key="module-auditor-2.4",
            prepared=prepared,
        ),
        module=module,
    )

    assert module_review_preflight_needs_revision(context) is True
    assert module_review_requires_agent(context) is False


def test_initial_review_finding_persists_and_routes_to_revision_gap(
    tmp_path: Path,
) -> None:
    module = _module_submission()
    scope = sorted(module.submodule_narratives)
    subject_ref = "Work/runs/run-review-finding/modules/2.4-r0.json"
    prepared = ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id="run-review-finding",
        module_id="2.4",
        lifecycle_id="initial",
        workflow_id="public-reporting",
        reviewer_session_key="module-auditor-2.4",
        review_root="Work/runs/run-review-finding/reviews/module/initial/2.4",
        progress_ref="Work/runs/run-review-finding/reviews/module/initial/2.4/progress.json",
        review_round=0,
        scope=scope,
        current=module,
        subject_ref=subject_ref,
    )
    context = DeclarativeModuleRuntimeLaneContext(
        module_id="2.4",
        workflow_id="public-reporting",
        reporting_state={"run_id": "run-review-finding"},
        status="review_ready",
        module=module,
        review=DeclarativeModuleReviewPreparation(
            envelope=None,
            reviewer_session_key="module-auditor-2.4",
            prepared=prepared,
        ),
    )
    finding = ModuleReviewFinding(
        id="M-2.4-initial-r0-1",
        target_submodule_id=scope[0],
        category="analysis_depth",
        impact="blocking",
        observation="当前子模块没有说明该条件对运行后果的具体影响。",
        evidence_refs=["module-2.4-r0"],
        required_change="补充可观察的运行后果，并保持当前证据边界不变。",
        reviewer_checks=["复审确认运行后果已明确写入该子模块。"],
    )
    advisory = finding.model_copy(
        update={
            "id": "M-2.4-initial-r0-2",
            "target_submodule_id": scope[1],
            "impact": "advisory",
            "observation": "当前子模块缺少对行动闭环和后续验证方式的说明。",
            "required_change": "补充责任、时序和可复核的验证方式，避免只保留原则性建议。",
        }
    )
    result = DeclarativeModuleReviewAgentResult(
        status="completed",
        submission=ModuleReviewFindingSubmission(
            coverage={"submodule_ids": scope},
            findings=[finding, advisory],
        ),
    )
    duplicate_submission = ModuleReviewFindingSubmission.model_construct(
        kind="module_review_finding_submission",
        coverage={"submodule_ids": scope},
        findings=[finding, finding],
    )
    with pytest.raises(ValueError, match="duplicate ids"):
        _validate_findings(
            duplicate_submission,
            module_id="2.4",
            lifecycle_id="initial",
            review_round=0,
            scope=set(scope),
        )

    accepted = accept_current_module_review(
        {"context": context, "result": result},
        store=ReportingStore(tmp_path),
    )

    assert accepted.status == "reviewed"
    assert accepted.review is not None
    assert accepted.review.acceptance is not None
    assert accepted.review.acceptance.next_action == "revise"
    assert accepted.review.acceptance.findings[0].id == "M-2.4-initial-r0-1"
    assert accepted.review.acceptance.findings[1].impact == "advisory"
    assert module_review_needs_recheck(accepted) is False
    assert module_review_needs_revision(accepted) is True
    assert (
        tmp_path
        / "Work/runs/run-review-finding/reviews/module/initial/2.4/findings-r0.json"
    ).is_file()
    progress = json.loads(
        (
            tmp_path
            / "Work/runs/run-review-finding/reviews/module/initial/2.4/progress.json"
        ).read_text()
    )
    assert progress["next_action"] == "revise"
    assert progress["pending"][0]["id"] == "M-2.4-initial-r0-1"
    assert progress["pending"][1]["id"] == "M-2.4-initial-r0-2"
    accepted_again = accept_current_module_review(
        {"context": context, "result": result},
        store=ReportingStore(tmp_path),
    )
    assert accepted_again.review is not None
    assert accepted_again.review.acceptance is not None
    assert accepted_again.review.acceptance.finding_refs == accepted.review.acceptance.finding_refs
    changed_result = result.model_copy(
        update={
            "submission": result.submission.model_copy(
                update={"findings": [finding]}
            )
        }
    )
    with pytest.raises(ValueError, match="refusing to overwrite immutable audit artifact"):
        accept_current_module_review(
            {"context": context, "result": changed_result},
            store=ReportingStore(tmp_path),
        )
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot={},
        snapshot_content=lambda *args: None,
        runtime_photo_ids=lambda *args: None,
    )
    plan = runtime.compile_plan(
        ReportRequest(
            operation="module_report",
            instruction="run only module 2.4",
            target_modules=["2.4"],
        )
    )[2]
    review_route = next(
        action
        for action in plan.subworkflow_plans["distribution-module-2.4-runtime-lane"].actions
        if action.id == "choose-module-review-revision"
    )
    assert review_route.then == "prepare-current-module-revision"


def test_full_report_stops_at_existing_tail_specialization_boundary(
    tmp_path: Path,
) -> None:
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot={},
        snapshot_content=lambda *args: None,
        runtime_photo_ids=lambda *args: None,
    )
    request = ReportRequest(
        operation="full_report",
        instruction="compile the full report root",
    )

    with pytest.raises(
        ValueError,
        match="distribution-cross-owner-2.1-pipeline",
    ):
        runtime.compile_plan(request)
