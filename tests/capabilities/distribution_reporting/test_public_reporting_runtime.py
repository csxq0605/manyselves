"""Characterization for the production public Reporting runtime binding."""

import asyncio
import json
from importlib.util import find_spec
from pathlib import Path
from typing import Any

import pytest

from manyselves.capabilities.distribution_reporting.runtime import preparation_tools
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleAuthoringAgentResult,
    DeclarativeModuleAuthoringPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.module_lane_tools import (
    accept_current_module_authoring,
)
from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
    PublicReportingWorkflowRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.workflow import WorkflowStatus
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


class _ScriptedModuleAgentLoop:
    def __init__(self, bus: MessageBus, runtime_id: str, result_ref: str) -> None:
        self.bus = bus
        self.runtime_id = runtime_id
        self.result_ref = result_ref
        self.received: list[UserMessage] = []
        self._callback = None

    def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
        del messages, task_boundaries, handoff_summary

    async def start(self) -> None:
        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.runtime_id:
                return
            self.received.append(message)
            await self.bus.publish(
                AgentResultMessage(
                    sender=self.runtime_id,
                    workflow_id=message.workflow_id,
                    task_id=message.task_id,
                    run_id=message.run_id,
                    result_path=self.result_ref,
                    task_attempt_id=message.task_attempt_id,
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


@pytest.mark.asyncio
async def test_module_report_host_reaches_next_tool_after_selected_module_agent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The neutral Agent bridge completes before the next unbound Tool gap."""

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
    result_ref = "Work/runs/public-module-boundary/results/module-2.4.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        json.dumps(_module_submission().model_dump(mode="json")),
        encoding="utf-8",
    )
    auditor_skill = tmp_path / "Work/report-template-role-skills/auditor-2.4/SKILL.md"
    auditor_skill.parent.mkdir(parents=True)
    auditor_skill.write_text("auditor-skill: preserve the review envelope", encoding="utf-8")
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=1)
    loops: list[_ScriptedModuleAgentLoop] = []

    def session_factory(runtime_id: str) -> _ScriptedModuleAgentLoop:
        loop = _ScriptedModuleAgentLoop(bus, runtime_id, result_ref)
        loops.append(loop)
        return loop

    module_runtime = _BoundaryModuleRuntime(execution, session_factory)
    store = InMemoryWorkflowStateStore()
    events = InMemoryWorkflowEventSink()
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
        with pytest.raises(
            RuntimeError,
            match="missing tool adapter: module-review-preflight-needs-revision",
        ):
            await runtime.execute(request, run_id)
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    state = store.load(run_id)
    assert state.status is WorkflowStatus.FAILED
    assert state.actions["run-reporting-preparation"].status.value == "completed"
    assert state.actions["run-evidence-readiness"].status.value == "completed"
    assert state.actions["build-reporting-state"].status.value == "completed"
    assert len(loops) == 1
    assert [message.turn_kind for message in loops[0].received] == ["task_initial"]
    assert "author-skill: preserve evidence references" in loops[0].received[0].content
    assert any(
        event.kind == "action.completed"
        and event.action_id == "invoke-current-module-author"
        for event in events.events
    )
    assert any(
        event.kind == "action.failed"
        and event.action_id == "module-review-preflight-needs-revision"
        for event in events.events
    )
    lane_state = state.model_dump(mode="json")["subworkflow_states"][
        "run-module-cohort"
    ]["subworkflow_states"]["execute-module-2.4"]
    assert lane_state["variables"]["module-author-result"]["module"]["module_id"] == "2.4"
    lane_context = lane_state["variables"]["lane-context"]
    assert lane_context["status"] == "review_ready"
    assert lane_context["module"]["module_id"] == "2.4"
    assert (
        lane_context["reporting_state"]["specialist_submissions"]["2.4"]["module_id"]
        == "2.4"
    )
    review = lane_context["review"]
    assert review["reviewer_session_key"] == "module-auditor-2.4"
    assert review["prepared"]["mode"] == "invoke_agent"
    assert review["prepared"]["review_input"]["phase"] == "initial"
    assert review["prepared"]["review_input"]["module_id"] == "2.4"
    assert review["prepared"]["envelope"]["task_id"] == "module-2.4-initial-review-r0"
    assert review["prepared"]["envelope"]["agent_id"] == "evidence-auditor"
    assert review["prepared"]["envelope"]["input_contract_kind"] == "module_review_input"
    assert review["prepared"]["envelope"]["input_contract_ref"].endswith(
        "reviews/module/initial/2.4/input-r0.json"
    )
    review_constraints = review["prepared"]["envelope"]["constraints"]
    assert {
        "coverage 记录实际检查范围，不是批准状态",
        "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
        "不得拆成独立小节级审查任务或会话",
        "finding 首次提出后不可改写；复审不得复述旧 finding",
        "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
        "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
        "首轮必须覆盖 input 中全部 required_submodule_ids",
    }.issubset(review_constraints)
    assert "auditor-skill: preserve the review envelope" in review["prepared"][
        "envelope"
    ]["inline_context"]
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
