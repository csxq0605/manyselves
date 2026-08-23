"""Capability-owned access to one already persisted Agent completion.

The durable attempt store remains the owner of current-attempt lookup and
result integrity checks.  This module only supplies the Capability adapter
that compares the existing task identity, decodes the existing AgentResult,
and exposes a completed payload to the neutral Agent bridge.
"""

from __future__ import annotations

from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    AgentResult,
)

from .state.parallel import TaskAttemptStore, TaskCorrelation


def same_recoverable_task(
    previous: TaskCorrelation,
    current: TaskCorrelation,
) -> bool:
    """Compare the existing durable recovery identity exactly as before.

    Attempt and lease fields are intentionally excluded because the existing
    Runner uses this comparison to find a reusable semantic task across a
    later physical attempt or lease.  No new identity or integrity policy is
    introduced here.
    """

    excluded = {"task_attempt_id", "lease_owner_id", "lease_epoch"}
    return previous.model_dump(exclude=excluded) == current.model_dump(
        exclude=excluded
    )


def load_completed_agent_result(
    workspace: Path,
    expected: TaskCorrelation,
) -> AgentResult | None:
    """Load a matching completed Agent result through the existing store.

    ``TaskAttemptStore`` remains the sole owner of persisted terminal/result
    validation, including its existing hash/CAS-related behavior.  The
    expected correlation must be supplied by the Capability binding; this
    helper does not synthesize lease, attempt, or digest values.
    """

    store = TaskAttemptStore(Path(workspace), expected.run_id)
    current = store.current(expected.task_id)
    if current is None or not same_recoverable_task(current, expected):
        return None
    recovered = store.load_verified_result(current)
    if recovered is None:
        return None
    terminal, payload = recovered
    result = AgentResult.model_validate(payload)
    if terminal.status != result.status.value:
        raise RuntimeError("persisted task result status does not match its terminal")
    if (
        result.task_id != expected.task_id
        or result.run_id != expected.run_id
        or result.agent_id != expected.agent_id
        or result.session_id != expected.session_id
    ):
        raise RuntimeError("persisted task result does not match its Agent identity")
    if terminal.status != "completed":
        return None
    return result


__all__ = ["load_completed_agent_result", "same_recoverable_task"]
