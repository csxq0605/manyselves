"""Each project photo is read once per run, as an image, against its bound records."""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.capabilities.distribution_reporting.runtime.joint_dossiers import (
    build_joint_dossiers,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ObservationAssessment,
    PartAssessment,
)
from manyselves.capabilities.distribution_reporting.runtime.models.photo_reading import (
    PhotoReadingInput,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    PreparationContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
    ReportRequest,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.photo_reader_provider import (
    PhotoReaderProviderRuntime,
)
from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
    build_photo_reading_tool_implementations,
    load_photo_readings,
)
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    prepare_module_fact_context,
)
from manyselves.config.schema import AgentDefaults
from manyselves.interfaces.types import UserMessage
from manyselves.kernel.contracts import build_contract_catalog
from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRecord,
)
from manyselves.kernel.definitions import (
    AgentDefinition,
    DefinitionKind,
    TaskDefinition,
    WorkflowDefinition,
)
from manyselves.kernel.executors import RuntimeContext, build_builtin_executor_registry
from manyselves.kernel.ports import AgentInvocationOutcome
from manyselves.kernel.workflow import WorkflowCompiler, WorkflowState, WorkflowStatus
from manyselves.runtime.agent_execution import AgentExecutionService
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost
from tests.capabilities.distribution_reporting.test_project_fact_packet import _write_facts
from tests.capabilities.distribution_reporting.test_public_entrypoint_workflows import (
    _registry_with_existing_workflow_specializers,
)

RUN_ID = "facts"
SUBMISSION = {
    "kind": "photo_reading_submission",
    "visible": "柜内进线端子，热像显示最高约35℃",
    "record_match": "matches",
    "reason": "图中读数与记录一致",
    "needs_review": False,
}


def _item(evidence_id: str, submodule_id: str, photo_refs: list[str], row: int = 3) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id, subject="A房/柜A", fact="进线端子温度=35℃",
        module_id=submodule_id.split(".")[0] + "." + submodule_id.split(".")[1],
        submodule_id=submodule_id, photo_refs=photo_refs,
        source=SourceLocation(file_id="s4-4", path=Path("Inputs/S4-4.xlsx"),
                              sheet="红外热成像检测记录表", cell=f"G{row}", row=row, column="G"),
    )


def _photo(workspace: Path, photo_id: str, primary: str) -> PhotoAsset:
    path = Path("Work/assets") / f"{photo_id}.png"
    (workspace / path).parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (2, 2)).save(workspace / path)
    return PhotoAsset(id=photo_id, path=path, sha256="0" * 64, media_type="image/png",
                      source_member=f"xl/media/{photo_id}.png", primary_evidence_id=primary)


def _context(workspace: Path, photos: int) -> PreparationContext:
    items, assets = [], []
    for number in range(1, photos + 1):
        photo_id = f"P-{number:04d}"
        owner, copy = f"E-{2 * number - 1:04d}", f"E-{2 * number:04d}"
        # The routed copy comes first so primary-first ordering is visible.
        items += [_item(copy, "2.4.4", [photo_id], number), _item(owner, "2.2.2.1", [photo_id], number)]
        assets.append(_photo(workspace, photo_id, owner))
    return PreparationContext(
        run_id=RUN_ID,
        request=ReportRequest(operation="full_report", instruction="photo reading"),
        evidence_items=items, photo_assets=assets,
    )


class _Invoker:
    def __init__(self, submission: dict | None = None) -> None:
        self.inputs: list[PhotoReadingInput] = []
        self.conversations: list[str] = []
        self.submission = submission or SUBMISSION

    async def invoke_with_recovery(self, agent, task, value, conversation, *, task_id, recovery_policy):
        del agent, task, task_id, recovery_policy
        self.inputs.append(PhotoReadingInput.model_validate(
            value.model_dump(mode="json") if hasattr(value, "model_dump") else value
        ))
        self.conversations.append(conversation.conversation_id)
        return AgentInvocationOutcome(status="ok", result=dict(self.submission))


async def _run_workflow(workspace: Path, context: PreparationContext, invoker: _Invoker):
    _capability, registry = load_distribution_reporting_capability()
    workflow = registry.require(DefinitionKind.WORKFLOW, "distribution-photo-reading")
    assert isinstance(workflow, WorkflowDefinition)
    plan = WorkflowCompiler(build_builtin_executor_registry()).compile(workflow, registry)
    contracts = build_contract_catalog(registry)
    factory = CapabilityToolAdapterFactory(
        "distribution-reporting", build_photo_reading_tool_implementations(workspace), contracts,
    )
    tool_ids = set(plan.tool_ids)
    for child in plan.subworkflow_plans.values():
        tool_ids |= set(child.tool_ids)
    tools = {
        tool_id: factory.build(registry.require(DefinitionKind.TOOL, tool_id))
        for tool_id in tool_ids
        if registry.require(DefinitionKind.TOOL, tool_id).model_visible is False
    }
    host = WorkflowRuntimeHost(
        build_builtin_executor_registry(), InMemoryWorkflowStateStore(), InMemoryWorkflowEventSink(),
    )
    state = await host.execute(
        plan,
        WorkflowState.for_plan(RUN_ID, plan, initial_variables={"preparation-context": context}),
        RuntimeContext(tools=tools, contracts=contracts, definitions=registry,
                       agents={"photo-reader": invoker}, subworkflows=plan.subworkflow_plans),
    )
    assert state.status is WorkflowStatus.COMPLETED
    result = state.outputs["result"]
    return result.model_dump(mode="json") if hasattr(result, "model_dump") else result


@pytest.mark.asyncio
async def test_workflow_reads_each_photo_once_in_fresh_conversations(tmp_path: Path) -> None:
    invoker = _Invoker()
    summary = await _run_workflow(tmp_path, _context(tmp_path, 5), invoker)

    assert sorted(item.photo_id for item in invoker.inputs) == [f"P-{n:04d}" for n in range(1, 6)]
    assert len(set(invoker.conversations)) == 5
    first = next(item for item in invoker.inputs if item.photo_id == "P-0001")
    assert [record.evidence_id for record in first.records] == ["E-0001", "E-0002"]
    assert first.records[0].primary and not first.records[1].primary
    assert summary["read_count"] == 5
    readings = load_photo_readings(tmp_path, RUN_ID)
    assert set(readings) == {f"P-{n:04d}" for n in range(1, 6)}
    assert readings["P-0001"].evidence_ids == ["E-0001", "E-0002"]

    # Same run again: every photo is already read, so the model is not called.
    again = _Invoker()
    summary = await _run_workflow(tmp_path, _context(tmp_path, 5), again)
    assert again.inputs == []
    assert summary["reused_count"] == 5 and summary["read_count"] == 0
    assert set(load_photo_readings(tmp_path, RUN_ID)) == set(readings)


@pytest.mark.asyncio
async def test_workflow_without_photos_skips_the_model(tmp_path: Path) -> None:
    invoker = _Invoker()
    summary = await _run_workflow(tmp_path, _context(tmp_path, 0), invoker)
    assert invoker.inputs == []
    assert summary["photo_count"] == 0 and summary["read_count"] == 0


def test_root_workflows_read_photos_before_any_module_writes() -> None:
    _capability, registry = load_distribution_reporting_capability()
    for workflow_id, writer in (
        ("full-report", "report-ledger"),
        ("module-report", "distribution-module-cohort"),
    ):
        workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        children = [a["workflow"] for a in workflow.actions if a["kind"] == "subworkflow"]
        assert children.index("distribution-photo-reading") < children.index(writer)
        specialized = _registry_with_existing_workflow_specializers()
        plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            specialized.require(DefinitionKind.WORKFLOW, workflow_id), specialized,
        )
        assert "distribution-photo-reading" in plan.workflow_ids


def _agent_and_task() -> tuple[AgentDefinition, TaskDefinition]:
    _capability, registry = load_distribution_reporting_capability()
    agent = registry.require(DefinitionKind.AGENT, "photo-reader")
    task = registry.require(DefinitionKind.TASK, "photo-reading")
    assert isinstance(agent, AgentDefinition) and isinstance(task, TaskDefinition)
    return agent, task


def _conversation(conversation_id: str) -> ConversationRecord:
    return ConversationRecord(
        conversation_id=conversation_id,
        key=ConversationKey(agent_id="photo-reader", value="photo-reading-lane-1",
                            mode=ConversationMode.RUN),
        run_id=RUN_ID,
    )


@pytest.mark.asyncio
async def test_provider_sends_the_image_and_reuses_a_completed_reading(tmp_path: Path) -> None:
    bus = MessageBus()
    bus_task = asyncio.create_task(bus.process_queue())
    received: list[UserMessage] = []
    restored: list[tuple] = []

    class Loop:
        def __init__(self, kwargs: dict) -> None:
            self.kwargs = kwargs
            self.tools = kwargs["tools"]
            self._callback = None

        def restore_conversation(self, messages, *, task_boundaries=(), handoff_summary=None):
            del task_boundaries, handoff_summary
            restored.append(tuple(messages))

        async def start(self) -> None:
            async def respond(message: UserMessage) -> None:
                if message.agent_type != self.kwargs["agent_type"]:
                    return
                received.append(message)
                await self.tools.get("submit_result")(**SUBMISSION)

            self._callback = respond
            bus.subscribe(UserMessage, respond)

        async def stop(self) -> None:
            if self._callback is not None:
                bus.unsubscribe(UserMessage, self._callback)

        async def wait_until_turn_complete(self) -> None:
            return None

    services = RuntimeServicesView(
        workspace=tmp_path, bus=bus, active_provider=object(),
        agent_defaults=AgentDefaults(), global_knowledge_root=None,
    )
    runtime = PhotoReaderProviderRuntime(
        services, execution=AgentExecutionService(bus, timeout=1),
        loop_builder=lambda **kwargs: Loop(kwargs),
    )
    agent, task = _agent_and_task()
    context = _context(tmp_path, 2)
    plan = build_photo_reading_tool_implementations(tmp_path)["prepare-photo-reading"](context)
    first, second = plan["lanes"]["1"][0], plan["lanes"]["2"][0]
    try:
        outcome = await runtime.invoke(agent, task, first, _conversation("c-1"), task_id="read-photo-1")
        reused = await runtime.invoke(agent, task, first, _conversation("c-2"), task_id="read-photo-1")
        other = await runtime.invoke(agent, task, second, _conversation("c-3"), task_id="read-photo-1")
    finally:
        await runtime.close()
        bus.shutdown()
        await bus_task

    assert outcome.status == "ok" and outcome.result == SUBMISSION
    assert reused.status == "ok" and reused.result == SUBMISSION
    assert [message.images for message in received] == [
        [{"path": str((tmp_path / "Work/assets/P-0001.png").resolve()), "media_type": "image/png"}],
        [{"path": str((tmp_path / "Work/assets/P-0002.png").resolve()), "media_type": "image/png"}],
    ]
    assert "E-0001" in received[0].content and "进线端子温度=35℃" in received[0].content
    assert other.status == "ok"
    # The lane session is reused, but the next photo starts with an empty history.
    assert () in restored


def _module(part_id: str, evidence_id: str) -> SimpleNamespace:
    return SimpleNamespace(
        assessments={part_id: PartAssessment(records=[ObservationAssessment.model_validate(
            {"evidence_ids": [evidence_id], "disposition": "no_concern", "narrative_anchor": "端子"}
        )])},
        submodule_narratives={part_id: "端子温度正常。"},
    )


@pytest.mark.asyncio
async def test_readings_reach_fact_packets_and_conflicts_keep_a_dossier(tmp_path: Path) -> None:
    context = _context(tmp_path, 1)
    _write_facts(tmp_path, context.evidence_items)
    modules = {"2.2": _module("2.2.2.1", "E-0001")}
    assert build_joint_dossiers(tmp_path, RUN_ID, modules) == []

    conflict = {**SUBMISSION, "record_match": "conflicts", "reason": "图中读数约60℃", "needs_review": True}
    await _run_workflow(tmp_path, context, _Invoker(conflict))

    packet = prepare_module_fact_context(tmp_path, RUN_ID, "2.2").inline_context
    assert '"photo_reading":"P-0001：柜内进线端子' in packet
    assert "与记录不符（图中读数约60℃）；需人工复核" in packet
    assert "逐张读图结果" in packet

    [dossier] = build_joint_dossiers(tmp_path, RUN_ID, modules)
    assert dossier["reasons"] == ["photo_conflicts_record"]
    assert {"module_id": "2.2", "submodule_id": "2.2.2.1"} in dossier["submodules"]
    assert "2.4" in dossier["module_ids"]
    fact = next(fact for fact in dossier["facts"] if fact["evidence_id"] == "E-0001")
    assert fact["photo_conflicts"] is True and fact["photo_readings"][0].startswith("P-0001：")
