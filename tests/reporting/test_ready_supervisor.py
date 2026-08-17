from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest

from manyselves.core.reporting.parallel_runtime import (
    AllReadySupervisor,
    LaneAttemptRecord,
    LaneTaskSpec,
    TaskAttemptStore,
    WorkflowReducer,
)


def _spec(task_id: str, *, revision: int = 1, ordinal: int = 0) -> LaneTaskSpec:
    return LaneTaskSpec(
        task_id=task_id,
        semantic_key=hashlib.sha256(task_id.encode()).hexdigest(),
        revision=revision,
        priority=50,
        ordinal=ordinal,
        payload_hash=hashlib.sha256((task_id + "-payload").encode()).hexdigest(),
        run_id="run-ready",
    )


def test_all_ready_preclaims_every_lane_and_drains_ordinary_failure(
    tmp_path: Path,
) -> None:
    async def scenario():
        specs = [_spec(f"task-{index}", ordinal=index) for index in range(4)]
        started: list[str] = []
        active = 0
        max_active = 0

        async def run_lane(spec: LaneTaskSpec):
            nonlocal active, max_active
            started.append(spec.task_id or "")
            active += 1
            max_active = max(max_active, active)
            await asyncio.sleep(0.01)
            active -= 1
            if spec.task_id == "task-1":
                raise RuntimeError("ordinary lane failure")
            return {
                "result_ref": f"Work/runs/run-ready/{spec.task_id}.json",
                "result_sha256": "a" * 64,
            }

        return await AllReadySupervisor(tmp_path, "run-ready").run(specs, run_lane), started, max_active

    result, started, max_active = asyncio.run(scenario())
    assert started == ["task-0", "task-1", "task-2", "task-3"]
    assert max_active == 4
    assert set(result.terminals) == set(started)
    assert result.failures == {"task-1": "RuntimeError: ordinary lane failure"}
    assert result.barrier_candidate is None


def test_hard_stop_marks_claimed_lanes_deferred_without_running_provider(
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    async def run_lane(spec: LaneTaskSpec):
        calls.append(spec.task_id or "")
        return {"result_ref": "unused", "result_sha256": "b" * 64}

    result = asyncio.run(
        AllReadySupervisor(tmp_path, "run-ready").run(
            [_spec("task-a"), _spec("task-b", ordinal=1)],
            run_lane,
            hard_stop=True,
        )
    )
    assert calls == []
    assert result.deferred == ("task-a", "task-b")
    assert all(record.status == "deferred" for record in result.terminals.values())


def test_resume_retries_unknown_and_missing_completed_results(tmp_path: Path) -> None:
    store = TaskAttemptStore(tmp_path, "run-ready")
    verified = LaneAttemptRecord(
        task_id="task-verified",
        task_attempt_id="attempt-verified",
        status="completed",
        disposition="completed",
        result_ref="Work/runs/run-ready/verified.json",
        result_sha256="c" * 64,
    )
    unknown = LaneAttemptRecord(
        task_id="task-unknown",
        task_attempt_id="attempt-unknown",
        status="failed",
        disposition="failed",
        error="Provider response may have been accepted",
    )
    store.append(verified)
    store.append(unknown)
    calls: list[str] = []

    async def run_lane(spec: LaneTaskSpec):
        calls.append(spec.task_id or "")
        return {"result_ref": "should-not-run", "result_sha256": "d" * 64}

    result = asyncio.run(
        AllReadySupervisor(tmp_path, "run-ready").run(
            [_spec("task-verified"), _spec("task-unknown", ordinal=1)],
            run_lane,
        )
    )
    assert calls == ["task-verified", "task-unknown"]
    # Historical terminals remain audit evidence, while explicit same-run
    # resume creates fresh attempts for both unknown and corrupt completions.
    assert result.recovered == ()
    assert result.blocked == ()
    assert result.terminals["task-verified"].status == "completed"
    assert result.terminals["task-unknown"].status == "completed"
    assert result.barrier_candidate is None


def test_reducer_promotes_once_and_commits_exact_revision_barrier(tmp_path: Path) -> None:
    spec = _spec("task-a", revision=7)
    reducer = WorkflowReducer(tmp_path, "run-ready")
    terminal = {
        "semantic_key": spec.semantic_key,
        "revision": 7,
        "result_ref": "Work/runs/run-ready/task-a.json",
        "result_sha256": "e" * 64,
        "task_attempt_id": "attempt-a",
    }
    reducer.promote_terminal(spec, terminal, expected_revision=7)
    with pytest.raises(RuntimeError, match="older revision|different identity"):
        reducer.promote_terminal(
            spec,
            {
                **terminal,
                "revision": 6,
                "task_attempt_id": "late-old",
            },
        )
    barrier = reducer.commit_exact_barrier(
        [spec],
        {"task-a": terminal},
        expected_revision=7,
    )
    assert barrier.target_tasks == ["task-a"]
    assert barrier.target_revisions == {"task-a": 7}
    assert reducer.commit_exact_barrier([spec], {"task-a": terminal}, expected_revision=7) == barrier
