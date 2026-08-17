from __future__ import annotations

import hashlib
import multiprocessing
from pathlib import Path

import pytest

from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.core.reporting.distributed_runtime import (
    DistributedTask,
    LocalEventStore,
    LocalTaskQueue,
    RunProjection,
    WorkspaceMaterializer,
)
from manyselves.core.reporting.parallel_runtime import ArtifactRef
from manyselves.core.reporting.parallel_runtime import ProjectWriteLease
from manyselves.core.reporting.parallel_runtime import ProjectWriteLeaseManager


def _artifact(path: Path, workspace: Path, *, media_type: str) -> ArtifactRef:
    content = path.read_bytes()
    return ArtifactRef(
        ref=path.relative_to(workspace).as_posix(),
        sha256=hashlib.sha256(content).hexdigest(),
        size=len(content),
        media_type=media_type,
    )


def _run_distributed_worker(workspace: str, result_queue) -> None:
    root = Path(workspace)
    queue = LocalTaskQueue(root, "run-distributed")
    claim = queue.claim_next("worker-one")
    if claim is None:
        result_queue.put({"error": "no task"})
        return
    materializer = WorkspaceMaterializer(root, "run-distributed")
    sandbox = materializer.materialize(claim.task.input_artifacts)
    try:
        input_path = sandbox / "inputs" / claim.task.input_artifacts[0].ref
        output_path = sandbox / "outputs" / "result.txt"
        output_path.write_text(
            input_path.read_text(encoding="utf-8").upper(),
            encoding="utf-8",
        )
        output_ref = materializer.publish_output(
            sandbox,
            output_path,
            "Work/runs/run-distributed/worker-outputs/result.txt",
            media_type="text/plain",
        )
        completion = queue.complete(
            claim,
            status="completed",
            output_artifacts=[output_ref],
        )
        result_queue.put(completion.model_dump(mode="json"))
    finally:
        materializer.cleanup(sandbox)


def _append_worker_events(workspace: str, worker_id: str, count: int) -> None:
    store = LocalEventStore(Path(workspace), "run-events")
    for index in range(count):
        store.append(
            "TaskDispatched",
            stage_id="parallel-events",
            task_id=f"{worker_id}-{index}",
            task_attempt_id=f"attempt-{worker_id}-{index}",
        )


def _task(
    input_ref: ArtifactRef,
    project_lease: ProjectWriteLease,
) -> DistributedTask:
    return DistributedTask(
        workflow_id="workflow-distributed",
        run_id="run-distributed",
        stage_id="module-work",
        task_id="module-2.1",
        task_attempt_id="attempt-one",
        identity_key="module-2.1-specialist",
        project_operation=project_lease.operation,
        project_lease_owner_id=project_lease.owner_id,
        project_lease_epoch=project_lease.lease_epoch,
        input_artifacts=[input_ref],
        payload={"module_id": "2.1"},
        created_at_ns=1,
    )


def test_spawned_worker_reconstructs_task_materializes_and_publishes(
    tmp_path: Path,
) -> None:
    input_path = tmp_path / "Work/runs/run-distributed/inputs/source.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("independent process", encoding="utf-8")
    project_handle = ProjectWriteLeaseManager(tmp_path).acquire(
        "run-distributed",
        "full_report",
    )
    try:
        task = _task(
            _artifact(input_path, tmp_path, media_type="text/plain"),
            project_handle.lease,
        )
        LocalEventStore(tmp_path, task.run_id).append(
            "StageReady",
            stage_id=task.stage_id,
            correlation_id=task.workflow_id,
        )
        LocalTaskQueue(tmp_path, task.run_id).enqueue(task)

        context = multiprocessing.get_context("spawn")
        result_queue = context.Queue()
        process = context.Process(
            target=_run_distributed_worker,
            args=(str(tmp_path), result_queue),
        )
        process.start()
        process.join(15)

        assert process.exitcode == 0
        result = result_queue.get(timeout=2)
        assert result["error"] is None
        assert result["status"] == "completed"
        assert result["project_lease_epoch"] == project_handle.lease.lease_epoch
        output_ref = ArtifactRef.model_validate(result["output_artifacts"][0])
        output_path = tmp_path / output_ref.ref
        assert output_path.read_text(encoding="utf-8") == "INDEPENDENT PROCESS"
        assert hashlib.sha256(output_path.read_bytes()).hexdigest() == output_ref.sha256

        reconstructed_queue = LocalTaskQueue(tmp_path, task.run_id)
        assert reconstructed_queue.claim_next("worker-two") is None
        assert reconstructed_queue.completions()[0].task_attempt_id == "attempt-one"
        events = LocalEventStore(tmp_path, task.run_id).read()
        assert [event.sequence for event in events] == list(range(1, len(events) + 1))
        assert [event.event_type for event in events] == [
            "StageReady",
            "TaskDispatched",
            "AttemptStarted",
            "TypedResultAccepted",
        ]
        projection = RunProjection.rebuild(task.run_id, events)
        assert projection.event_cursor == 4
        assert projection.stages["module-work"]["status"] == "ready"
        assert projection.tasks["module-2.1"]["status"] == "completed"
        assert projection.artifacts[output_ref.ref] == output_ref
    finally:
        project_handle.release()


def test_stale_worker_epoch_cannot_complete_after_reclaim(tmp_path: Path) -> None:
    input_path = tmp_path / "Work/runs/run-distributed/inputs/source.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("input", encoding="utf-8")
    project_handle = ProjectWriteLeaseManager(tmp_path).acquire(
        "run-distributed",
        "full_report",
    )
    queue = LocalTaskQueue(tmp_path, "run-distributed")
    queue.enqueue(
        _task(
            _artifact(input_path, tmp_path, media_type="text/plain"),
            project_handle.lease,
        )
    )

    try:
        stale = queue.claim_next("worker-stale")
        assert stale is not None
        stale.release()
        current = queue.claim_next("worker-current")
        assert current is not None
        assert current.lease_epoch == stale.lease_epoch + 1

        with pytest.raises(RuntimeError, match="stale identity lease"):
            queue.complete(stale, status="completed")
        accepted = queue.complete(current, status="completed")
        assert accepted.worker_id == "worker-current"
        assert accepted.lease_epoch == current.lease_epoch
    finally:
        project_handle.release()


def test_reassigned_project_epoch_rejects_old_distributed_worker(tmp_path: Path) -> None:
    input_path = tmp_path / "Work/runs/run-distributed/inputs/source.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("input", encoding="utf-8")
    manager = ProjectWriteLeaseManager(tmp_path)
    first = manager.acquire("run-distributed", "full_report")
    queue = LocalTaskQueue(tmp_path, "run-distributed")
    queue.enqueue(
        _task(
            _artifact(input_path, tmp_path, media_type="text/plain"),
            first.lease,
        )
    )
    stale_claim = queue.claim_next("worker-old")
    assert stale_claim is not None
    first.release()
    successor = manager.acquire("run-successor", "module_report")
    try:
        with pytest.raises(RuntimeError, match="stale project write lease"):
            queue.complete(stale_claim, status="completed")
        assert queue.completions() == []
    finally:
        stale_claim.release()
        successor.release()


def test_materializer_rejects_hash_mismatch_and_output_escape(tmp_path: Path) -> None:
    input_path = tmp_path / "Work/runs/run-distributed/inputs/source.txt"
    input_path.parent.mkdir(parents=True)
    input_path.write_text("original", encoding="utf-8")
    ref = _artifact(input_path, tmp_path, media_type="text/plain")
    input_path.write_text("tampered", encoding="utf-8")
    materializer = WorkspaceMaterializer(tmp_path, "run-distributed")

    with pytest.raises(ValueError, match="hash mismatch"):
        materializer.materialize([ref])

    valid_ref = _artifact(input_path, tmp_path, media_type="text/plain")
    sandbox = materializer.materialize([valid_ref])
    try:
        output = sandbox / "outputs/result.txt"
        output.write_text("result", encoding="utf-8")
        with pytest.raises(ValueError, match="beneath its run"):
            materializer.publish_output(
                sandbox,
                output,
                "Work/runs/another-run/result.txt",
                media_type="text/plain",
            )
        with pytest.raises(ValueError, match="outside materialized root"):
            materializer.cleanup(tmp_path)
    finally:
        materializer.cleanup(sandbox)


def test_materializer_accepts_same_lineage_trusted_artifact_without_rehash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Work/runs/run-distributed/raw/source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("trusted input", encoding="utf-8")
    store = ContentAddressedStore(tmp_path)
    blob = store.ingest_file(source)
    handle = store.issue_trusted_handle(
        blob, lineage_id="run-distributed:input:source"
    )
    view = tmp_path / "Work/runs/run-distributed/inputs/source.txt"
    store.link_trusted_view(handle, view)
    ref = ArtifactRef(
        ref=view.relative_to(tmp_path).as_posix(),
        sha256=handle.sha256,
        size=handle.size,
        media_type="text/plain",
        trusted_handle_ref=handle.manifest_ref.as_posix(),
    )
    materializer = WorkspaceMaterializer(tmp_path, "run-distributed")

    sandbox = materializer.materialize([ref])
    try:
        materialized = sandbox / "inputs" / ref.ref
        assert materialized.read_text(encoding="utf-8") == "trusted input"
    finally:
        materializer.cleanup(sandbox)


def test_event_store_sequences_concurrent_process_appends(tmp_path: Path) -> None:
    context = multiprocessing.get_context("spawn")
    processes = [
        context.Process(
            target=_append_worker_events,
            args=(str(tmp_path), f"worker-{index}", 10),
        )
        for index in range(3)
    ]
    for process in processes:
        process.start()
    for process in processes:
        process.join(15)
        assert process.exitcode == 0

    events = LocalEventStore(tmp_path, "run-events").read()
    assert len(events) == 30
    assert [event.sequence for event in events] == list(range(1, 31))
    assert len({event.task_id for event in events}) == 30
