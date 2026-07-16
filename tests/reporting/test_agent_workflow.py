import asyncio
from pathlib import Path

import pytest
from docx import Document
from PIL import Image

from autoreport.core.loops.bus import MessageBus
from autoreport.core.providers.base import LLMProvider
from autoreport.core.reporting.agentic_models import (
    AgentResult,
    AgentRunStatus,
    AuditSubmission,
    ClaimRecord,
    CrossReviewSubmission,
    EditedReportSubmission,
    ModuleSubmission,
    PlanSubmission,
    TaskEnvelope,
)
from autoreport.core.reporting.models import (
    REPORT_MODULE_IDS,
    EvidenceItem,
    PhotoAsset,
    ReportRequest,
    ReviewIssue,
    SourceLocation,
)
from autoreport.core.reporting.service import ReportingService
from autoreport.core.reporting.taxonomy import REPORT_TAXONOMY
from autoreport.core.reporting.workflow import ReportWorkflowRunner
from autoreport.core.tools.task_board import TaskBoard


class ScriptedWorkflowAgents:
    def __init__(self, target_modules=REPORT_MODULE_IDS, photo_ids=()):
        self.target_modules = tuple(target_modules)
        self.photo_ids = tuple(photo_ids)
        self.active_specialists = 0
        self.max_active_specialists = 0
        self.closed = False
        self.specialist_ids: list[str] = []
        self.envelopes: dict[str, TaskEnvelope] = {}

    async def run(self, definition, envelope, shared_artifacts, *, workflow_id, session_key=None):
        agent_id = definition.id
        if agent_id == "report-planner":
            payload = PlanSubmission(
                module_tasks=[
                    TaskEnvelope(
                        task_id=f"planned-{module_id}",
                        run_id=envelope.run_id,
                        agent_id=f"module-{module_id}-specialist",
                        objective=f"分析模块 {module_id}",
                    )
                    for module_id in self.target_modules
                ],
                rationale="五个专业并行分析后汇合。",
            )
        elif agent_id.startswith("module-"):
            self.specialist_ids.append(agent_id)
            self.envelopes[agent_id] = envelope
            module_id = agent_id.removeprefix("module-").removesuffix("-specialist")
            self.active_specialists += 1
            self.max_active_specialists = max(self.max_active_specialists, self.active_specialists)
            await asyncio.sleep(0.02)
            self.active_specialists -= 1
            narrative = f"模块 {module_id} 从本专业机理出发形成主动分析。"
            photo_claim = module_id == "2.4" and bool(self.photo_ids)
            claim_text = "1A2 柜连接状态需要复核" if photo_claim else narrative
            if photo_claim:
                narrative += claim_text + "。"
            payload = ModuleSubmission(
                module_id=module_id,
                markdown=narrative,
                submodule_narratives={
                    submodule_id: f"{submodule_id}：{narrative}"
                    for submodule_id in REPORT_TAXONOMY[module_id].submodules
                },
                claims=[
                    ClaimRecord(
                        id=f"C-{module_id}-001",
                        module_id=module_id,
                        submodule_id=next(iter(REPORT_TAXONOMY[module_id].submodules)),
                        text=claim_text,
                        claim_type="technical_interpretation",
                        source_ids=["E-0001"] if photo_claim else [],
                        confidence=1.0 if photo_claim else 0.5,
                        footnote_required=photo_claim,
                        unresolved=not photo_claim,
                    )
                ],
                source_ids=[],
                unresolved_questions=["尚无客户事实，保留判断边界"],
                revision=envelope.revision,
            )
        elif agent_id == "evidence-auditor":
            module_id = envelope.task_id.split("-")[1]
            payload = AuditSubmission(
                module_id=module_id,
                approved=True,
                issues=[],
                checked_claim_ids=[f"C-{module_id}-001"],
            )
        elif agent_id == "cross-module-reviewer":
            payload = CrossReviewSubmission(
                approved=True,
                global_constraints=["所有未知均保持为未知"],
            )
        elif agent_id == "chief-editor":
            module_narratives = {
                module_id: f"模块 {module_id} 从本专业机理出发形成主动分析。"
                for module_id in REPORT_MODULE_IDS
            }
            if self.photo_ids:
                module_narratives["2.4"] += "1A2 柜连接状态需要复核。"
            payload = EditedReportSubmission(
                title="配电安全专家咨询报告",
                overview="本报告按五个专业视角综合审视配电安全。",
                module_narratives=module_narratives,
                conclusion="现有资料不足以形成客户现场事实结论，后续应补充核验。",
                protected_claim_ids=[f"C-{module_id}-001" for module_id in REPORT_MODULE_IDS],
                citation_anchors=(
                    {"C-2.4-001": "1A2 柜连接状态需要复核"} if self.photo_ids else {}
                ),
                photo_ids=list(self.photo_ids),
            )
        else:
            raise AssertionError(agent_id)
        return AgentResult(
            task_id=envelope.task_id,
            run_id=envelope.run_id,
            agent_id=agent_id,
            session_id=session_key or envelope.task_id,
            status=AgentRunStatus.COMPLETED,
            payload=payload,
        )

    async def close_workflow(self, workflow_id):
        self.closed = True


class NeverCalledProvider(LLMProvider):
    def __init__(self):
        super().__init__("test", model="never-called")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("ReportWorkflowRunner must use the injected scripted agents")


class DriftingRevisionAgents(ScriptedWorkflowAgents):
    async def run(self, definition, envelope, shared_artifacts, *, workflow_id, session_key=None):
        result = await super().run(
            definition,
            envelope,
            shared_artifacts,
            workflow_id=workflow_id,
            session_key=session_key,
        )
        if definition.id == "evidence-auditor" and envelope.revision == 0:
            result.payload = AuditSubmission(
                module_id="2.4",
                approved=False,
                issues=[
                    ReviewIssue(
                        module_id="2.4",
                        submodule_id="2.4.1.1",
                        kind="unsupported",
                        message="只修订容量判断",
                        severity="blocking",
                    )
                ],
                checked_claim_ids=[],
            )
        if definition.id == "module-2.4-specialist" and envelope.revision == 1:
            assert isinstance(result.payload, ModuleSubmission)
            payload = result.payload.model_dump(mode="python")
            payload["submodule_narratives"]["2.4.2.2"] = "越界修改接地子模块"
            result.payload = ModuleSubmission.model_validate(payload)
        return result


@pytest.mark.asyncio
async def test_full_five_module_workflow_runs_parallel_barrier_editor_and_handoff_docx(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "AUTOREPORT_HANDOFF_DOCX_CORE",
        str(tmp_path / "external-core-must-not-be-used.py"),
    )
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    agents = ScriptedWorkflowAgents()
    state = {
        "run_id": "run-full",
        "request": ReportRequest(
            instruction="生成完整配电安全专家报告",
            missing_evidence_policy="draft",
        ),
    }

    await ReportWorkflowRunner(service, agents).run(state)

    assert agents.max_active_specialists == 5
    assert agents.closed is True
    assert set(state["module_submissions"]) == set(REPORT_MODULE_IDS)
    report_path = tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx"
    assert report_path.is_file()
    rendered = Document(report_path)
    text = "\n".join(paragraph.text for paragraph in rendered.paragraphs)
    assert "模块 2.1 从本专业机理出发形成主动分析。" in text
    assert "模块 2.5 从本专业机理出发形成主动分析。" in text


@pytest.mark.asyncio
async def test_partial_request_runs_only_target_module_and_propagates_requirements(
    tmp_path: Path,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    agents = ScriptedWorkflowAgents(target_modules=("2.4",))
    state = {
        "run_id": "run-partial",
        "request": ReportRequest(
            instruction="只重写设备模块",
            target_modules=["2.4"],
            execution_requirements=["深度核对现场图片"],
            missing_evidence_policy="draft",
        ),
    }

    await ReportWorkflowRunner(service, agents).run(state)

    assert agents.specialist_ids == ["module-2.4-specialist"]
    assert "深度核对现场图片" in agents.envelopes["module-2.4-specialist"].constraints
    assert (tmp_path / "Outputs/Modules/2.4.md").is_file()
    assert not (tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx").exists()


@pytest.mark.asyncio
async def test_workflow_rejects_revision_that_changes_unrequested_submodule(
    tmp_path: Path,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    agents = DriftingRevisionAgents(target_modules=("2.4",))
    state = {
        "run_id": "run-drift",
        "request": ReportRequest(
            instruction="修订设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="draft",
        ),
    }

    with pytest.raises(Exception, match="protected submodule 2.4.2.2"):
        await ReportWorkflowRunner(service, agents).run(state)


@pytest.mark.asyncio
async def test_active_workflow_passes_traceable_photo_to_renderer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    photo_path = tmp_path / "Work/assets/IMG-1.png"
    photo_path.parent.mkdir(parents=True)
    Image.new("RGB", (40, 30), color="red").save(photo_path)
    template_photo_count = len(Document(service.report_template_path).inline_shapes)

    async def normalize(state: dict) -> None:
        state["evidence_items"] = [
            EvidenceItem(
                id="E-0001",
                subject="1A2 柜",
                fact="连接点存在异常",
                source=SourceLocation(file_id="F-1", path=Path("Inputs/check.xlsx"), cell="A2"),
                module_id="2.4",
                submodule_id="2.4.1.1",
                photo_refs=["IMG-1"],
            )
        ]
        state["photo_assets"] = [
            PhotoAsset(
                id="IMG-1",
                path=photo_path.relative_to(tmp_path),
                sha256="test",
                media_type="image/png",
                source_member="media/image1.png",
            )
        ]

    monkeypatch.setattr(service, "_normalize_evidence", normalize)
    state = {
        "run_id": "run-photo",
        "request": ReportRequest(
            instruction="生成带现场图片的完整报告",
            missing_evidence_policy="draft",
        ),
    }

    await ReportWorkflowRunner(service, ScriptedWorkflowAgents(photo_ids=("IMG-1",))).run(state)

    rendered = Document(tmp_path / "Outputs/Reports/配电安全专家咨询报告.docx")
    assert len(rendered.inline_shapes) == template_photo_count + 1
