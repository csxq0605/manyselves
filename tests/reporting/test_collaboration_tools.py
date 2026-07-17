import asyncio
import json
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.taxonomy import REPORT_TAXONOMY
from manyselves.core.tools.reporting_collaboration_tools import (
    PeerMessageRouter,
    QueryPeerTool,
    ReplyPeerTool,
    ReportBlockedTool,
    ReportGapTool,
    SubmissionValidationError,
    SubmitResultTool,
)
from manyselves.interfaces.types import (
    AgentResultMessage,
    BlockedNoticeMessage,
    ProgressNoteMessage,
    UserMessage,
)

MODULE_PAYLOAD = {
    "kind": "module_submission",
    "module_id": "2.4",
    "markdown": "设备分析正文",
    "submodule_narratives": {
        submodule_id: f"{submodule_id} 分析" for submodule_id in REPORT_TAXONOMY["2.4"].submodules
    },
    "claims": [],
    "source_ids": [],
    "unresolved_questions": [],
    "revision": 0,
}


@pytest.mark.asyncio
async def test_submit_result_rejects_disallowed_output_before_persisting(tmp_path: Path):
    tool = SubmitResultTool(
        agent_id="chief-editor",
        session_id="session-chief",
        run_id="run-output",
        task_id="chief-task",
        store=ReportingStore(tmp_path),
        bus=MessageBus(),
        allowed_outputs=["edited_report_submission"],
    )
    with pytest.raises(SubmissionValidationError, match="allowed output"):
        await tool(payload=MODULE_PAYLOAD)
    assert not (tmp_path / "Work/runs/run-output/results/chief-task.json").exists()


@pytest.mark.asyncio
async def test_submit_result_persists_typed_payload_before_publish(tmp_path: Path):
    bus = MessageBus()
    tool = SubmitResultTool(
        agent_id="module-2.4-specialist",
        session_id="session-24",
        run_id="run-1",
        task_id="task-24",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )

    result = await tool(payload=MODULE_PAYLOAD)
    message = await bus._queue.get()

    assert isinstance(message, AgentResultMessage)
    persisted = json.loads((tmp_path / message.result_path).read_text(encoding="utf-8"))
    assert persisted["payload"]["markdown"] == "设备分析正文"
    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_submit_result_supports_full_five_module_domain_without_research(
    tmp_path: Path,
):
    bus = MessageBus()
    payload = {
        **MODULE_PAYLOAD,
        "module_id": "2.1",
        "markdown": "系统架构分析",
        "submodule_narratives": {
            submodule_id: f"{submodule_id} 分析"
            for submodule_id in REPORT_TAXONOMY["2.1"].submodules
        },
    }
    tool = SubmitResultTool(
        agent_id="module-2.1-specialist",
        session_id="session-21",
        run_id="run-1",
        task_id="task-21",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )

    result = await tool(payload=payload)

    assert result["status"] == "completed"


@pytest.mark.asyncio
async def test_submit_result_unwraps_provider_item_wrapped_lists(tmp_path: Path):
    """Accept the list encoding emitted by the configured OpenAI-compatible API."""
    bus = MessageBus()
    tool = SubmitResultTool(
        agent_id="report-planner",
        session_id="session-plan",
        run_id="run-1",
        task_id="report-plan",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )
    payload = {
        "kind": "plan_submission",
        "rationale": "为每个固定模块安排一名专家。",
        "module_tasks": {
            "item": [
                {
                    "task_id": "module-2.1",
                    "run_id": "run-1",
                    "agent_id": "module-2.1-specialist",
                    "objective": "完成 2.1 分析。",
                    "input_refs": {"item": ["Work/evidence.jsonl"]},
                    "constraints": {"item": []},
                    "allowed_outputs": {"item": ["module_submission"]},
                    "issue_refs": {"item": []},
                    "context_summary_refs": {"item": []},
                    "target_submodule_ids": {
                        "item": ["2.1.1", "2.1.2", "2.1.3", "2.1.4", "2.1.5"]
                    },
                }
            ]
        },
    }

    result = await tool(payload=payload)

    assert result["status"] == "completed"
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/results/report-plan.json").read_text(encoding="utf-8")
    )
    assert persisted["payload"]["module_tasks"][0]["input_refs"] == ["Work/evidence.jsonl"]


@pytest.mark.asyncio
async def test_submit_result_wraps_item_wrapped_scalar_as_a_single_list_value(
    tmp_path: Path,
):
    bus = MessageBus()
    tool = SubmitResultTool(
        agent_id="report-planner",
        session_id="session-plan",
        run_id="run-1",
        task_id="report-plan",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )
    payload = {
        "kind": "plan_submission",
        "rationale": "为每个固定模块安排一名专家。",
        "module_tasks": {
            "item": {
                "task_id": "module-2.1",
                "run_id": "run-1",
                "agent_id": "module-2.1-specialist",
                "objective": "完成 2.1 分析。",
                "input_refs": {"item": "Work/evidence.jsonl"},
                "constraints": {"item": "仅引用项目资料。"},
                "allowed_outputs": {"item": "module_submission"},
                "issue_refs": {"item": "E-0001"},
                "context_summary_refs": {"item": "Work/coverage.json"},
                "expected_plan_agent_ids": {"item": "module-2.2-specialist"},
                "target_submodule_ids": {"item": "2.1.1"},
            }
        },
    }

    result = await tool(payload=payload)

    assert result["status"] == "completed"
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/results/report-plan.json").read_text(encoding="utf-8")
    )
    task = persisted["payload"]["module_tasks"][0]
    assert task["allowed_outputs"] == ["module_submission"]
    assert task["context_summary_refs"] == ["Work/coverage.json"]
    assert task["expected_plan_agent_ids"] == ["module-2.2-specialist"]


@pytest.mark.asyncio
async def test_submit_result_normalizes_an_empty_string_in_a_list_field(tmp_path: Path):
    """Treat the provider's empty-list encoding as an empty list, not a validation error."""
    bus = MessageBus()
    tool = SubmitResultTool(
        agent_id="report-planner",
        session_id="session-plan",
        run_id="run-1",
        task_id="report-plan",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )
    payload = {
        "kind": "plan_submission",
        "rationale": "为每个固定模块安排一名专家。",
        "module_tasks": {
            "item": {
                "task_id": "module-2.1",
                "run_id": "run-1",
                "agent_id": "module-2.1-specialist",
                "objective": "完成 2.1 分析。",
                "allowed_outputs": {"item": "module_submission"},
                "issue_refs": "",
            }
        },
    }

    result = await tool(payload=payload)

    assert result["status"] == "completed"
    persisted = json.loads(
        (tmp_path / "Work/runs/run-1/results/report-plan.json").read_text(encoding="utf-8")
    )
    assert persisted["payload"]["module_tasks"][0]["issue_refs"] == []


@pytest.mark.asyncio
async def test_submit_result_rejects_a_plan_that_omits_required_specialists(
    tmp_path: Path,
):
    bus = MessageBus()
    tool = SubmitResultTool(
        agent_id="report-planner",
        session_id="session-plan",
        run_id="run-1",
        task_id="report-plan",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
        expected_plan_agent_ids=["module-2.1-specialist", "module-2.2-specialist"],
    )
    payload = {
        "kind": "plan_submission",
        "rationale": "仅安排了一项，因而应被拒绝。",
        "module_tasks": [
            {
                "task_id": "module-2.1",
                "run_id": "run-1",
                "agent_id": "module-2.1-specialist",
                "objective": "完成 2.1 分析。",
                "allowed_outputs": ["module_submission"],
            }
        ],
    }

    with pytest.raises(ValueError, match="missing=.*module-2.2-specialist"):
        await tool(payload=payload)

    assert not (tmp_path / "Work/runs/run-1/results/report-plan.json").exists()


@pytest.mark.asyncio
async def test_report_blocked_persists_and_publishes_typed_notice(tmp_path: Path):
    bus = MessageBus()
    tool = ReportBlockedTool(
        agent_id="module-2.4-specialist",
        session_id="session-24",
        run_id="run-1",
        task_id="task-24",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )

    result = await tool(reason="缺少断路器整定值", artifact_refs=["Work/evidence.jsonl"])
    messages = [await bus._queue.get(), await bus._queue.get()]

    assert result["status"] == "blocked"
    assert any(isinstance(message, BlockedNoticeMessage) for message in messages)
    assert any(isinstance(message, AgentResultMessage) for message in messages)


@pytest.mark.asyncio
async def test_report_gap_is_persisted_but_does_not_end_task(tmp_path: Path):
    bus = MessageBus()
    result = await ReportGapTool(
        agent_id="module-2.3-specialist",
        run_id="run-1",
        task_id="task-23",
        store=ReportingStore(tmp_path),
        bus=bus,
        workflow_id="wf-1",
    )(
        gap="缺少断路器整定值",
        impact="无法确认上下级选择性",
        requested_input="提供现行整定单",
    )
    message = await bus._queue.get()

    assert result["status"] == "reported"
    assert isinstance(message, ProgressNoteMessage)
    assert not isinstance(message, AgentResultMessage)
    assert (tmp_path / result["artifact_ref"]).exists()


@pytest.mark.asyncio
async def test_query_peer_routes_to_target_and_accepts_only_matching_reply():
    bus = MessageBus()
    PeerMessageRouter(bus)
    delivered: list[UserMessage] = []

    async def reply(message: UserMessage):
        delivered.append(message)
        await ReplyPeerTool(bus, "report-planner", workflow_id="wf-1")(
            task_id="task-24",
            query_id=message.message_id or "",
            target_agent="module-2.4-specialist",
            target_session_id="some-other-session",
            answer="这条回复不应被接收。",
            source_ids=[],
        )
        await ReplyPeerTool(bus, "report-planner", workflow_id="wf-1")(
            task_id="task-24",
            query_id=message.message_id or "",
            target_agent="module-2.4-specialist",
            target_session_id="session-24",
            answer="规划范围只包含 2.4。",
            source_ids=[],
        )

    bus.subscribe(UserMessage, reply)
    processor = asyncio.create_task(bus.process_queue())
    result = await QueryPeerTool(
        bus,
        task_id="task-24",
        agent_id="module-2.4-specialist",
        session_id="session-24",
        workflow_id="wf-1",
        timeout=1.0,
    )(
        target_agent="report-planner",
        question="是否需要扩展到 2.5？",
        artifact_refs=["Work/coverage.json"],
    )

    assert result["status"] == "replied"
    assert result["answer"] == "规划范围只包含 2.4。"
    assert delivered[0].agent_type == "report-planner"
    assert "Work/coverage.json" in delivered[0].content
    bus.shutdown()
    await processor
