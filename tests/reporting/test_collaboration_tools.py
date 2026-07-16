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
