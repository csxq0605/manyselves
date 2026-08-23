"""Characterization for the Capability-owned full-report tail composition."""

import asyncio
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from pydantic import ValidationError

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
    AgentRunStatus,
    CrossOwnerFindingSubmission,
    CrossReviewCoverageEntry,
    ModuleSubmission,
)
from manyselves.core.loops.bus import MessageBus
from manyselves.interfaces.types import AgentResultMessage, UserMessage
from manyselves.kernel.conversations import ConversationKey, ConversationMode, ConversationRecord
from manyselves.kernel.definitions import AgentDefinition, TaskDefinition
from manyselves.runtime.agent_execution import AgentExecutionService


def _cross_port_with_all_declared_tools() -> SimpleNamespace:
    attributes = {
        "prepare",
        "reduce",
        "prepare_initial",
        "initial_requires_agent",
        "accept_initial",
        "initial_has_findings",
        "prepare_revision",
        "revision_requires_agent",
        "accept_revision",
        "prepare_author_exception",
        "prepare_reviewer_exception",
        "main_exception_requires_agent",
        "accept_main_exception",
        "main_exception_requests_user",
        "apply_main_exception_user_input",
        "author_exception_returns_to_author",
        "prepare_local_review",
        "local_review_requires_agent",
        "accept_local_review",
        "prepare_recheck",
        "recheck_requires_agent",
        "accept_recheck",
        "advance_round",
        "round_needs_revision",
        "complete_owner_round",
        "complete_owner_without_findings",
    }
    return SimpleNamespace(
        agent_invokers={"cross-module-reviewer": object()},
        **{attribute: (lambda value=None: value) for attribute in attributes},
    )


def _chief_port_with_all_declared_tools() -> SimpleNamespace:
    attributes = {
        "prepare",
        "prepare_lane",
        "requires_agent",
        "accept_lane",
        "complete_lane",
        "reduce",
    }
    return SimpleNamespace(
        agent_invokers={"chief-editor": object()},
        **{attribute: (lambda value=None: value) for attribute in attributes},
    )


def _module_submissions() -> dict[str, ModuleSubmission]:
    return {
        module_id: ModuleSubmission(
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
        for module_id in REPORT_TAXONOMY
    }


def _cross_state(run_id: str = "cross-runtime-run") -> dict[str, Any]:
    return {
        "run_id": run_id,
        "module_submissions": _module_submissions(),
        "module_artifact_refs": {
            module_id: {
                "ref": f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                "sha256": "0" * 64,
            }
            for module_id in REPORT_TAXONOMY
        },
    }


def test_full_report_tail_composition_reaches_first_unbound_cross_tool(
    tmp_path: Path,
) -> None:
    """The public full-report root needs a Capability tail composition."""

    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    assert ReportingTailComposition(tmp_path).workflow_specializer is not None


def test_full_report_tail_composition_binds_declared_cross_ports(
    tmp_path: Path,
) -> None:
    """An injected lifecycle supplies all declared Cross actions by name."""

    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    cross_runtime = _cross_port_with_all_declared_tools()
    composition = ReportingTailComposition(tmp_path, cross_runtime=cross_runtime)
    tools = composition.tool_implementations()

    assert "prepare-cross-owner-cohort" in tools
    assert "reduce-cross-owner-cohort" in tools
    assert "prepare-current-cross-owner-initial" in tools
    assert "prepare-current-cross-owner-local-review" in tools
    assert "prepare-current-cross-owner-recheck" in tools
    assert "complete-current-cross-owner-pipeline" in tools
    assert composition.agent_invokers == cross_runtime.agent_invokers


def test_full_report_tail_composition_binds_declared_chief_ports_and_invoker(
    tmp_path: Path,
) -> None:
    """The compiled full-report Chief cohort is owned by the Chief runtime."""

    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRequest,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )

    chief_runtime = _chief_port_with_all_declared_tools()
    composition = ReportingTailComposition(tmp_path, chief_runtime=chief_runtime)
    tools = composition.tool_implementations()
    public_runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        workflow_specializers=(composition.workflow_specializer,),
        additional_tool_implementations=tools,
    )
    _, _, plan = public_runtime.compile_plan(
        ReportRequest(
            operation="full_report",
            instruction="compile the full report tail",
            target_modules=list(REPORT_TAXONOMY),
            missing_evidence_policy="draft",
            preparation_mode="serial",
        )
    )
    chief_plan_tool_ids = {
        tool_id
        for workflow_id, child_plan in plan.subworkflow_plans.items()
        if workflow_id.startswith("distribution-chief-chapter-")
        for tool_id in child_plan.tool_ids
    }

    assert chief_plan_tool_ids <= tools.keys()
    assert composition.agent_invokers["chief-editor"] is chief_runtime.agent_invokers[
        "chief-editor"
    ]


def test_chief_tail_composition_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys, tempfile\n"
                "from pathlib import Path\n"
                "from types import SimpleNamespace\n"
                "from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime "
                "import ReportingTailComposition\n"
                "chief = SimpleNamespace(agent_invokers={'chief-editor': object()})\n"
                "ReportingTailComposition(Path(tempfile.mkdtemp()), chief_runtime=chief)\n"
                "print(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "[]"


def test_cross_composition_import_does_not_load_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                "import manyselves.capabilities.distribution_reporting.runtime.cross_owner_composition\n"
                "print(json.dumps(sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting'))))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert completed.stdout.strip() == "[]"


@pytest.mark.asyncio
async def test_full_report_tail_host_stops_at_first_unbound_cross_tool(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRequest,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )
    from manyselves.kernel.conversations import ConversationRegistry
    from manyselves.kernel.executors import RuntimeContext
    from manyselves.kernel.workflow import WorkflowState, WorkflowStatus
    from manyselves.runtime.conversation_store import FileConversationStore
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    store = InMemoryWorkflowStateStore()
    events = InMemoryWorkflowEventSink()
    composition = ReportingTailComposition(tmp_path)
    runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        workflow_specializers=(composition.workflow_specializer,),
        additional_tool_implementations=composition.tool_implementations(),
        state_store=store,
        events=events,
    )
    request = ReportRequest(
        operation="full_report",
        instruction="run the full report tail",
        target_modules=["2.1", "2.2", "2.3", "2.4", "2.5"],
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )

    definitions, contracts, root_plan = runtime.compile_plan(request)
    tail_plan = root_plan.subworkflow_plans["distribution-reporting-tail"]
    tools = runtime._tools(definitions, contracts, root_plan)
    assert tail_plan.workflow_id == "distribution-reporting-tail"
    cross_plan = root_plan.subworkflow_plans["distribution-cross-owner-cohort"]
    assert "distribution-cross-owner-2.1-pipeline" in cross_plan.workflow_ids
    assert "prepare-render-delivery" in tools
    assert "complete-delivery" in tools
    assert "prepare-cross-owner-cohort" not in tools

    run_id = "public-full-tail-boundary"
    state = WorkflowState.for_plan(
        run_id,
        tail_plan,
        initial_variables={"reporting-state": {"run_id": run_id}},
    )
    host = WorkflowRuntimeHost(runtime.executors, store, events)

    with pytest.raises(RuntimeError, match="missing tool adapter: prepare-cross-owner-cohort"):
        await host.execute(
            tail_plan,
            state,
            RuntimeContext(
                tools=tools,
                contracts=contracts,
                definitions=definitions,
                conversations=ConversationRegistry(FileConversationStore(tmp_path)),
                subworkflows=root_plan.subworkflow_plans,
            ),
        )

    persisted = store.load(run_id)
    assert persisted.status is WorkflowStatus.FAILED
    assert persisted.actions["run-cross"].status.value == "failed"
    assert "missing tool adapter: prepare-cross-owner-cohort" in (
        persisted.actions["run-cross"].error or ""
    )


def test_cross_owner_runtime_requires_existing_relation_digest_metadata(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
        CrossOwnerRuntime,
    )

    state = _cross_state()
    state.pop("module_artifact_refs")
    with pytest.raises(ValidationError):
        CrossOwnerRuntime(tmp_path).prepare(state)


def test_cross_owner_runtime_prepares_and_reduces_five_no_finding_owners(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
        CrossOwnerRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        CrossDecisionPack,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.cross_owner import (
        DeclarativeCrossOwnerInitialAgentResult,
    )

    runtime = CrossOwnerRuntime(tmp_path)
    prepared_state = runtime.prepare(_cross_state())
    outcomes = {}
    for owner_module_id in REPORT_TAXONOMY:
        context = runtime.prepare_initial(
            {
                "state": prepared_state,
                "owner_module_id": owner_module_id,
            }
        )
        assert context.preparation is not None
        assert context.preparation.envelope is not None
        assert context.preparation.envelope.input_contract_kind == "cross_owner_input"
        assert owner_module_id in context.preparation.envelope.inline_context
        result = DeclarativeCrossOwnerInitialAgentResult(
            status="completed",
            submission=CrossOwnerFindingSubmission(
                owner_module_id=owner_module_id,
                coverage=CrossReviewCoverageEntry(
                    module_id=owner_module_id,
                    checked_dimensions=[
                        "terminology",
                        "facts",
                        "risk_levels",
                        "dependencies",
                        "propagation",
                        "joint_verification",
                    ],
                ),
                findings=[],
                synthesis_inputs=[],
            ),
        )
        accepted = runtime.accept_initial(
            {"context": context, "result": result}
        )
        assert not runtime.initial_has_findings(accepted)
        outcomes[owner_module_id] = runtime.complete_owner_without_findings(accepted)

    resumed = runtime.prepare_initial(
        {"state": prepared_state, "owner_module_id": "2.1"}
    )
    assert resumed.status == "initial_resumed"
    assert resumed.acceptance is not None
    assert not runtime.initial_has_findings(resumed)

    reduced = runtime.reduce({"state": prepared_state, "outcomes": outcomes})
    assert reduced["cross_review_completion_ref"].endswith("cross-completion.json")
    assert reduced["cross_decision_pack_ref"].endswith("cross-decision-pack.json")
    pack = CrossDecisionPack.model_validate_json(
        (tmp_path / reduced["cross_decision_pack_ref"]).read_text(encoding="utf-8")
    )
    assert pack.run_id == "cross-runtime-run"
    assert pack.module_ids == list(REPORT_TAXONOMY)
    assert (tmp_path / reduced["cross_review_completion_ref"]).is_file()


@pytest.mark.asyncio
async def test_cross_owner_agent_invoker_uses_specialized_owner_session(
    tmp_path: Path,
) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
        CrossOwnerAgentInvoker,
        CrossOwnerRuntime,
    )

    runtime = CrossOwnerRuntime(tmp_path)
    state = runtime.prepare(_cross_state("cross-agent-run"))
    context = runtime.prepare_initial(
        {"state": state, "owner_module_id": "2.1"}
    )
    result_ref = "Work/runs/cross-agent-run/results/cross-owner-2.1.json"
    result_path = tmp_path / result_ref
    result_path.parent.mkdir(parents=True)
    result_path.write_text(
        AgentResult(
            task_id="cross-owner-2.1-r0-initial",
            run_id="cross-agent-run",
            agent_id="cross-module-reviewer",
            session_id="public-reporting:cross-owner-2.1",
            status=AgentRunStatus.COMPLETED,
            payload=CrossOwnerFindingSubmission(
                owner_module_id="2.1",
                coverage=CrossReviewCoverageEntry(
                    module_id="2.1",
                    checked_dimensions=[
                        "terminology",
                        "facts",
                        "risk_levels",
                        "dependencies",
                        "propagation",
                        "joint_verification",
                    ],
                ),
            ),
        ).model_dump_json(),
        encoding="utf-8",
    )
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    received: list[UserMessage] = []

    class ScriptedCrossLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.callback = None

        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                received.append(message)
                await bus.publish(
                    AgentResultMessage(
                        sender="cross-module-reviewer",
                        workflow_id=message.workflow_id,
                        task_id="cross-owner-2.1-r0-initial",
                        run_id=message.run_id,
                        result_path=result_ref,
                        task_attempt_id="",
                        session_id=message.session_id,
                    )
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    loops: list[ScriptedCrossLoop] = []

    def session_factory(runtime_id: str) -> ScriptedCrossLoop:
        loop = ScriptedCrossLoop(runtime_id)
        loops.append(loop)
        return loop

    execution = AgentExecutionService(bus, timeout=1)
    bridge = CrossOwnerAgentInvoker(
        tmp_path,
        execution=execution,
        session_factory=session_factory,
    )
    agent = AgentDefinition(
        id="cross-module-reviewer",
        version="1.0.0",
        description="Cross owner reviewer",
        instructions="只输出 Cross owner typed result。",
    )
    task = TaskDefinition(
        id="cross-owner-runtime-initial-review",
        version="1.0.0",
        description="Cross owner initial",
        agent=agent.id,
        objective="review cross-module interfaces",
        input_contract="reporting_tool_input",
        output_contract="declarative_cross_owner_initial_agent_result",
    )
    conversation = ConversationRecord(
        conversation_id="cross-conversation",
        key=ConversationKey(
            agent_id=agent.id,
            value="cross-owner-2.1",
            mode=ConversationMode.RUN,
        ),
        run_id="cross-agent-run",
    )
    try:
        first = await bridge.invoke(
            agent,
            task,
            context,
            conversation,
            task_id="invoke-cross-owner-2.1",
        )
        second = await bridge.invoke(
            agent,
            task,
            context,
            conversation,
            task_id="invoke-cross-owner-2.1-again",
        )
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert first.status == "ok"
    assert second.status == "ok"
    assert len(loops) == 1
    assert len(received) == 2
    assert received[0].session_id == received[1].session_id
    assert "cross_lane_specialization" in received[0].content
    assert "template_role_skill" not in received[0].content


@pytest.mark.asyncio
async def test_full_report_tail_host_drains_cross_and_stops_at_real_chief_boundary(
    tmp_path: Path,
) -> None:
    """The real compiled tail reaches Chief after all five Cross owner lanes."""

    from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
        CrossOwnerRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRequest,
    )
    from manyselves.capabilities.distribution_reporting.runtime.public_reporting import (
        PublicReportingWorkflowRuntime,
    )
    from manyselves.capabilities.distribution_reporting.runtime.reporting_tail_runtime import (
        ReportingTailComposition,
    )
    from manyselves.kernel.conversations import ConversationRegistry
    from manyselves.kernel.executors import RuntimeContext
    from manyselves.kernel.workflow import WorkflowState
    from manyselves.runtime.conversation_store import FileConversationStore
    from manyselves.runtime.state_store import InMemoryWorkflowStateStore
    from manyselves.runtime.workflow_host import (
        InMemoryWorkflowEventSink,
        WorkflowRuntimeHost,
    )

    run_id = "cross-tail-host-run"
    result_refs = {
        module_id: f"Work/runs/{run_id}/results/{module_id}.json"
        for module_id in REPORT_TAXONOMY
    }
    for module_id, result_ref in result_refs.items():
        result_path = tmp_path / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        result_path.write_text(
            AgentResult(
                task_id=f"cross-owner-{module_id}-r0-initial",
                run_id=run_id,
                agent_id="cross-module-reviewer",
                session_id=f"public-reporting:cross-owner-{module_id}",
                status=AgentRunStatus.COMPLETED,
                payload=CrossOwnerFindingSubmission(
                    owner_module_id=module_id,
                    coverage=CrossReviewCoverageEntry(
                        module_id=module_id,
                        checked_dimensions=[
                            "terminology",
                            "facts",
                            "risk_levels",
                            "dependencies",
                            "propagation",
                            "joint_verification",
                        ],
                    ),
                    findings=[],
                    synthesis_inputs=[],
                ),
            ).model_dump_json(),
            encoding="utf-8",
        )

    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    execution = AgentExecutionService(bus, timeout=2)
    loops: list[Any] = []

    class ScriptedCrossLoop:
        def __init__(self, runtime_id: str) -> None:
            self.runtime_id = runtime_id
            self.callback = None

        def restore_conversation(
            self,
            messages,
            *,
            task_boundaries=(),
            handoff_summary=None,
        ) -> None:
            del messages, task_boundaries, handoff_summary

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.runtime_id:
                    return
                owner_module_id = self.runtime_id.rsplit("cross-owner-", 1)[-1]
                await bus.publish(
                        AgentResultMessage(
                            sender="cross-module-reviewer",
                            workflow_id=message.workflow_id,
                            task_id=f"cross-owner-{owner_module_id}-r0-initial",
                            run_id=message.run_id,
                            result_path=result_refs[owner_module_id],
                            task_attempt_id="",
                        session_id=message.session_id,
                    )
                )

            self.callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self.callback is not None:
                bus.unsubscribe(UserMessage, self.callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    def session_factory(runtime_id: str) -> ScriptedCrossLoop:
        loop = ScriptedCrossLoop(runtime_id)
        loops.append(loop)
        return loop

    cross_runtime = CrossOwnerRuntime(
        tmp_path,
        agent_execution=execution,
        agent_session_factory=session_factory,
    )
    composition = ReportingTailComposition(tmp_path, cross_runtime=cross_runtime)
    module_runtime = SimpleNamespace(
        agent_invokers=composition.agent_invokers,
        agent_execution=execution,
        agent_session_factory=session_factory,
    )
    request = ReportRequest(
        operation="full_report",
        instruction="run the full report tail",
        target_modules=list(REPORT_TAXONOMY),
        missing_evidence_policy="draft",
        preparation_mode="serial",
    )
    public_runtime = PublicReportingWorkflowRuntime(
        tmp_path,
        input_snapshot=lambda _run_id: {},
        snapshot_content=lambda source, target: (target, "a" * 64, "blob"),
        runtime_photo_ids=lambda _evidence, _photos: None,
        module_runtime=module_runtime,
        workflow_specializers=(composition.workflow_specializer,),
        additional_tool_implementations=composition.tool_implementations(),
        state_store=InMemoryWorkflowStateStore(),
        events=InMemoryWorkflowEventSink(),
    )
    try:
        definitions, contracts, root_plan = public_runtime.compile_plan(request)
        tail_plan = root_plan.subworkflow_plans["distribution-reporting-tail"]
        state = WorkflowState.for_plan(
            run_id,
            tail_plan,
            initial_variables={"reporting-state": _cross_state(run_id)},
        )
        tools = public_runtime._tools(definitions, contracts, root_plan)
        with pytest.raises(
            RuntimeError,
            match="missing tool adapter: prepare-chief-chapter-cohort",
        ):
            await WorkflowRuntimeHost(
                public_runtime.executors,
                InMemoryWorkflowStateStore(),
                InMemoryWorkflowEventSink(),
            ).execute(
                tail_plan,
                state,
                RuntimeContext(
                    tools=tools,
                    agents=public_runtime._agent_invokers(),
                    contracts=contracts,
                    definitions=definitions,
                    conversations=ConversationRegistry(
                        FileConversationStore(tmp_path)
                    ),
                    subworkflows=root_plan.subworkflow_plans,
                ),
            )
    finally:
        await execution.close_workflow("public-reporting")
        bus.shutdown()
        await bus_task

    assert len(loops) == len(REPORT_TAXONOMY)
    assert (tmp_path / f"Work/runs/{run_id}/reviews/cross-completion.json").is_file()
    assert (tmp_path / f"Work/runs/{run_id}/reviews/cross-decision-pack.json").is_file()
