"""Each project photo is read once per run, as an image, against its bound records."""

from __future__ import annotations

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
from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
    build_photo_reading_tool_implementations,
    load_photo_readings,
)
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    prepare_module_fact_context,
)
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
from manyselves.runtime.state_store import InMemoryWorkflowStateStore
from manyselves.runtime.tool_adapter import CapabilityToolAdapterFactory
from manyselves.runtime.workflow_host import InMemoryWorkflowEventSink, WorkflowRuntimeHost
from tests.capabilities.distribution_reporting.support import write_facts as _write_facts

RUN_ID = "facts"
SUBMISSION = {
    "kind": "photo_reading_submission",
    "visible": "柜内进线端子的热像图",
    "transcription": [{"label": "最高温度", "text": "35.1℃", "legible": True}],
    "photo_date": "2025-06-12 10:31",
    "needs_review": False,
}


def _item(evidence_id: str, submodule_id: str, photo_refs: list[str], row: int = 3) -> EvidenceItem:
    return EvidenceItem(
        id=evidence_id, subject="A房/柜A", fact="进线端子温度=35℃", value=35, unit="℃",
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
    for workflow_id, writer in (("full-report", "report-ledger"),):
        workflow = registry.require(DefinitionKind.WORKFLOW, workflow_id)
        children = [a["workflow"] for a in workflow.actions if a["kind"] == "subworkflow"]
        assert children.index("distribution-photo-reading") < children.index(writer)
        plan = WorkflowCompiler(build_builtin_executor_registry()).compile(
            registry.require(DefinitionKind.WORKFLOW, workflow_id), registry,
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

    # The reader only copies; the program finds the copied 60.2 is not the record's 35.
    conflict = {
        **SUBMISSION,
        "transcription": [
            {"label": "最高温度", "text": "60.2℃", "legible": True},
            {"label": "柜号", "text": "D1701", "legible": False},
        ],
        "readings": [{"quantity": "记录值", "value": 60.2, "unit": "℃", "legible": True}],
    }
    await _run_workflow(tmp_path, context, _Invoker(conflict))

    packet = prepare_module_fact_context(tmp_path, RUN_ID, "2.2").inline_context
    assert '"photo_reading":"P-0001：柜内进线端子' in packet
    # The table record stands: a reading that differs shows no numbers to cite.
    assert (
        "P-0001：柜内进线端子的热像图；照片日期：2025-06-12 10:31；"
        "程序比对：照片读数未能确认，以表格记录为准" in packet
    )
    assert "60.2" not in packet
    assert "逐张读图结果" in packet

    [dossier] = build_joint_dossiers(tmp_path, RUN_ID, modules)
    assert dossier["reasons"] == ["photo_conflicts_record"]
    assert {"module_id": "2.2", "submodule_id": "2.2.2.1"} in dossier["submodules"]
    assert "2.4" in dossier["module_ids"]
    fact = next(fact for fact in dossier["facts"] if fact["evidence_id"] == "E-0001")
    assert fact["photo_conflicts"] is True and fact["photo_readings"][0].startswith("P-0001：")


def test_program_compares_structured_readings_by_quantity_and_names_another_record() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
        compare_readings,
        readings_agree,
    )

    bound = {"residual_current": [11.3]}
    others = {"residual_current": [[2.9, "E-7"]]}

    def read(value, legible=True, quantity="residual_current"):
        return [{"quantity": quantity, "value": value, "unit": "A", "legible": legible}]

    assert compare_readings(read(11.27), bound)[0] == "matches"
    assert compare_readings(read(None, False), bound)[0] == "cannot_tell"
    # A quantity the bound records do not declare is not compared.
    assert compare_readings(read(23, quantity="ambient_temperature"), bound)[0] == "cannot_tell"
    match, reason = compare_readings(read(2.9), bound, others)
    assert match == "conflicts" and "E-7" in reason
    assert readings_agree(read(11.3), read(11.27))[0] is True
    agrees, why = readings_agree(read(11.3), read(2.9))
    assert agrees is False and "读数待核" in why


def test_items_a_first_reading_left_blank_or_illegible_are_read_again_and_compared() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
        unread_agree,
        unread_labels,
    )

    first = [
        {"label": "1月", "text": "√", "legible": True},
        {"label": "5月", "text": "", "legible": True},
        {"label": "面板读数", "text": "", "legible": False},
    ]
    labels = unread_labels(first)
    assert labels == ["5月", "面板读数"]
    agrees, why = unread_agree(first, [{"label": "5月", "text": "√ 王", "legible": True}], labels)
    assert agrees is False and "5月：首读为空白" in why
    # A second reading that also sees nothing, or leaves the item out, settles nothing.
    assert unread_agree(first, [{"label": "5月", "text": "", "legible": True}], labels)[0] is True
    assert unread_agree(first, [], labels)[0] is True


def test_review_gets_only_photos_whose_readings_are_in_doubt() -> None:
    from manyselves.capabilities.distribution_reporting.domain.judgment_ledger import JudgmentRow
    from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import photos_to_check
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import PhotoReading

    row = JudgmentRow.model_validate(
        {"id": "J-1", "submodule_id": "2.2.1", "object": "1#", "checkpoint": "消防",
         "disposition": "concern", "evidence_status": "现场确认", "evidence_ids": ["E-1"],
         "photo_ids": ["P-1", "P-2", "P-3"], "basis": "b", "risk_level": "中", "hazard": "h",
         "hazard_conditions": ["c"], "action": {"measure": "m", "priority": "一般", "timeframe": "一月"}}
    )
    reference = {"photos": {p: {"path": f"{p}.jpg", "media_type": "image/jpeg"} for p in ("P-1", "P-2", "P-3")}}

    def reading(photo_id, **extra):
        return PhotoReading(photo_id=photo_id, visible="v", record_match="cannot_tell",
                            reason="r", needs_review=True, **extra)

    readings = {
        "P-1": reading("P-1"),  # the reader asked for a look: the re-read answers that
        "P-2": reading("P-2", reread_agrees=False, reread_reason="两次读图不一致"),
        "P-3": reading("P-3").model_copy(update={"record_match": "conflicts"}),
    }
    assert [p["photo_id"] for p in photos_to_check([row], reference, readings)] == ["P-2", "P-3"]




def test_a_photo_is_read_again_only_where_a_judgment_rests_on_it() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.photo_reading import (
        PhotoReading,
    )
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
        rests_on_photo,
    )

    def reading(*readings):
        return PhotoReading(photo_id="P-1", visible="v", record_match="cannot_tell", reason="r",
                            needs_review=False, readings=list(readings))

    table_row = SimpleNamespace(evidence_status="现场确认")
    photo_row = SimpleNamespace(evidence_status="照片待核")
    shown = {"quantity": "temperature", "value": 48.7, "legible": True}
    # A photo backing a table record is not read again ...
    assert not rests_on_photo([table_row], reading(), {})
    assert not rests_on_photo([table_row], reading(shown), {"temperature": [48.7]})
    # ... one the author marked 照片待核, or one showing a number the table lacks, is.
    assert rests_on_photo([photo_row], reading(), {})
    assert rests_on_photo([table_row], reading(shown), {})


def test_a_reread_sees_the_photo_and_its_four_quarters_enlarged(tmp_path: Path) -> None:
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import zoom_tiles

    Image.new("RGB", (908, 1215), "white").save(tmp_path / "p.jpg")
    tiles = zoom_tiles(tmp_path, "p.jpg", "tiles", "P-1")
    assert [t["part"][:2] for t in tiles] == ["左上", "右上", "左下", "右下"]
    for tile in tiles:
        with Image.open(tmp_path / tile["path"]) as image:
            assert max(image.size) == 1568
    assert zoom_tiles(tmp_path, "missing.jpg", "tiles", "P-2") == []
    reread = load_distribution_reporting_capability()[1].require(
        DefinitionKind.TASK, "photo-reread"
    )
    assert reread.image.list_field == "images"


def test_labels_follow_the_table_and_leave_out_what_the_photo_cannot_show() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.models.photo_reading import (
        PhotoReading,
    )
    from manyselves.capabilities.distribution_reporting.runtime.photo_reading import (
        UNSTABLE,
        reading_label,
    )

    card = [
        {"label": "3月", "text": "已检", "legible": True},
        {"label": "5月", "text": "", "legible": True, "after_photo_date": True},
    ]
    base = PhotoReading(photo_id="P-1", visible="巡检卡", transcription=card, photo_date="2026-04-02",
                        readings=[{"quantity": "current", "value": 1.97, "legible": True}],
                        record_match="matches", reason="可见", needs_review=False)
    label = reading_label(base)
    assert "3月=已检" in label and "5月" not in label and "拍摄日期之后的1格未计入" in label
    unstable = reading_label(base.model_copy(update={"reread_agrees": False}))
    assert unstable.endswith(UNSTABLE) and "1.97" not in unstable and "已检" not in unstable
