from __future__ import annotations

import hashlib
import json
import multiprocessing
from pathlib import Path

import pytest

from manyselves.core.reporting.parallel_runtime import (
    ArtifactRef,
    CrossOwnerCompletion,
    IdentityLeaseManager,
    LaneCompletion,
    ProjectWriteLeaseManager,
    TaskAttemptStore,
    TaskCorrelation,
    WorkflowReducer,
    bind_project_write_lease,
    reset_project_write_lease,
)
from manyselves.core.reporting.store import ReportingStore


def _hold_identity_lease(
    workspace: str,
    ready,
    release,
    result_queue,
) -> None:
    manager = IdentityLeaseManager(Path(workspace), "run-process")
    handle = manager.acquire("workflow-process", "module-2.1-specialist")
    result_queue.put(handle.lease.lease_epoch)
    ready.set()
    release.wait(10)
    handle.release()


def _hold_project_write_lease(workspace: str, ready, release) -> None:
    handle = ProjectWriteLeaseManager(Path(workspace)).acquire(
        "run-first",
        "full_report",
    )
    ready.set()
    release.wait(10)
    handle.release()


def test_identity_lease_is_process_exclusive_and_epoch_is_monotonic(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    result_queue = context.Queue()
    process = context.Process(
        target=_hold_identity_lease,
        args=(str(tmp_path), ready, release, result_queue),
    )
    process.start()
    assert ready.wait(10)
    assert result_queue.get(timeout=2) == 1

    manager = IdentityLeaseManager(tmp_path, "run-process")
    with pytest.raises(RuntimeError, match="already active"):
        manager.acquire("workflow-process", "module-2.1-specialist")

    release.set()
    process.join(10)
    assert process.exitcode == 0

    successor = manager.acquire("workflow-process", "module-2.1-specialist")
    try:
        assert successor.lease.lease_epoch == 2
        successor.validate()
    finally:
        successor.release()


def test_project_write_lease_serializes_distinct_runs_across_processes(
    tmp_path: Path,
) -> None:
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_project_write_lease,
        args=(str(tmp_path), ready, release),
    )
    process.start()
    assert ready.wait(10)

    manager = ProjectWriteLeaseManager(tmp_path)
    with pytest.raises(RuntimeError, match="active write operation"):
        manager.acquire("run-second", "module_report")

    release.set()
    process.join(10)
    assert process.exitcode == 0
    successor = manager.acquire("run-second", "module_report")
    try:
        assert successor.lease.lease_epoch == 2
        assert successor.lease.run_id == "run-second"
    finally:
        successor.release()


def test_reporting_store_rejects_old_project_fencing_epoch(tmp_path: Path) -> None:
    manager = ProjectWriteLeaseManager(tmp_path)
    stale = manager.acquire("run-stale", "full_report")
    token = bind_project_write_lease(tmp_path, stale.lease)
    try:
        ReportingStore(tmp_path).write_json(
            "Work/runs/run-stale/checkpoint.json",
            {"epoch": stale.lease.lease_epoch},
        )
        stale.release()
        current = manager.acquire("run-current", "module_report")
        try:
            with pytest.raises(RuntimeError, match="stale project write lease"):
                ReportingStore(tmp_path).write_json(
                    "Work/runs/run-stale/late-write.json",
                    {"late": True},
                )
            assert not (
                tmp_path / "Work/runs/run-stale/late-write.json"
            ).exists()
        finally:
            current.release()
    finally:
        reset_project_write_lease(token)


def _correlation(
    owner: str,
    epoch: int,
    attempt: str,
) -> TaskCorrelation:
    return TaskCorrelation(
        workflow_id="workflow-attempt",
        run_id="run-attempt",
        task_id="module-2.1",
        task_attempt_id=attempt,
        agent_id="module-2.1-specialist",
        identity_key="module-2.1-specialist",
        session_id="session-module-2.1",
        lease_owner_id=owner,
        lease_epoch=epoch,
    )


def test_late_attempt_is_append_only_but_cannot_replace_current_result(
    tmp_path: Path,
) -> None:
    manager = IdentityLeaseManager(tmp_path, "run-attempt")
    handle = manager.acquire("workflow-attempt", "module-2.1-specialist")
    store = TaskAttemptStore(tmp_path, "run-attempt")
    first = _correlation(handle.lease.owner_id, handle.lease.lease_epoch, "attempt-first")
    second = _correlation(handle.lease.owner_id, handle.lease.lease_epoch, "attempt-second")
    try:
        store.activate(first)
        store.activate(second)
        late = store.persist_result(
            first,
            {"status": "completed", "value": "late"},
            status="completed",
        )
        current = store.persist_result(
            second,
            {"status": "completed", "value": "current"},
            status="completed",
        )
    finally:
        handle.release()

    assert late.promoted_to_canonical is False
    assert current.promoted_to_canonical is True
    canonical = json.loads(
        (tmp_path / "Work/runs/run-attempt/results/module-2.1.json").read_text(
            encoding="utf-8"
        )
    )
    assert canonical["value"] == "current"
    assert (tmp_path / late.result_ref).is_file()
    assert (tmp_path / current.result_ref).is_file()


def test_persisted_attempt_hash_tampering_is_rejected(tmp_path: Path) -> None:
    manager = IdentityLeaseManager(tmp_path, "run-attempt")
    handle = manager.acquire("workflow-attempt", "module-2.1-specialist")
    store = TaskAttemptStore(tmp_path, "run-attempt")
    correlation = _correlation(
        handle.lease.owner_id,
        handle.lease.lease_epoch,
        "attempt-tamper",
    )
    try:
        store.activate(correlation)
        terminal = store.persist_result(
            correlation,
            {"status": "completed", "value": "verified"},
            status="completed",
        )
    finally:
        handle.release()
    (tmp_path / terminal.result_ref).write_text(
        '{"status":"completed","value":"tampered"}\n',
        encoding="utf-8",
    )

    with pytest.raises(RuntimeError, match="identity or hash mismatch"):
        store.load_verified_result(correlation)


def _completion(tmp_path: Path, module_id: str) -> tuple[str, LaneCompletion]:
    subject_bytes = f"subject-{module_id}".encode()
    review_bytes = f"review-{module_id}".encode()
    import hashlib

    subject = ArtifactRef(
        ref=f"Work/runs/run-barrier/modules/{module_id}.json",
        sha256=hashlib.sha256(subject_bytes).hexdigest(),
        size=len(subject_bytes),
        media_type="application/json",
    )
    review = ArtifactRef(
        ref=f"Work/runs/run-barrier/reviews/{module_id}.json",
        sha256=hashlib.sha256(review_bytes).hexdigest(),
        size=len(review_bytes),
        media_type="application/json",
    )
    completion = LaneCompletion(
        lane_id=f"module-{module_id}",
        run_id="run-barrier",
        module_id=module_id,
        semantic_key="a" * 64,
        subject=subject,
        review_completion=review,
        author_task_attempt_id=f"attempt-{module_id}",
        reviewer_session_id=f"reviewer-{module_id}",
        lease_epoch=1,
    )
    return f"Work/runs/run-barrier/lanes/{module_id}.json", completion


def test_module_barrier_is_deterministic_across_completion_order(
    tmp_path: Path,
) -> None:
    reducer = WorkflowReducer(tmp_path, "run-barrier")
    targets = ["2.1", "2.2", "2.3"]
    completions = [_completion(tmp_path, module_id) for module_id in targets]

    first = reducer.write_module_barrier(targets, list(reversed(completions)))
    second = reducer.write_module_barrier(list(reversed(targets)), completions)

    assert first == second
    assert first.target_modules == targets
    assert list(first.completion_refs) == targets


def test_module_barrier_rejects_missing_or_duplicate_completion(tmp_path: Path) -> None:
    reducer = WorkflowReducer(tmp_path, "run-barrier")
    completion = _completion(tmp_path, "2.1")
    with pytest.raises(ValueError, match="exact unique target"):
        reducer.write_module_barrier(["2.1", "2.2"], [completion])
    with pytest.raises(ValueError, match="exact unique target"):
        reducer.write_module_barrier(["2.1"], [completion, completion])


def _cross_owner_completion(module_id: str) -> tuple[str, CrossOwnerCompletion]:
    subject_bytes = f"cross-subject-{module_id}".encode()
    review_bytes = f"cross-review-{module_id}".encode()
    validation_bytes = f"cross-validation-{module_id}".encode()
    completion = CrossOwnerCompletion(
        lane_id=f"cross-r1-module-{module_id}",
        run_id="run-barrier",
        review_round=1,
        module_id=module_id,
        semantic_key=hashlib.sha256(f"semantic-{module_id}".encode()).hexdigest(),
        subject=ArtifactRef(
            ref=f"Work/runs/run-barrier/modules/{module_id}-r1.json",
            sha256=hashlib.sha256(subject_bytes).hexdigest(),
            size=len(subject_bytes),
            media_type="application/json",
        ),
        local_review_completion=ArtifactRef(
            ref=f"Work/runs/run-barrier/reviews/{module_id}.json",
            sha256=hashlib.sha256(review_bytes).hexdigest(),
            size=len(review_bytes),
            media_type="application/json",
        ),
        machine_validation=ArtifactRef(
            ref=f"Work/runs/run-barrier/validation/{module_id}.json",
            sha256=hashlib.sha256(validation_bytes).hexdigest(),
            size=len(validation_bytes),
            media_type="application/json",
        ),
        author_task_attempt_id=f"attempt-{module_id}",
        reviewer_session_id=f"reviewer-{module_id}",
        lease_epoch=1,
    )
    return (
        f"Work/runs/run-barrier/lanes/cross-r1/{module_id}.json",
        completion,
    )


def test_cross_owner_barrier_is_exact_and_deterministic(tmp_path: Path) -> None:
    reducer = WorkflowReducer(tmp_path, "run-barrier")
    completions = [
        _cross_owner_completion(module_id) for module_id in ("2.1", "2.3")
    ]

    first = reducer.write_cross_owner_barrier(
        1,
        ["2.3", "2.1"],
        completions,
    )
    second = reducer.write_cross_owner_barrier(
        1,
        ["2.1", "2.3"],
        list(reversed(completions)),
    )

    assert first == second
    assert first.target_modules == ["2.1", "2.3"]
    with pytest.raises(ValueError, match="exact unique target"):
        reducer.write_cross_owner_barrier(1, ["2.1", "2.3"], completions[:1])
