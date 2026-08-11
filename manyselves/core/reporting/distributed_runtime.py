"""Local durable adapters for process-independent reporting task distribution.

The adapter intentionally exchanges only typed task records and logical
``ArtifactRef`` values.  A future remote queue/object-store implementation can
replace it without changing module-lane contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from ..artifacts.content_store import ContentAddressedStore
from .parallel_runtime import (
    ArtifactRef,
    IdentityLeaseHandle,
    IdentityLeaseManager,
    ProjectWriteLease,
    ProjectWriteLeaseManager,
    atomic_write_json,
    bind_project_write_lease,
    exclusive_file_lock,
    reset_project_write_lease,
    validate_bound_project_write_lease,
    current_bound_project_write_lease,
)


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


RunEventType: TypeAlias = Literal[
    "RunQueued",
    "RunClaimed",
    "CancelRequested",
    "RunAmbiguous",
    "RunCancelled",
    "StageReady",
    "TaskDispatched",
    "AttemptStarted",
    "TypedResultAccepted",
    "TaskFailed",
    "StageCompleted",
    "RunWaitingUser",
    "ArtifactPublished",
    "RunCompleted",
    "RunFailed",
]


class RunEvent(_StrictModel):
    schema_version: str = "1"
    run_id: str
    sequence: int = Field(ge=1)
    event_type: RunEventType
    stage_id: str | None = None
    task_id: str | None = None
    task_attempt_id: str | None = None
    lease_epoch: int | None = Field(default=None, ge=1)
    artifact_refs: list[ArtifactRef] = Field(default_factory=list)
    causation_id: str | None = None
    correlation_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    occurred_at_ns: int = Field(ge=1)


class LocalEventStore:
    """Append-only, monotonic run event log shared by local processes."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = Path(run_id).name
        if not run_id or self.run_id != run_id:
            raise ValueError("run_id must be one safe path component")
        self.path = self.workspace / "Work" / "runs" / run_id / "events" / "events.jsonl"
        self.lock_path = self.path.with_suffix(".lock")

    def read(self) -> list[RunEvent]:
        if not self.path.is_file():
            return []
        with exclusive_file_lock(self.lock_path):
            events = [
                RunEvent.model_validate_json(line)
                for line in self.path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        if [event.sequence for event in events] != list(range(1, len(events) + 1)):
            raise RuntimeError("run event sequence is not contiguous")
        return events

    def append(
        self,
        event_type: RunEventType,
        *,
        stage_id: str | None = None,
        task_id: str | None = None,
        task_attempt_id: str | None = None,
        lease_epoch: int | None = None,
        artifact_refs: list[ArtifactRef] | None = None,
        causation_id: str | None = None,
        correlation_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> RunEvent:
        validate_bound_project_write_lease(self.workspace)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            lines = (
                self.path.read_text(encoding="utf-8").splitlines()
                if self.path.is_file()
                else []
            )
            sequence = len([line for line in lines if line.strip()]) + 1
            event = RunEvent(
                run_id=self.run_id,
                sequence=sequence,
                event_type=event_type,
                stage_id=stage_id,
                task_id=task_id,
                task_attempt_id=task_attempt_id,
                lease_epoch=lease_epoch,
                artifact_refs=artifact_refs or [],
                causation_id=causation_id,
                correlation_id=correlation_id,
                payload=payload or {},
                occurred_at_ns=time.time_ns(),
            )
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(event.model_dump_json() + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            return event


class DistributedTask(_StrictModel):
    schema_version: str = "1"
    workflow_id: str
    run_id: str
    stage_id: str
    task_id: str
    task_attempt_id: str
    identity_key: str
    project_operation: str
    project_lease_owner_id: str
    project_lease_epoch: int = Field(ge=1)
    input_artifacts: list[ArtifactRef] = Field(default_factory=list)
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at_ns: int = Field(ge=1)


class DistributedTaskCompletion(_StrictModel):
    schema_version: str = "1"
    workflow_id: str
    run_id: str
    task_id: str
    task_attempt_id: str
    worker_id: str
    lease_epoch: int = Field(ge=1)
    project_lease_epoch: int = Field(ge=1)
    status: Literal["completed", "failed", "needs_input"]
    output_artifacts: list[ArtifactRef] = Field(default_factory=list)
    error: str | None = None
    completed_at_ns: int = Field(ge=1)


class DistributedTaskClaim:
    def __init__(
        self,
        queue: "LocalTaskQueue",
        task: DistributedTask,
        worker_id: str,
        lease_handle: IdentityLeaseHandle,
        project_lease: ProjectWriteLease,
        project_lease_token,
    ) -> None:
        self.queue = queue
        self.task = task
        self.worker_id = worker_id
        self.lease_handle = lease_handle
        self.project_lease = project_lease
        self._project_lease_token = project_lease_token
        self._released = False

    @property
    def lease_epoch(self) -> int:
        return self.lease_handle.lease.lease_epoch

    def release(self) -> None:
        if self._released:
            return
        try:
            self.lease_handle.release()
        finally:
            reset_project_write_lease(self._project_lease_token)
            self._released = True


class LocalTaskQueue:
    """Project-local queue whose claims survive coordinator object loss."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = Path(run_id).name
        if not run_id or self.run_id != run_id:
            raise ValueError("run_id must be one safe path component")
        self.root = self.workspace / "Work" / "runs" / run_id / "distributed"
        self.queued_root = self.root / "queued"
        self.claim_root = self.root / "claims"
        self.completion_root = self.root / "completions"
        self.queue_lock_path = self.root / "queue.lock"

    @staticmethod
    def _safe(value: str, field: str) -> str:
        if not value or Path(value).name != value:
            raise ValueError(f"{field} must be one safe path component")
        return value

    def _task_path(self, task: DistributedTask) -> Path:
        self._safe(task.task_id, "task_id")
        self._safe(task.task_attempt_id, "task_attempt_id")
        return self.queued_root / task.task_id / f"{task.task_attempt_id}.json"

    def _completion_path(self, task: DistributedTask) -> Path:
        return self.completion_root / task.task_id / f"{task.task_attempt_id}.json"

    def enqueue(self, task: DistributedTask) -> Path:
        if task.run_id != self.run_id:
            raise ValueError("distributed task belongs to another run")
        validate_bound_project_write_lease(self.workspace)
        self._validate_project_lease(task)
        with exclusive_file_lock(self.queue_lock_path):
            path = self._task_path(task)
            payload = task.model_dump(mode="json")
            if path.is_file():
                existing = DistributedTask.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if existing != task:
                    raise RuntimeError(
                        "distributed task identity already has different bytes"
                    )
                return path
            atomic_write_json(path, payload)
            LocalEventStore(self.workspace, self.run_id).append(
                "TaskDispatched",
                stage_id=task.stage_id,
                task_id=task.task_id,
                task_attempt_id=task.task_attempt_id,
                artifact_refs=task.input_artifacts,
                correlation_id=task.workflow_id,
            )
            return path

    def _queued_tasks(self) -> list[DistributedTask]:
        tasks = []
        for path in sorted(self.queued_root.glob("*/*.json")):
            task = DistributedTask.model_validate_json(path.read_text(encoding="utf-8"))
            if not self._completion_path(task).is_file():
                tasks.append(task)
        return tasks

    def _validate_project_lease(self, task: DistributedTask) -> ProjectWriteLease:
        manager = ProjectWriteLeaseManager(self.workspace)
        lease = ProjectWriteLease(
            project_id=manager.project_id,
            run_id=task.run_id,
            operation=task.project_operation,
            owner_id=task.project_lease_owner_id,
            lease_epoch=task.project_lease_epoch,
            acquired_at_ns=1,
        )
        manager.validate(lease)
        return lease

    def claim_next(self, worker_id: str) -> DistributedTaskClaim | None:
        self._safe(worker_id, "worker_id")
        for task in self._queued_tasks():
            project_lease = self._validate_project_lease(task)
            manager = IdentityLeaseManager(self.workspace, self.run_id)
            try:
                handle = manager.acquire(
                    task.workflow_id,
                    task.identity_key,
                    owner_id=f"worker:{worker_id}:{task.task_attempt_id}",
                )
            except RuntimeError:
                continue
            token = bind_project_write_lease(self.workspace, project_lease)
            try:
                claim = DistributedTaskClaim(
                    self,
                    task,
                    worker_id,
                    handle,
                    project_lease,
                    token,
                )
                atomic_write_json(
                    self.claim_root / task.task_id / f"{task.task_attempt_id}.json",
                    {
                        "worker_id": worker_id,
                        "task_id": task.task_id,
                        "task_attempt_id": task.task_attempt_id,
                        "identity_key": task.identity_key,
                        "lease_owner_id": handle.lease.owner_id,
                        "lease_epoch": handle.lease.lease_epoch,
                        "project_lease_owner_id": project_lease.owner_id,
                        "project_lease_epoch": project_lease.lease_epoch,
                        "claimed_at_ns": time.time_ns(),
                    },
                )
                LocalEventStore(self.workspace, self.run_id).append(
                    "AttemptStarted",
                    stage_id=task.stage_id,
                    task_id=task.task_id,
                    task_attempt_id=task.task_attempt_id,
                    lease_epoch=handle.lease.lease_epoch,
                    correlation_id=task.workflow_id,
                    payload={
                        "worker_id": worker_id,
                        "project_lease_epoch": project_lease.lease_epoch,
                    },
                )
                return claim
            except BaseException:
                try:
                    handle.release()
                finally:
                    reset_project_write_lease(token)
                raise
        return None

    def complete(
        self,
        claim: DistributedTaskClaim,
        *,
        status: Literal["completed", "failed", "needs_input"],
        output_artifacts: list[ArtifactRef] | None = None,
        error: str | None = None,
    ) -> DistributedTaskCompletion:
        validate_bound_project_write_lease(self.workspace)
        if claim.queue is not self:
            raise ValueError("distributed claim belongs to another queue")
        ProjectWriteLeaseManager(self.workspace).validate(claim.project_lease)
        claim.lease_handle.validate()
        completion = DistributedTaskCompletion(
            workflow_id=claim.task.workflow_id,
            run_id=self.run_id,
            task_id=claim.task.task_id,
            task_attempt_id=claim.task.task_attempt_id,
            worker_id=claim.worker_id,
            lease_epoch=claim.lease_epoch,
            project_lease_epoch=claim.project_lease.lease_epoch,
            status=status,
            output_artifacts=output_artifacts or [],
            error=error,
            completed_at_ns=time.time_ns(),
        )
        path = self._completion_path(claim.task)
        if path.is_file():
            existing = DistributedTaskCompletion.model_validate_json(
                path.read_text(encoding="utf-8")
            )
            if existing != completion:
                raise RuntimeError("distributed completion is immutable")
        else:
            atomic_write_json(path, completion.model_dump(mode="json"))
        LocalEventStore(self.workspace, self.run_id).append(
            "TypedResultAccepted" if status == "completed" else "TaskFailed",
            stage_id=claim.task.stage_id,
            task_id=claim.task.task_id,
            task_attempt_id=claim.task.task_attempt_id,
            lease_epoch=claim.lease_epoch,
            artifact_refs=completion.output_artifacts,
            correlation_id=claim.task.workflow_id,
            payload={"worker_id": claim.worker_id, "status": status},
        )
        claim.release()
        return completion

    def completions(self) -> list[DistributedTaskCompletion]:
        return [
            DistributedTaskCompletion.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.completion_root.glob("*/*.json"))
        ]


class WorkspaceMaterializer:
    """Verify logical refs and expose read-only worker inputs in a temp sandbox."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = Path(run_id).name
        self.materialized_root = (
            self.workspace / "Work" / "runs" / self.run_id / "materialized"
        )

    def materialize(self, artifacts: list[ArtifactRef]) -> Path:
        self.materialized_root.mkdir(parents=True, exist_ok=True)
        sandbox = Path(
            tempfile.mkdtemp(prefix="worker-", dir=self.materialized_root)
        ).resolve()
        try:
            content_store = ContentAddressedStore(self.workspace)
            for artifact in artifacts:
                source = (self.workspace / artifact.ref).resolve()
                if (
                    not source.is_relative_to(self.workspace)
                    or not source.is_file()
                ):
                    raise ValueError(f"artifact is not readable: {artifact.ref}")
                if artifact.trusted_handle_ref is not None:
                    handle = content_store.load_trusted_handle(
                        Path(artifact.trusted_handle_ref)
                    )
                    blob = content_store.resolve_trusted_handle(
                        handle,
                        expected_sha256=artifact.sha256,
                        expected_size=artifact.size,
                    )
                    if source != blob:
                        raise ValueError(
                            f"trusted artifact view does not resolve to its blob: {artifact.ref}"
                        )
                else:
                    content = source.read_bytes()
                    if len(content) != artifact.size:
                        raise ValueError(f"artifact size mismatch: {artifact.ref}")
                    if hashlib.sha256(content).hexdigest() != artifact.sha256:
                        raise ValueError(f"artifact hash mismatch: {artifact.ref}")
                target = sandbox / "inputs" / artifact.ref
                target.parent.mkdir(parents=True, exist_ok=True)
                linked = False
                try:
                    target.symlink_to(os.path.relpath(source, start=target.parent))
                    linked = True
                except (NotImplementedError, OSError):
                    shutil.copyfile(source, target)
                if not linked:
                    target.chmod(0o400)
            (sandbox / "outputs").mkdir(parents=True, exist_ok=True)
            return sandbox
        except BaseException:
            self.cleanup(sandbox)
            raise

    def publish_output(
        self,
        sandbox: Path,
        source: Path,
        logical_ref: str,
        *,
        media_type: str,
    ) -> ArtifactRef:
        validate_bound_project_write_lease(self.workspace)
        sandbox = Path(sandbox).resolve()
        source = Path(source).resolve()
        if not source.is_relative_to(sandbox / "outputs") or not source.is_file():
            raise ValueError("worker output must be a file beneath sandbox/outputs")
        logical_path = Path(logical_ref)
        if logical_path.is_absolute() or ".." in logical_path.parts:
            raise ValueError("worker output ref must be workspace-relative")
        target = (self.workspace / logical_path).resolve()
        run_root = (self.workspace / "Work" / "runs" / self.run_id).resolve()
        if not target.is_relative_to(run_root):
            raise ValueError("worker output must remain beneath its run")
        content_store = ContentAddressedStore(self.workspace)
        blob = content_store.ingest_file(source)
        handle = content_store.issue_trusted_handle(
            blob,
            lineage_id=f"worker-output:{self.run_id}:{logical_path.as_posix()}",
        )
        if target.exists() or target.is_symlink():
            if not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != blob.sha256:
                raise RuntimeError("published worker output already exists with different bytes")
        else:
            try:
                content_store.link_trusted_view(handle, target)
            except FileExistsError:
                if (
                    not target.is_file()
                    or hashlib.sha256(target.read_bytes()).hexdigest()
                    != blob.sha256
                ):
                    raise RuntimeError(
                        "concurrent worker output publish resolved to different bytes"
                    ) from None
        active_lease = current_bound_project_write_lease(self.workspace)
        return ArtifactRef(
            ref=logical_path.as_posix(),
            sha256=blob.sha256,
            size=blob.size,
            media_type=media_type,
            trusted_handle_ref=handle.manifest_ref.as_posix(),
            project_lease_epoch=(
                active_lease.lease_epoch if active_lease is not None else None
            ),
        )

    def cleanup(self, sandbox: Path) -> None:
        sandbox = Path(sandbox).resolve()
        root = self.materialized_root.resolve()
        if (
            not sandbox.is_relative_to(root)
            or sandbox.parent != root
            or not sandbox.name.startswith("worker-")
        ):
            raise ValueError("worker sandbox cleanup target is outside materialized root")
        shutil.rmtree(sandbox)


class RunProjection(_StrictModel):
    run_id: str
    event_cursor: int = 0
    run_status: str = "unknown"
    stages: dict[str, dict[str, Any]] = Field(default_factory=dict)
    tasks: dict[str, dict[str, Any]] = Field(default_factory=dict)
    artifacts: dict[str, ArtifactRef] = Field(default_factory=dict)

    @classmethod
    def rebuild(cls, run_id: str, events: list[RunEvent]) -> "RunProjection":
        projection = cls(run_id=run_id)
        for event in events:
            if event.run_id != run_id or event.sequence != projection.event_cursor + 1:
                raise ValueError("projection event identity or sequence mismatch")
            projection.event_cursor = event.sequence
            if event.event_type == "RunQueued":
                projection.run_status = "queued"
            elif event.event_type == "RunClaimed":
                projection.run_status = "running"
            elif event.event_type == "CancelRequested":
                projection.run_status = "cancel_requested"
            elif event.event_type == "RunWaitingUser":
                projection.run_status = "needs_input"
            elif event.event_type == "RunAmbiguous":
                projection.run_status = "ambiguous"
            elif event.event_type == "RunCompleted":
                projection.run_status = "completed"
            elif event.event_type == "RunFailed":
                projection.run_status = "failed"
            elif event.event_type == "RunCancelled":
                projection.run_status = "cancelled"
            if event.stage_id:
                stage = projection.stages.setdefault(event.stage_id, {})
                if event.event_type == "StageReady":
                    stage["status"] = "ready"
                elif event.event_type == "StageCompleted":
                    stage["status"] = "completed"
            if event.task_id:
                task = projection.tasks.setdefault(event.task_id, {})
                task["current_attempt"] = event.task_attempt_id
                if event.event_type == "TaskDispatched":
                    task["status"] = "pending"
                elif event.event_type == "AttemptStarted":
                    task["status"] = "running"
                elif event.event_type == "TypedResultAccepted":
                    task["status"] = "completed"
                elif event.event_type == "TaskFailed":
                    task["status"] = "failed"
            for artifact in event.artifact_refs:
                projection.artifacts[artifact.ref] = artifact
        return projection
