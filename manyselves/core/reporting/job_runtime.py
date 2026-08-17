"""Durable run-level job queue for headless reporting workers."""

from __future__ import annotations

import hashlib
import json
import time
import zipfile
from pathlib import Path
from typing import Literal

from pydantic import Field

from .agentic_models import StrictModel
from .distributed_runtime import LocalEventStore, RunProjection
from .models import ReportRequest, RevisionRequest
from .parallel_runtime import (
    ProjectWriteLeaseHandle,
    ProjectWriteLeaseManager,
    atomic_write_json,
    bind_project_write_lease,
    exclusive_file_lock,
    reset_project_write_lease,
)

JobStatus = Literal[
    "queued",
    "running",
    "needs_input",
    "completed",
    "failed",
    "cancelled",
]


class ReportingJob(StrictModel):
    schema_version: Literal["1"] = "1"
    job_id: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    job_kind: Literal["report", "revision"] = "report"
    idempotency_key: str = Field(min_length=1)
    request_ref: str = Field(min_length=1)
    status: JobStatus = "queued"
    cancel_requested: bool = False
    resume_requested: bool = False
    attempt: int = Field(default=0, ge=0)
    worker_id: str | None = None
    project_lease_epoch: int | None = Field(default=None, ge=1)
    result_ref: str | None = None
    error: str | None = None
    created_at_ns: int = Field(ge=1)
    updated_at_ns: int = Field(ge=1)


class ReportingJobClaim:
    """One live worker claim holding the project's fenced write lease."""

    def __init__(
        self,
        store: "LocalReportingJobStore",
        job: ReportingJob,
        worker_id: str,
        lease_handle: ProjectWriteLeaseHandle,
        lease_token,
    ) -> None:
        self.store = store
        self.job = job
        self.worker_id = worker_id
        self.lease_handle = lease_handle
        self._lease_token = lease_token
        self._released = False

    @property
    def lease(self):
        return self.lease_handle.lease

    def release(self) -> None:
        if self._released:
            return
        try:
            self.lease_handle.release()
        finally:
            reset_project_write_lease(self._lease_token)
            self._released = True


class LocalReportingJobStore:
    """Process-safe queue and transaction record for one project volume."""

    TERMINAL = frozenset({"needs_input", "completed", "failed", "cancelled"})

    def __init__(self, workspace: Path, *, state_root: Path | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.project_manager = ProjectWriteLeaseManager(self.workspace)
        self.project_id = self.project_manager.project_id
        configured_root = (
            Path(state_root).resolve()
            if state_root is not None
            else self.workspace / "Work" / "service-state"
        )
        self.root = configured_root / "projects" / self.project_id / "jobs"
        self.lock_path = self.root / "jobs.lock"

    @staticmethod
    def _safe_component(value: str, field: str) -> str:
        if not value or Path(value).name != value:
            raise ValueError(f"{field} must be one safe path component")
        return value

    @staticmethod
    def _idempotency_key(run_id: str, operation: str) -> str:
        return hashlib.sha256(f"{run_id}\0{operation}".encode("utf-8")).hexdigest()

    def _path(self, job_id: str) -> Path:
        self._safe_component(job_id, "job_id")
        return self.root / f"{job_id}.json"

    def _load_all_unlocked(self) -> list[ReportingJob]:
        if not self.root.is_dir():
            return []
        return [
            ReportingJob.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("job-*.json"))
        ]

    def enqueue(self, run_id: str, request: ReportRequest) -> ReportingJob:
        self._safe_component(run_id, "run_id")
        request_ref = f"Work/runs/{run_id}/request.json"
        request_path = self.workspace / request_ref
        if not request_path.is_file():
            raise FileNotFoundError("report request must be persisted before enqueue")
        persisted = ReportRequest.model_validate_json(
            request_path.read_text(encoding="utf-8")
        )
        if persisted != request:
            raise ValueError("persisted report request does not match queued request")
        key = self._idempotency_key(run_id, request.operation)
        job_id = f"job-{key[:24]}"
        now = time.time_ns()
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            path = self._path(job_id)
            if path.is_file():
                existing = ReportingJob.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
                if (
                    existing.run_id != run_id
                    or existing.operation != request.operation
                    or existing.request_ref != request_ref
                ):
                    raise RuntimeError("reporting job idempotency collision")
                return existing
            job = ReportingJob(
                job_id=job_id,
                project_id=self.project_id,
                run_id=run_id,
                operation=request.operation,
                job_kind="report",
                idempotency_key=key,
                request_ref=request_ref,
                created_at_ns=now,
                updated_at_ns=now,
            )
            atomic_write_json(path, job.model_dump(mode="json"))
        LocalEventStore(self.workspace, run_id).append(
            "RunQueued",
            correlation_id=job_id,
            payload={"operation": request.operation},
        )
        return job

    def enqueue_revision(
        self, run_id: str, request: RevisionRequest
    ) -> ReportingJob:
        self._safe_component(run_id, "run_id")
        request_ref = f"Work/runs/{run_id}/revision-request.json"
        request_path = self.workspace / request_ref
        if not request_path.is_file():
            raise FileNotFoundError("revision request must be persisted before enqueue")
        persisted = RevisionRequest.model_validate_json(
            request_path.read_text(encoding="utf-8")
        )
        if persisted != request:
            raise ValueError("persisted revision request does not match queued request")
        operation = "revision"
        key = self._idempotency_key(run_id, operation)
        job_id = f"job-{key[:24]}"
        now = time.time_ns()
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            path = self._path(job_id)
            if path.is_file():
                return ReportingJob.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            job = ReportingJob(
                job_id=job_id,
                project_id=self.project_id,
                run_id=run_id,
                operation=operation,
                job_kind="revision",
                idempotency_key=key,
                request_ref=request_ref,
                created_at_ns=now,
                updated_at_ns=now,
            )
            atomic_write_json(path, job.model_dump(mode="json"))
        LocalEventStore(self.workspace, run_id).append(
            "RunQueued",
            correlation_id=job_id,
            payload={"operation": operation},
        )
        return job

    def requeue(self, run_id: str) -> ReportingJob:
        job = self.get_run(run_id)
        if job.status == "completed":
            result_path = self.workspace / f"Work/runs/{run_id}.json"
            try:
                payload = json.loads(result_path.read_text(encoding="utf-8"))
                outputs = payload.get("output_paths")
                resolved = [
                    Path(path)
                    if Path(path).is_absolute()
                    else self.workspace / path
                    for path in outputs or []
                ]
                complete = bool(resolved) and all(
                    path.is_file()
                    and path.stat().st_size > 0
                    and (
                        path.suffix.casefold() != ".docx"
                        or zipfile.is_zipfile(path)
                    )
                    for path in resolved
                )
            except (OSError, ValueError, TypeError):
                complete = False
            if complete:
                raise ValueError("completed run with readable outputs must be revised, not resumed")
        with exclusive_file_lock(self.lock_path):
            current = self.get(job.job_id)
            if current.status in {"queued", "running"}:
                return current
            updated = current.model_copy(
                update={
                    "status": "queued",
                    "resume_requested": True,
                    "cancel_requested": False,
                    "worker_id": None,
                    "project_lease_epoch": None,
                    "error": None,
                    "updated_at_ns": time.time_ns(),
                }
            )
            atomic_write_json(
                self._path(current.job_id), updated.model_dump(mode="json")
            )
        LocalEventStore(self.workspace, run_id).append(
            "RunQueued",
            correlation_id=job.job_id,
            payload={"operation": job.operation, "resume": True},
        )
        return updated

    def stop_waiting(self, run_id: str, *, reason: str) -> ReportingJob:
        job = self.get_run(run_id)
        if job.status != "needs_input":
            raise ValueError("only a waiting run can be stopped without a worker")
        with exclusive_file_lock(self.lock_path):
            current = self.get(job.job_id)
            updated = current.model_copy(
                update={
                    "status": "cancelled",
                    "cancel_requested": True,
                    "error": reason,
                    "updated_at_ns": time.time_ns(),
                }
            )
            atomic_write_json(
                self._path(current.job_id), updated.model_dump(mode="json")
            )
        LocalEventStore(self.workspace, run_id).append(
            "RunCancelled",
            correlation_id=job.job_id,
            payload={"reason": reason},
        )
        return updated

    def get(self, job_id: str) -> ReportingJob:
        path = self._path(job_id)
        if not path.is_file():
            raise FileNotFoundError(f"unknown reporting job: {job_id}")
        return ReportingJob.model_validate_json(path.read_text(encoding="utf-8"))

    def get_run(self, run_id: str) -> ReportingJob:
        self._safe_component(run_id, "run_id")
        with exclusive_file_lock(self.lock_path):
            matches = [job for job in self._load_all_unlocked() if job.run_id == run_id]
        if len(matches) != 1:
            raise FileNotFoundError(f"reporting run is missing or non-unique: {run_id}")
        return matches[0]

    def list(self) -> list[ReportingJob]:
        with exclusive_file_lock(self.lock_path):
            return self._load_all_unlocked()

    def claim_next(self, worker_id: str) -> ReportingJobClaim | None:
        self._safe_component(worker_id, "worker_id")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            candidates = sorted(
                (
                    job
                    for job in self._load_all_unlocked()
                    if job.status in {"queued", "running"}
                ),
                key=lambda job: (job.created_at_ns, job.job_id),
            )
            for job in candidates:
                try:
                    lease_handle = self.project_manager.acquire(
                        job.run_id,
                        job.operation,
                        owner_id=f"report-worker:{worker_id}:{job.job_id}",
                    )
                except RuntimeError:
                    continue
                token = bind_project_write_lease(
                    self.workspace, lease_handle.lease
                )
                try:
                    claimed = job.model_copy(
                        update={
                            "status": "running",
                            "attempt": job.attempt + 1,
                            "worker_id": worker_id,
                            "project_lease_epoch": lease_handle.lease.lease_epoch,
                            "updated_at_ns": time.time_ns(),
                            "error": None,
                        }
                    )
                    atomic_write_json(
                        self._path(job.job_id), claimed.model_dump(mode="json")
                    )
                    LocalEventStore(self.workspace, job.run_id).append(
                        "RunClaimed",
                        lease_epoch=lease_handle.lease.lease_epoch,
                        correlation_id=job.job_id,
                        payload={
                            "worker_id": worker_id,
                            "attempt": claimed.attempt,
                        },
                    )
                    return ReportingJobClaim(
                        self,
                        claimed,
                        worker_id,
                        lease_handle,
                        token,
                    )
                except BaseException:
                    try:
                        lease_handle.release()
                    finally:
                        reset_project_write_lease(token)
                    raise
        return None

    def request_cancel(self, run_id: str) -> ReportingJob:
        job = self.get_run(run_id)
        if job.status in self.TERMINAL:
            return job
        with exclusive_file_lock(self.lock_path):
            current = self.get(job.job_id)
            updated = current.model_copy(
                update={"cancel_requested": True, "updated_at_ns": time.time_ns()}
            )
            atomic_write_json(
                self._path(job.job_id), updated.model_dump(mode="json")
            )
        LocalEventStore(self.workspace, run_id).append(
            "CancelRequested",
            correlation_id=job.job_id,
        )
        return updated

    def cancel_requested(self, job_id: str) -> bool:
        return self.get(job_id).cancel_requested

    def finish(
        self,
        claim: ReportingJobClaim,
        *,
        status: JobStatus,
        result_ref: str | None = None,
        error: str | None = None,
    ) -> ReportingJob:
        if claim.store is not self:
            raise ValueError("reporting job claim belongs to another store")
        if status not in self.TERMINAL:
            raise ValueError("reporting job can only finish at a terminal state")
        claim.lease_handle.validate()
        with exclusive_file_lock(self.lock_path):
            current = self.get(claim.job.job_id)
            if (
                current.status != "running"
                or current.worker_id != claim.worker_id
                or current.project_lease_epoch != claim.lease.lease_epoch
            ):
                raise RuntimeError("stale reporting job worker cannot finish")
            updated = current.model_copy(
                update={
                    "status": status,
                    "result_ref": result_ref,
                    "error": error,
                    "updated_at_ns": time.time_ns(),
                }
            )
            atomic_write_json(
                self._path(current.job_id), updated.model_dump(mode="json")
            )
        event_type = {
            "completed": "RunCompleted",
            "needs_input": "RunWaitingUser",
            "cancelled": "RunCancelled",
        }.get(status, "RunFailed")
        LocalEventStore(self.workspace, current.run_id).append(
            event_type,
            lease_epoch=claim.lease.lease_epoch,
            correlation_id=current.job_id,
            payload={"status": status, "result_ref": result_ref, "error": error},
        )
        claim.release()
        return updated

    def projection(self, run_id: str) -> RunProjection:
        return RunProjection.rebuild(
            run_id, LocalEventStore(self.workspace, run_id).read()
        )


class ReportingJobWorker:
    """Execute one durable job without owning GUI or API process state."""

    def __init__(self, service, jobs: LocalReportingJobStore, worker_id: str) -> None:
        self.service = service
        self.jobs = jobs
        self.worker_id = self.jobs._safe_component(worker_id, "worker_id")

    async def run_once(self):
        from .service import ReportingRunResult

        claim = self.jobs.claim_next(self.worker_id)
        if claim is None:
            return None
        try:
            if self.jobs.cancel_requested(claim.job.job_id):
                result = ReportingRunResult(
                    run_id=claim.job.run_id,
                    status="cancelled",
                    error="cancel requested before worker execution",
                )
                self.service._save_run(result)
            else:
                request_payload = (
                    self.jobs.workspace / claim.job.request_ref
                ).read_text(encoding="utf-8")
                if claim.job.job_kind == "revision":
                    request = RevisionRequest.model_validate_json(request_payload)
                    result = await self.service.run_revision_claimed(
                        request,
                        claim.job.run_id,
                        claim.lease,
                        resume=claim.job.resume_requested,
                    )
                else:
                    request = ReportRequest.model_validate_json(request_payload)
                    result = await self.service.run_prepared_claimed(
                        request,
                        claim.job.run_id,
                        claim.lease,
                        resume=claim.job.resume_requested,
                    )
            if result.status == "completed":
                normalized: JobStatus = "completed"
            elif result.status in {"cancelled", "stopped_incomplete"}:
                normalized = "cancelled"
            elif result.status in {
                "needs_decision",
                "needs_user_decision",
                "needs_scope_expansion",
                "blocked",
            }:
                normalized = "needs_input"
            else:
                normalized = "failed"
            self.jobs.finish(
                claim,
                status=normalized,
                result_ref=f"Work/runs/{claim.job.run_id}.json",
                error=result.error,
            )
            return result
        except BaseException as exc:
            if not claim._released:
                self.jobs.finish(claim, status="failed", error=str(exc))
            raise
