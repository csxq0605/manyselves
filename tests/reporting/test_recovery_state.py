from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from manyselves.core.reporting.parallel_runtime import (
    AggregateState,
    AllReadySupervisor,
    LaneAttemptRecord,
    LaneTaskSpec,
    RecoveryPlan,
    RecoveryStateStore,
)


def _write_result(root: Path, ref: str, **payload: object) -> None:
    path = root / ref
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_load_completed_lanes_uses_business_state_and_file_validation(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-state")
    ref = "Work/runs/run-state/results/lane-a.json"
    _write_result(
        tmp_path,
        ref,
        run_id="run-state",
        stage="author",
        lane_id="lane-a",
        revision=3,
        status="completed",
    )
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-state",
            stage="author",
            lane_id="lane-a",
            attempt=1,
            revision=3,
            status="completed",
            result_ref=ref,
            # Deliberately not a digest; hashes are not active identity.
            result_sha256="not-a-hash",
        )
    )
    assert list(store.load_completed_lanes("author")) == ["lane-a"]
    (tmp_path / ref).unlink()
    assert store.load_completed_lanes("author") == {}


def test_accepted_unknown_without_result_gets_explicit_new_attempt(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-state")
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-state",
            stage="author",
            lane_id="lane-a",
            attempt=1,
            revision=2,
            status="accepted_or_unknown",
            disposition="accepted_or_unknown",
        )
    )
    plan = store.retry_failed_lanes("author", ["lane-a"])
    assert isinstance(plan, RecoveryPlan)
    assert plan.lane_ids == ["lane-a"]
    attempt = store.retry_lane_attempt("author", "lane-a", revision=2)
    assert attempt.attempt == 2


def test_retry_lanes_and_aggregate_and_rollback_are_explicit(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-state")
    ref1 = "Work/runs/run-state/results/aggregate-r1.json"
    ref2 = "Work/runs/run-state/results/aggregate-r2.json"
    _write_result(tmp_path, ref1, status="completed")
    _write_result(tmp_path, ref2, status="completed")
    result = store.retry_lanes(
        "author",
        ["lane-a"],
        lambda lane, attempt: {"status": "completed", "result_ref": ref1},
        revision=1,
    )
    assert result["lane-a"]["result_ref"] == ref1
    first = store.record_aggregate(
        AggregateState(
            run_id="run-state",
            stage="author",
            revision=1,
            status="completed",
            result_ref=ref1,
        )
    )
    store.record_aggregate(
        AggregateState(
            run_id="run-state",
            stage="author",
            revision=2,
            status="completed",
            result_ref=ref2,
        )
    )
    rolled = store.rollback_to_aggregate("author")
    assert rolled.result_ref == first.result_ref


def test_all_ready_reconciles_only_existing_completed_result(tmp_path: Path) -> None:
    ref = "Work/runs/run-ready/results/lane-a.json"
    _write_result(tmp_path, ref, run_id="run-ready", stage="lane", lane_id="lane-a", revision=1)
    store = RecoveryStateStore(tmp_path, "run-ready")
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-ready",
            stage="lane",
            lane_id="lane-a",
            task_id="lane-a",
            attempt=1,
            revision=1,
            status="accepted_or_unknown",
            disposition="accepted_or_unknown",
            result_ref=ref,
        )
    )
    calls: list[str] = []

    async def run_lane(spec: LaneTaskSpec):
        calls.append(spec.task_id or "")
        raise AssertionError("reconciled completed result must not redispatch")

    result = asyncio.run(
        AllReadySupervisor(tmp_path, "run-ready", recovery_store=store).run(
            [LaneTaskSpec(run_id="run-ready", stage="lane", task_id="lane-a", revision=1)],
            run_lane,
        )
    )
    assert calls == []
    assert result.recovered == ("lane-a",)


def test_fixed_attempt_one_cannot_overwrite_terminal_lane_state(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-monotonic")
    first_ref = "Work/runs/run-monotonic/results/lane-a-r1.json"
    _write_result(tmp_path, first_ref, run_id="run-monotonic", stage="author", lane_id="lane-a", status="completed")
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-monotonic",
            stage="author",
            lane_id="lane-a",
            attempt=1,
            revision=0,
            status="completed",
            result_ref=first_ref,
        )
    )
    with pytest.raises(ValueError, match="larger attempt"):
        store.record_lane_attempt(
            LaneAttemptRecord(
                run_id="run-monotonic",
                stage="author",
                lane_id="lane-a",
                attempt=1,
                revision=0,
                status="failed",
                error="late failure",
            )
        )
    assert store.load_lane_state("author", "lane-a").result_ref == first_ref


def test_aggregate_revision_cannot_regress_or_conflict(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-aggregate-monotonic")
    ref = "Work/runs/run-aggregate-monotonic/results/aggregate.json"
    _write_result(tmp_path, ref, run_id="run-aggregate-monotonic", stage="author", status="completed")
    store.record_aggregate(
        AggregateState(
            run_id="run-aggregate-monotonic",
            stage="author",
            revision=2,
            status="completed",
            result_ref=ref,
        )
    )
    with pytest.raises(ValueError, match="regressed"):
        store.record_aggregate(
            AggregateState(
                run_id="run-aggregate-monotonic",
                stage="author",
                revision=1,
                status="completed",
                result_ref=ref,
            )
        )
    with pytest.raises(ValueError, match="conflicts"):
        store.record_aggregate(
            AggregateState(
                run_id="run-aggregate-monotonic",
                stage="author",
                revision=2,
                status="failed",
                result_ref=ref,
            )
        )


def test_successful_aggregate_requires_all_declared_completed_lanes(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-barrier-state")
    aggregate_ref = "Work/runs/run-barrier-state/results/aggregate.json"
    lane_ref = "Work/runs/run-barrier-state/results/lane-a.json"
    _write_result(tmp_path, aggregate_ref, run_id="run-barrier-state", stage="cross", status="completed")
    with pytest.raises(ValueError, match="readable completed lane"):
        store.record_aggregate(
            AggregateState(
                run_id="run-barrier-state",
                stage="cross",
                lane_ids=["lane-a"],
                revision=1,
                status="completed",
                result_ref=aggregate_ref,
            )
        )
    _write_result(tmp_path, lane_ref, run_id="run-barrier-state", stage="cross", lane_id="lane-a", status="completed")
    store.record_lane_completion(
        {
            "lane_id": "lane-a",
            "run_id": "run-barrier-state",
            "stage": "cross",
            "attempt": 1,
            "revision": 1,
            "status": "completed",
            "result_ref": lane_ref,
        }
    )
    aggregate = store.record_aggregate(
        AggregateState(
            run_id="run-barrier-state",
            stage="cross",
            lane_ids=["lane-a"],
            revision=1,
            status="completed",
            result_ref=aggregate_ref,
        )
    )
    assert aggregate.lane_ids == ["lane-a"]


def test_failed_lane_retry_then_reaggregate_is_lane_local(tmp_path: Path) -> None:
    store = RecoveryStateStore(tmp_path, "run-rerun")
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-rerun",
            stage="cross",
            lane_id="lane-a",
            attempt=1,
            status="failed",
            error="provider timeout",
        )
    )
    store.record_lane_attempt(
        LaneAttemptRecord(
            run_id="run-rerun",
            stage="cross",
            lane_id="lane-b",
            attempt=1,
            status="completed",
            result_ref="Work/runs/run-rerun/results/lane-b.json",
        )
    )
    _write_result(tmp_path, "Work/runs/run-rerun/results/lane-b.json", status="completed")
    plan = store.retry_failed_lanes("cross", ["lane-a", "lane-b"])
    assert plan.lane_ids == ["lane-a"]
    lane_ref = "Work/runs/run-rerun/results/lane-a-r2.json"
    aggregate_ref = "Work/runs/run-rerun/results/cross-r2.json"
    _write_result(tmp_path, lane_ref, run_id="run-rerun", stage="cross", lane_id="lane-a", status="completed")
    _write_result(tmp_path, aggregate_ref, run_id="run-rerun", stage="cross", status="completed")
    result = store.retry_lanes(
        "cross",
        plan.lane_ids,
        lambda lane, attempt: {"status": "completed", "result_ref": lane_ref},
        revision=1,
    )
    assert result["lane-a"]["result_ref"] == lane_ref
    assert store.load_lane_state("cross", "lane-a").attempt == 2
    aggregate = store.record_aggregate(
        AggregateState(
            run_id="run-rerun",
            stage="cross",
            lane_ids=["lane-a", "lane-b"],
            revision=2,
            status="completed",
            result_ref=aggregate_ref,
        )
    )
    assert set(aggregate.lane_ids) == {"lane-a", "lane-b"}


def test_repeated_aggregate_failure_restarts_from_previous_boundary(tmp_path: Path) -> None:
    run_id = "run-stage-rollback"
    store = RecoveryStateStore(tmp_path, run_id)
    module_ref = f"Work/runs/{run_id}/module-boundary.json"
    _write_result(
        tmp_path,
        module_ref,
        run_id=run_id,
        stage="module",
        status="completed",
        revision=0,
    )
    store.record_aggregate(
        AggregateState(
            run_id=run_id,
            stage="module",
            status="completed",
            result_ref=module_ref,
        )
    )
    for lane_id in ("2.1", "2.2"):
        result_ref = f"Work/runs/{run_id}/cross-{lane_id}.json"
        _write_result(
            tmp_path,
            result_ref,
            run_id=run_id,
            stage="cross",
            lane_id=lane_id,
            status="completed",
            revision=0,
        )
        store.record_lane_attempt(
            LaneAttemptRecord(
                run_id=run_id,
                stage="cross",
                lane_id=lane_id,
                attempt=1,
                status="completed",
                result_ref=result_ref,
            )
        )

    first = store.recover_aggregate_failure(
        "cross", ["2.1", "2.2"], previous_stage="module", reason="reducer failed"
    )
    second = store.recover_aggregate_failure(
        "cross", ["2.1", "2.2"], previous_stage="module", reason="reducer failed again"
    )

    assert first.action == "retry_aggregate"
    assert second.action == "rollback_aggregate"
    assert second.status == "applied"
    assert second.aggregate_id == "module"
    assert second.result_ref == module_ref
    assert store.load_completed_lanes("cross", ["2.1", "2.2"]) == {}
    assert all(
        store.load_lane_state("cross", lane_id).status == "invalidated"
        for lane_id in ("2.1", "2.2")
    )
