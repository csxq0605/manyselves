"""Characterize the existing durable Reporting continuation progress semantics."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from manyselves.capabilities.distribution_reporting.runtime.continuation_progress import (
    ReportingContinuationProgressObserver,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    TaskEnvelope,
)
from manyselves.kernel.recovery import RecoveryEventKind


@pytest.mark.asyncio
async def test_progress_baseline_survives_observer_recreation_and_detects_durable_write(
    tmp_path,
) -> None:
    """Same events are stalled after restore; a new durable artifact is progress."""

    envelope = TaskEnvelope(
        task_id="module-2.1",
        task_attempt_id="attempt-1",
        run_id="run-progress",
        agent_id="module-2.1-specialist",
        objective="write module",
        allowed_outputs=["module_submission"],
        revision=0,
    )
    loop = SimpleNamespace(
        _conversation_history=[
            SimpleNamespace(
                role="assistant",
                content="working",
                is_tool_result=False,
                tool_calls=[],
            )
        ],
        tools={},
    )

    first = ReportingContinuationProgressObserver(tmp_path, envelope)
    assert await first.observe(
        loop,
        RecoveryEventKind.TOOL_SLICE_BOUNDARY,
        {"task_id": envelope.task_id},
    ) == "progressed"

    restored = ReportingContinuationProgressObserver(tmp_path, envelope)
    assert await restored.observe(
        loop,
        RecoveryEventKind.TOOL_SLICE_BOUNDARY,
        {"task_id": envelope.task_id},
    ) == "no_progress"

    draft = (
        tmp_path
        / "Work/runs/run-progress/drafts/module-2.1/r0/2.1.1.md"
    )
    draft.parent.mkdir(parents=True)
    draft.write_text("new durable work", encoding="utf-8")

    assert await restored.observe(
        loop,
        RecoveryEventKind.TOOL_SLICE_BOUNDARY,
        {"task_id": envelope.task_id},
    ) == "progressed"
    assert first.state_ref == (
        "Work/runs/run-progress/continuations/module-2.1/attempt-1.json"
    )
