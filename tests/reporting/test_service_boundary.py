import asyncio
import json
from pathlib import Path

import pytest
from docx import Document

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.decisions import EvidenceDecisionStore
from manyselves.core.reporting.models import EvidenceDecisionRequest, OutputArtifact, ReportRequest
from manyselves.core.reporting.service import ReportingService
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.workflow import AgentWorkflowBlocked, ReportWorkflowRunner
from manyselves.core.tools.reporting_tool import (
    ResumeReportingWorkflowTool,
    RunReportingWorkflowTool,
)
from manyselves.core.tools.task_board import TaskBoard


class TemplateResolutionProvider(LLMProvider):
    def __init__(self):
        super().__init__("test", model="never-called")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("template resolution must not call the provider")


@pytest.mark.asyncio
async def test_agent_blocked_state_is_preserved(tmp_path: Path, monkeypatch) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=TemplateResolutionProvider(),
    )

    async def blocked(_self, _state: dict) -> None:
        raise AgentWorkflowBlocked("chief-editor", "edit", "missing reviewed modules")

    monkeypatch.setattr(ReportWorkflowRunner, "run", blocked)
    result = await service.run(ReportRequest(instruction="生成报告"))
    assert result.status == "blocked"
    assert result.error == "missing reviewed modules"


def test_project_report_template_takes_priority_without_restarting_service(
    tmp_path: Path,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=TemplateResolutionProvider(),
    )

    assert service.report_template_source == "packaged"
    assert service.report_template_path == service.packaged_report_template_path

    project_template = tmp_path / "Templates/report_template.docx"
    project_template.parent.mkdir(parents=True)
    Document().save(project_template)

    assert service.report_template_source == "project"
    assert service.report_template_path == project_template


def test_project_report_template_path_must_be_a_file(tmp_path: Path) -> None:
    (tmp_path / "Templates/report_template.docx").mkdir(parents=True)
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=TemplateResolutionProvider(),
    )

    with pytest.raises(ValueError, match="must be a DOCX file"):
        service.resolve_report_template()


def test_reporting_service_rejects_missing_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an LLM provider"):
        ReportingService(
            tmp_path,
            bus=MessageBus(),
            task_board=TaskBoard(),
            llm_provider=None,  # type: ignore[arg-type]
        )


def test_reporting_tool_rejects_missing_provider(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="requires an LLM provider"):
        RunReportingWorkflowTool(
            workspace=tmp_path,
            bus=MessageBus(),
            task_board=TaskBoard(),
            llm_provider=None,  # type: ignore[arg-type]
        )


@pytest.mark.asyncio
async def test_service_returns_resumable_decision_before_calling_provider_when_evidence_requires_confirmation(
    tmp_path: Path,
) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("blocked workflow must not call the provider")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )

    result = await service.run(
        ReportRequest(
            instruction="生成设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="ask",
        )
    )

    assert result.status == "needs_user_decision"
    assert result.decision_id
    assert result.missing_evidence
    assert result.error is None
    workflow_state = json.loads(
        (tmp_path / f"Work/runs/{result.run_id}/workflow-state.json").read_text(encoding="utf-8")
    )
    assert workflow_state["status"] == "blocked"
    decision = json.loads(
        (tmp_path / f"Work/runs/{result.run_id}/decisions/{result.decision_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert decision["allowed_actions"] == ["supplement", "draft", "skip", "stop"]


@pytest.mark.asyncio
async def test_supplement_rescans_the_same_run_after_process_restart(tmp_path: Path) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("coverage should block before provider use")

    first = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    pending = await first.run(
        ReportRequest(
            instruction="生成设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="ask",
        )
    )

    restarted = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    rescanned = await restarted.resume(pending.decision_id, "supplement", "已补充资料")

    assert rescanned.run_id == pending.run_id
    assert rescanned.status == "needs_user_decision"
    assert rescanned.decision_id != pending.decision_id


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["draft", "skip"])
async def test_draft_and_skip_resume_same_run_with_explicit_policy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, action: str
) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("provider is replaced by workflow spy")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    pending = await service.run(
        ReportRequest(
            instruction="生成设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="ask",
        )
    )
    seen: dict[str, str] = {}

    async def completed(self, state: dict) -> None:
        seen["policy"] = state["request"].missing_evidence_policy
        output = self.service.workspace / "Outputs/Reports/current-run.txt"
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text("verified report output", encoding="utf-8")
        state["output_artifacts"] = [
            OutputArtifact(kind="report", path=output.relative_to(self.service.workspace))
        ]

    monkeypatch.setattr(ReportWorkflowRunner, "run", completed)
    restarted = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    result = await restarted.resume(pending.decision_id, action, "确认继续")

    assert result.run_id == pending.run_id
    assert result.status == "completed"
    assert seen["policy"] == action
    choice = json.loads(
        (tmp_path / f"Work/runs/{pending.run_id}/evidence-choice.json").read_text(encoding="utf-8")
    )
    assert choice["selected_action"] == action


@pytest.mark.asyncio
async def test_service_never_marks_a_run_completed_without_verified_current_outputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=TemplateResolutionProvider(),
    )

    async def no_outputs(_self, _state: dict) -> None:
        return None

    monkeypatch.setattr(ReportWorkflowRunner, "run", no_outputs)

    result = await service.run(ReportRequest(instruction="生成报告"))

    assert result.status == "failed"
    assert result.output_paths == []
    assert result.error == "workflow finished without verified output artifacts"


@pytest.mark.asyncio
async def test_stop_marks_run_incomplete_without_success_artifact(tmp_path: Path) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("stop must not call provider")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )
    pending = await service.run(
        ReportRequest(
            instruction="生成设备模块",
            target_modules=["2.4"],
            missing_evidence_policy="ask",
        )
    )

    result = await service.resume(pending.decision_id, "stop")

    assert result.run_id == pending.run_id
    assert result.status == "stopped_incomplete"
    assert result.output_paths == []


@pytest.mark.asyncio
async def test_user_cancellation_is_saved_as_cancelled_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=TemplateResolutionProvider(),
    )

    async def cancelled(_self, _state: dict) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr(ReportWorkflowRunner, "run", cancelled)

    result = await service.run(ReportRequest(instruction="生成报告"))

    assert result.status == "cancelled"
    saved = json.loads((tmp_path / f"Work/runs/{result.run_id}.json").read_text(encoding="utf-8"))
    assert saved["status"] == "cancelled"
    assert saved["error"] == "interrupted by user"


@pytest.mark.asyncio
async def test_resume_tool_uses_persisted_decision_id(tmp_path: Path) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("stop must not call provider")

    run_id = "report-tool-resume"
    ReportingStore(tmp_path).write_json(
        f"Work/runs/{run_id}/request.json",
        ReportRequest(instruction="生成设备模块", target_modules=["2.4"]).model_dump(mode="json"),
    )
    decision = EvidenceDecisionStore(tmp_path).create(
        EvidenceDecisionRequest(
            decision_id="evidence-tool-resume",
            run_id=run_id,
            missing_items=["2.4.1.1 缺少证据"],
            affected_modules=["2.4"],
        )
    )
    tool = ResumeReportingWorkflowTool(
        tmp_path,
        MessageBus(),
        TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )

    payload = await tool(decision.decision_id, "stop")

    assert payload["run_id"] == run_id
    assert payload["status"] == "stopped_incomplete"
