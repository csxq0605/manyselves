from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.agent_result_payload import (
    load_agent_result_payload,
)
from manyselves.capabilities.distribution_reporting.runtime.collaboration_tools import (
    SubmitResultTool,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FinalChapterLaneFindingSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.interfaces.types import AgentResultMessage
from manyselves.runtime.loops.bus import MessageBus


@pytest.mark.asyncio
async def test_loads_submit_result_agent_wrapper_and_preserves_identity(
    tmp_path: Path,
) -> None:
    """Characterize the persisted wrapper emitted by the real SubmitResultTool."""

    run_id = "run-agent-result-payload"
    task_id = "final-chapter-1"
    agent_id = "final-auditor"
    session_id = "session-final-1"
    workflow_id = "distribution-reporting"
    bus = MessageBus()
    store = ReportingStore(tmp_path)
    tool = SubmitResultTool(
        agent_id,
        session_id,
        run_id,
        task_id,
        store,
        bus,
        workflow_id,
        allowed_outputs=["final_chapter_lane_finding_submission"],
    )
    terminal_messages: list[AgentResultMessage] = []

    async def capture(message: AgentResultMessage) -> None:
        terminal_messages.append(message)

    bus.subscribe(AgentResultMessage, capture)
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        outcome = await tool(
            kind="final_chapter_lane_finding_submission",
            run_id=run_id,
            chapter_id="1",
            checked_section_ids=["1.1", "1.2", "1.3"],
            findings=[],
            residual_risks=[],
        )
        await asyncio.sleep(0)
    finally:
        bus.shutdown()
        await bus_task

    assert outcome["status"] == "completed"
    assert len(terminal_messages) == 1
    terminal = terminal_messages[0]
    loaded = load_agent_result_payload(tmp_path, terminal.result_path)

    assert isinstance(loaded.payload, FinalChapterLaneFindingSubmission)
    assert loaded.payload.run_id == run_id
    assert loaded.identity.task_id == task_id
    assert loaded.identity.run_id == run_id
    assert loaded.identity.agent_id == agent_id
    assert loaded.identity.session_id == session_id
    assert loaded.identity.status == "completed"
