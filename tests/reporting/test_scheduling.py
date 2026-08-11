import asyncio

import pytest

from manyselves.core.reporting.scheduling import (
    AdaptiveTaskScheduler,
    SchedulingCandidate,
    TaskTimingHistory,
)


@pytest.mark.asyncio
async def test_scheduler_starts_longest_critical_path_first_with_stable_ties() -> None:
    scheduler = AdaptiveTaskScheduler(
        [
            SchedulingCandidate(
                task_id="2.1", owner_key="2.1", task_kind="module", ordinal=0,
                expected_duration_ms=20,
            ),
            SchedulingCandidate(
                task_id="2.2", owner_key="2.2", task_kind="module", ordinal=1,
                expected_duration_ms=80,
            ),
            SchedulingCandidate(
                task_id="2.3", owner_key="2.3", task_kind="module", ordinal=2,
                expected_duration_ms=80,
            ),
        ]
    )

    assert (await scheduler.next()).task_id == "2.2"
    assert (await scheduler.next()).task_id == "2.3"
    assert (await scheduler.next()).task_id == "2.1"
    assert await scheduler.next() is None
    assert [item.sequence for item in scheduler.decisions] == [1, 2, 3]


@pytest.mark.asyncio
async def test_scheduler_concurrent_workers_claim_each_task_once() -> None:
    scheduler = AdaptiveTaskScheduler(
        [
            SchedulingCandidate(
                task_id=str(index), owner_key=str(index), task_kind="module", ordinal=index
            )
            for index in range(20)
        ]
    )

    async def worker() -> list[str]:
        claimed: list[str] = []
        while candidate := await scheduler.next():
            claimed.append(candidate.task_id)
            await asyncio.sleep(0)
        return claimed

    claims = [task for group in await asyncio.gather(*(worker() for _ in range(5))) for task in group]
    assert len(claims) == 20
    assert len(set(claims)) == 20


def test_timing_history_uses_recent_weighted_successes_only(tmp_path) -> None:
    history = TaskTimingHistory(tmp_path)
    history.record(
        run_id="run-1", task_id="a", task_kind="module", owner_key="2.1",
        duration_ms=100, status="completed",
    )
    history.record(
        run_id="run-2", task_id="b", task_kind="module", owner_key="2.1",
        duration_ms=200, status="completed",
    )
    history.record(
        run_id="run-3", task_id="c", task_kind="module", owner_key="2.1",
        duration_ms=1, status="failed",
    )

    assert history.estimate_ms("module", "2.1") == 167
    assert history.estimate_ms("module", "2.2", default=321) == 321
