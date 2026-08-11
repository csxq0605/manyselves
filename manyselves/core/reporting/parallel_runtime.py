"""Durable contracts for isolated, recoverable reporting lanes.

The reporting workflow is still coordinated in-process, but these primitives
deliberately use project-local files and POSIX advisory locks so task identity,
lease ownership, and successful results do not depend on one Python object's
memory.  They are the compatibility boundary for later worker processes.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import socket
import tempfile
import time
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Literal, TextIO
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _safe_component(value: str, *, field: str) -> str:
    if not value or Path(value).name != value or value in {".", ".."}:
        raise ValueError(f"{field} must be one safe path component")
    return value


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="wb",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
        temporary = Path(handle.name)
    os.replace(temporary, path)


def atomic_write_json(path: Path, value: Any) -> None:
    _atomic_write_bytes(
        path,
        json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8") + b"\n",
    )


@contextmanager
def exclusive_file_lock(path: Path):
    """Serialize one project-local read/modify/write section across processes."""

    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = path.open("a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ArtifactRef(_StrictModel):
    """Logical immutable artifact identity safe to pass between workers."""

    ref: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    media_type: str = Field(min_length=1)
    trusted_handle_ref: str | None = None
    project_lease_epoch: int | None = Field(default=None, ge=1)

    @field_validator("ref")
    @classmethod
    def logical_workspace_ref(cls, value: str) -> str:
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError("artifact ref must be one canonical workspace-relative path")
        return value

    @field_validator("trusted_handle_ref")
    @classmethod
    def trusted_handle_is_logical_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = Path(value)
        if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
            raise ValueError(
                "trusted handle ref must be one canonical workspace-relative path"
            )
        return value


class IdentityLease(_StrictModel):
    workflow_id: str = Field(min_length=1)
    identity_key: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)
    acquired_at_ns: int = Field(ge=1)
    released_at_ns: int | None = Field(default=None, ge=1)
    status: Literal["active", "released"] = "active"


class IdentityLeaseHandle:
    """A live process-owned lease; the file descriptor is the crash fence."""

    def __init__(
        self,
        manager: "IdentityLeaseManager",
        lease: IdentityLease,
        handle: TextIO,
    ) -> None:
        self.manager = manager
        self.lease = lease
        self._handle = handle
        self._released = False

    def validate(self) -> None:
        self.manager.validate(self.lease)

    def release(self) -> None:
        if self._released:
            return
        self.manager._release(self.lease, self._handle)
        self._released = True

    def __enter__(self) -> "IdentityLeaseHandle":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()


class IdentityLeaseManager:
    """Cross-process exclusive identity leases with monotonic fencing epochs."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = _safe_component(run_id, field="run_id")
        self.root = self.workspace / "Work" / "runs" / self.run_id / "leases" / "identity"
        self.root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def default_owner_id() -> str:
        return f"{socket.gethostname()}:{os.getpid()}:{uuid4().hex}"

    @staticmethod
    def _key(workflow_id: str, identity_key: str) -> str:
        return hashlib.sha256(
            _canonical_json_bytes([workflow_id, identity_key])
        ).hexdigest()

    def _paths(self, workflow_id: str, identity_key: str) -> tuple[Path, Path]:
        key = self._key(workflow_id, identity_key)
        return self.root / f"{key}.json", self.root / f"{key}.lock"

    def acquire(
        self,
        workflow_id: str,
        identity_key: str,
        *,
        owner_id: str | None = None,
    ) -> IdentityLeaseHandle:
        record_path, lock_path = self._paths(workflow_id, identity_key)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(
                "reporting identity is already active: "
                f"workflow={workflow_id} identity={identity_key}"
            ) from exc

        previous_epoch = 0
        if record_path.is_file():
            try:
                previous_epoch = int(
                    json.loads(record_path.read_text(encoding="utf-8")).get(
                        "lease_epoch", 0
                    )
                )
            except (OSError, ValueError, TypeError):
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise RuntimeError(f"identity lease record is invalid: {record_path}")

        lease = IdentityLease(
            workflow_id=workflow_id,
            identity_key=identity_key,
            owner_id=owner_id or self.default_owner_id(),
            lease_epoch=previous_epoch + 1,
            acquired_at_ns=time.time_ns(),
        )
        atomic_write_json(record_path, lease.model_dump(mode="json"))
        return IdentityLeaseHandle(self, lease, handle)

    def validate(self, lease: IdentityLease) -> None:
        record_path, _lock_path = self._paths(lease.workflow_id, lease.identity_key)
        try:
            current = IdentityLease.model_validate_json(
                record_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("identity lease record is missing or invalid") from exc
        if (
            current.status != "active"
            or current.owner_id != lease.owner_id
            or current.lease_epoch != lease.lease_epoch
        ):
            raise RuntimeError(
                "stale identity lease rejected: "
                f"expected owner={lease.owner_id} epoch={lease.lease_epoch}; "
                f"current owner={current.owner_id} epoch={current.lease_epoch} "
                f"status={current.status}"
            )

    def _release(self, lease: IdentityLease, handle: TextIO) -> None:
        record_path, _lock_path = self._paths(lease.workflow_id, lease.identity_key)
        try:
            self.validate(lease)
            released = lease.model_copy(
                update={"status": "released", "released_at_ns": time.time_ns()}
            )
            atomic_write_json(record_path, released.model_dump(mode="json"))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


class ProjectWriteLease(_StrictModel):
    project_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    operation: str = Field(min_length=1)
    owner_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)
    acquired_at_ns: int = Field(ge=1)
    released_at_ns: int | None = Field(default=None, ge=1)
    status: Literal["active", "released"] = "active"


class ProjectWriteLeaseHandle:
    def __init__(
        self,
        manager: "ProjectWriteLeaseManager",
        lease: ProjectWriteLease,
        handle: TextIO,
    ) -> None:
        self.manager = manager
        self.lease = lease
        self._handle = handle
        self._released = False

    def validate(self) -> None:
        self.manager.validate(self.lease)

    def release(self) -> None:
        if self._released:
            return
        self.manager._release(self.lease, self._handle)
        self._released = True


class ProjectWriteLeaseManager:
    """One fenced write operation per project workspace, independent of run id."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "Work" / "leases" / "project-write"
        self.record_path = self.root / "current.json"
        self.lock_path = self.root / "active.lock"

    @property
    def project_id(self) -> str:
        return hashlib.sha256(str(self.workspace).encode("utf-8")).hexdigest()

    def acquire(
        self,
        run_id: str,
        operation: str,
        *,
        owner_id: str | None = None,
    ) -> ProjectWriteLeaseHandle:
        _safe_component(run_id, field="run_id")
        if not operation:
            raise ValueError("operation is required")
        self.root.mkdir(parents=True, exist_ok=True)
        handle = self.lock_path.open("a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError("project already has an active write operation") from exc
        previous_epoch = 0
        if self.record_path.is_file():
            try:
                previous_epoch = ProjectWriteLease.model_validate_json(
                    self.record_path.read_text(encoding="utf-8")
                ).lease_epoch
            except (OSError, ValueError) as exc:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                handle.close()
                raise RuntimeError("project write lease record is invalid") from exc
        lease = ProjectWriteLease(
            project_id=self.project_id,
            run_id=run_id,
            operation=operation,
            owner_id=owner_id or IdentityLeaseManager.default_owner_id(),
            lease_epoch=previous_epoch + 1,
            acquired_at_ns=time.time_ns(),
        )
        atomic_write_json(self.record_path, lease.model_dump(mode="json"))
        return ProjectWriteLeaseHandle(self, lease, handle)

    def validate(self, lease: ProjectWriteLease) -> None:
        try:
            current = ProjectWriteLease.model_validate_json(
                self.record_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise RuntimeError("project write lease is missing or invalid") from exc
        if (
            current.project_id != self.project_id
            or current.status != "active"
            or current.owner_id != lease.owner_id
            or current.lease_epoch != lease.lease_epoch
            or current.run_id != lease.run_id
            or current.operation != lease.operation
        ):
            raise RuntimeError(
                "stale project write lease rejected: "
                f"expected run={lease.run_id} owner={lease.owner_id} "
                f"epoch={lease.lease_epoch}; current run={current.run_id} "
                f"owner={current.owner_id} epoch={current.lease_epoch} "
                f"status={current.status}"
            )

    def _release(self, lease: ProjectWriteLease, handle: TextIO) -> None:
        try:
            self.validate(lease)
            released = lease.model_copy(
                update={"status": "released", "released_at_ns": time.time_ns()}
            )
            atomic_write_json(self.record_path, released.model_dump(mode="json"))
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


_PROJECT_WRITE_LEASE: ContextVar[
    tuple[Path, ProjectWriteLease] | None
] = ContextVar("reporting_project_write_lease", default=None)


def bind_project_write_lease(
    workspace: Path,
    lease: ProjectWriteLease,
) -> Token:
    return _PROJECT_WRITE_LEASE.set((Path(workspace).resolve(), lease))


def reset_project_write_lease(token: Token) -> None:
    _PROJECT_WRITE_LEASE.reset(token)


def validate_bound_project_write_lease(workspace: Path) -> None:
    bound = _PROJECT_WRITE_LEASE.get()
    if bound is None:
        return
    bound_workspace, lease = bound
    workspace = Path(workspace).resolve()
    if workspace != bound_workspace:
        raise RuntimeError("project write fence belongs to another workspace")
    ProjectWriteLeaseManager(workspace).validate(lease)


def current_bound_project_write_lease(
    workspace: Path,
) -> ProjectWriteLease | None:
    """Return the validated bound lease, if this call is outside a write job."""

    bound = _PROJECT_WRITE_LEASE.get()
    if bound is None:
        return None
    bound_workspace, lease = bound
    workspace = Path(workspace).resolve()
    if workspace != bound_workspace:
        raise RuntimeError("project write fence belongs to another workspace")
    ProjectWriteLeaseManager(workspace).validate(lease)
    return lease


class TaskCorrelation(_StrictModel):
    workflow_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    task_attempt_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    identity_key: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    task_envelope_sha256: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    execution_profile_sha256: str = Field(
        default="0" * 64,
        pattern=r"^[0-9a-f]{64}$",
    )
    input_contract_ref: str | None = None
    input_contract_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    subject_ref: str | None = None
    subject_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    lease_owner_id: str = Field(min_length=1)
    lease_epoch: int = Field(ge=1)

    @model_validator(mode="after")
    def paired_refs_and_hashes(self) -> "TaskCorrelation":
        if bool(self.input_contract_ref) != bool(self.input_contract_sha256):
            raise ValueError("input contract ref and hash must be supplied together")
        if bool(self.subject_ref) != bool(self.subject_sha256):
            raise ValueError("subject ref and hash must be supplied together")
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.task_id, field="task_id")
        _safe_component(self.task_attempt_id, field="task_attempt_id")
        return self


class TaskTerminal(_StrictModel):
    kind: Literal["task_terminal"] = "task_terminal"
    correlation: TaskCorrelation
    status: Literal["completed", "blocked", "incomplete", "failed"]
    result_ref: str
    result_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    promoted_to_canonical: bool
    persisted_at_ns: int = Field(ge=1)


class TaskAttemptStore:
    """Append-only result attempts plus fenced canonical compatibility views."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = _safe_component(run_id, field="run_id")
        self.run_root = self.workspace / "Work" / "runs" / self.run_id

    def _current_path(self, task_id: str) -> Path:
        _safe_component(task_id, field="task_id")
        return self.run_root / "task-attempts" / task_id / "current.json"

    def _task_lock(self, task_id: str) -> TextIO:
        path = self.run_root / "task-attempts" / task_id / ".promotion.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = path.open("a+", encoding="utf-8")
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        return handle

    def activate(self, correlation: TaskCorrelation) -> Path:
        validate_bound_project_write_lease(self.workspace)
        if correlation.run_id != self.run_id:
            raise ValueError("task correlation belongs to another run")
        IdentityLeaseManager(self.workspace, self.run_id).validate(
            IdentityLease(
                workflow_id=correlation.workflow_id,
                identity_key=correlation.identity_key,
                owner_id=correlation.lease_owner_id,
                lease_epoch=correlation.lease_epoch,
                acquired_at_ns=1,
            )
        )
        handle = self._task_lock(correlation.task_id)
        try:
            path = self._current_path(correlation.task_id)
            atomic_write_json(path, correlation.model_dump(mode="json"))
            return path
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

    def current(self, task_id: str) -> TaskCorrelation | None:
        path = self._current_path(task_id)
        if not path.is_file():
            return None
        return TaskCorrelation.model_validate_json(path.read_text(encoding="utf-8"))

    def load_verified_result(
        self,
        correlation: TaskCorrelation,
    ) -> tuple[TaskTerminal, dict[str, Any]] | None:
        """Load one fully persisted attempt without requiring its expired lease."""

        relative = (
            Path("Work/runs")
            / self.run_id
            / "results"
            / "attempts"
            / correlation.task_id
            / f"{correlation.task_attempt_id}.json"
        )
        result_path = self.workspace / relative
        terminal_path = result_path.with_suffix(".terminal.json")
        if not result_path.exists() and not terminal_path.exists():
            return None
        if not result_path.is_file() or not terminal_path.is_file():
            raise RuntimeError("persisted task attempt is incomplete")
        try:
            terminal = TaskTerminal.model_validate_json(
                terminal_path.read_text(encoding="utf-8")
            )
            payload = json.loads(result_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            raise RuntimeError("persisted task attempt is unreadable or invalid") from exc
        result_bytes = result_path.read_bytes()
        if (
            terminal.correlation != correlation
            or terminal.result_ref != relative.as_posix()
            or terminal.result_sha256 != _sha256_bytes(result_bytes)
            or terminal.payload_sha256 != _sha256_bytes(
                _canonical_json_bytes(payload)
            )
        ):
            raise RuntimeError("persisted task attempt identity or hash mismatch")
        return terminal, payload

    def persist_result(
        self,
        correlation: TaskCorrelation,
        result_payload: dict[str, Any],
        *,
        status: Literal["completed", "blocked", "incomplete", "failed"],
    ) -> TaskTerminal:
        validate_bound_project_write_lease(self.workspace)
        if correlation.run_id != self.run_id:
            raise ValueError("task correlation belongs to another run")
        IdentityLeaseManager(self.workspace, self.run_id).validate(
            IdentityLease(
                workflow_id=correlation.workflow_id,
                identity_key=correlation.identity_key,
                owner_id=correlation.lease_owner_id,
                lease_epoch=correlation.lease_epoch,
                acquired_at_ns=1,
            )
        )
        payload_bytes = _canonical_json_bytes(result_payload)
        payload_sha256 = _sha256_bytes(payload_bytes)
        serialized = (
            json.dumps(result_payload, ensure_ascii=False, indent=2).encode("utf-8")
            + b"\n"
        )
        result_sha256 = _sha256_bytes(serialized)
        relative = Path(
            "Work/runs"
        ) / self.run_id / "results" / "attempts" / correlation.task_id / (
            f"{correlation.task_attempt_id}.json"
        )
        result_path = self.workspace / relative
        if result_path.is_file():
            if result_path.read_bytes() != serialized:
                raise RuntimeError(
                    "immutable task attempt result already exists with different bytes: "
                    f"{relative.as_posix()}"
                )
        else:
            _atomic_write_bytes(result_path, serialized)

        promoted = False
        handle = self._task_lock(correlation.task_id)
        try:
            current = self.current(correlation.task_id)
            if current == correlation:
                canonical = self.run_root / "results" / f"{correlation.task_id}.json"
                _atomic_write_bytes(canonical, serialized)
                promoted = True
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()

        terminal = TaskTerminal(
            correlation=correlation,
            status=status,
            result_ref=relative.as_posix(),
            result_sha256=result_sha256,
            payload_sha256=payload_sha256,
            promoted_to_canonical=promoted,
            persisted_at_ns=time.time_ns(),
        )
        terminal_path = result_path.with_suffix(".terminal.json")
        terminal_bytes = (
            json.dumps(
                terminal.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
            ).encode("utf-8")
            + b"\n"
        )
        if terminal_path.is_file():
            if terminal_path.read_bytes() != terminal_bytes:
                raise RuntimeError("immutable task terminal already exists with different bytes")
        else:
            _atomic_write_bytes(terminal_path, terminal_bytes)
        return terminal


class LaneTaskSpec(_StrictModel):
    lane_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    preparation_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    collaboration_bundle_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    schema_version: str = "1"


class LaneAttemptRecord(_StrictModel):
    lane_id: str
    task_attempt_id: str
    lease_epoch: int = Field(ge=1)
    started_at_ns: int = Field(ge=1)
    finished_at_ns: int | None = Field(default=None, ge=1)
    status: Literal["started", "completed", "failed", "ambiguous"]
    error: str | None = None


class LaneCompletion(_StrictModel):
    kind: Literal["lane_completion"] = "lane_completion"
    lane_id: str
    run_id: str
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: ArtifactRef
    review_completion: ArtifactRef
    author_task_attempt_id: str
    reviewer_session_id: str
    lease_epoch: int = Field(ge=1)
    schema_version: str = "1"

    def completion_sha256(self) -> str:
        deterministic = self.model_dump(
            mode="json",
            exclude={
                "author_task_attempt_id",
                "reviewer_session_id",
                "lease_epoch",
            },
        )
        return _sha256_bytes(_canonical_json_bytes(deterministic))


class LaneExceptionCandidate(_StrictModel):
    kind: Literal["lane_exception_candidate"] = "lane_exception_candidate"
    lane_id: str
    run_id: str
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    disposition: Literal["needs_input", "disputed", "escalate", "failed"]
    reason: str = Field(min_length=1)
    task_attempt_id: str


class CohortBarrier(_StrictModel):
    kind: Literal["cohort_barrier"] = "cohort_barrier"
    run_id: str
    target_modules: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]]
    completion_refs: dict[str, str]
    completion_hashes: dict[str, str]
    barrier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["full", "partial"] = "full"


class CrossOwnerCompletion(_StrictModel):
    kind: Literal["cross_owner_completion"] = "cross_owner_completion"
    lane_id: str
    run_id: str
    review_round: int = Field(ge=0)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    subject: ArtifactRef
    local_review_completion: ArtifactRef
    machine_validation: ArtifactRef
    author_task_attempt_id: str
    reviewer_session_id: str
    lease_epoch: int = Field(ge=1)
    schema_version: str = "1"

    def completion_sha256(self) -> str:
        deterministic = self.model_dump(
            mode="json",
            exclude={
                "author_task_attempt_id",
                "reviewer_session_id",
                "lease_epoch",
            },
        )
        return _sha256_bytes(_canonical_json_bytes(deterministic))


class CrossOwnerBarrier(_StrictModel):
    kind: Literal["cross_owner_barrier"] = "cross_owner_barrier"
    run_id: str
    review_round: int = Field(ge=0)
    target_modules: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]]
    completion_refs: dict[str, str]
    completion_hashes: dict[str, str]
    barrier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowReducer:
    """Single-writer deterministic promotion of successful module lanes."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = _safe_component(run_id, field="run_id")

    def write_module_barrier(
        self,
        target_modules: list[str],
        completions: list[tuple[str, LaneCompletion]],
        *,
        partial: bool = False,
    ) -> CohortBarrier:
        validate_bound_project_write_lease(self.workspace)
        ordered_targets = sorted(target_modules, key=lambda value: float(value))
        if len(ordered_targets) != len(set(ordered_targets)):
            raise ValueError("module barrier target set contains duplicates")
        by_module = {completion.module_id: (ref, completion) for ref, completion in completions}
        if set(by_module) != set(ordered_targets) or len(by_module) != len(completions):
            raise ValueError("module barrier requires the exact unique target completion set")
        for module_id, (_ref, completion) in by_module.items():
            if completion.run_id != self.run_id or completion.module_id != module_id:
                raise ValueError("lane completion ownership mismatch")
        refs = {module_id: by_module[module_id][0] for module_id in ordered_targets}
        hashes = {
            module_id: by_module[module_id][1].completion_sha256()
            for module_id in ordered_targets
        }
        barrier_payload = {
            "run_id": self.run_id,
            "target_modules": ordered_targets,
            "completion_refs": refs,
            "completion_hashes": hashes,
            "scope": "partial" if partial else "full",
        }
        barrier = CohortBarrier(
            **barrier_payload,
            barrier_sha256=_sha256_bytes(_canonical_json_bytes(barrier_payload)),
        )
        path = (
            self.workspace
            / "Work"
            / "runs"
            / self.run_id
            / "lanes"
            / ("partial-module-barrier.json" if partial else "module-barrier.json")
        )
        atomic_write_json(path, barrier.model_dump(mode="json"))
        return barrier

    def write_cross_owner_barrier(
        self,
        review_round: int,
        target_modules: list[str],
        completions: list[tuple[str, CrossOwnerCompletion]],
    ) -> CrossOwnerBarrier:
        validate_bound_project_write_lease(self.workspace)
        ordered_targets = sorted(target_modules, key=float)
        by_module = {
            completion.module_id: (ref, completion)
            for ref, completion in completions
        }
        if (
            len(ordered_targets) != len(set(ordered_targets))
            or set(by_module) != set(ordered_targets)
            or len(by_module) != len(completions)
        ):
            raise ValueError(
                "Cross owner barrier requires the exact unique target completion set"
            )
        for module_id, (_ref, completion) in by_module.items():
            if (
                completion.run_id != self.run_id
                or completion.review_round != review_round
                or completion.module_id != module_id
            ):
                raise ValueError("Cross owner completion ownership mismatch")
        refs = {module_id: by_module[module_id][0] for module_id in ordered_targets}
        hashes = {
            module_id: by_module[module_id][1].completion_sha256()
            for module_id in ordered_targets
        }
        payload = {
            "run_id": self.run_id,
            "review_round": review_round,
            "target_modules": ordered_targets,
            "completion_refs": refs,
            "completion_hashes": hashes,
        }
        barrier = CrossOwnerBarrier(
            **payload,
            barrier_sha256=_sha256_bytes(_canonical_json_bytes(payload)),
        )
        path = (
            self.workspace
            / "Work"
            / "runs"
            / self.run_id
            / "lanes"
            / f"cross-r{review_round}"
            / "owner-barrier.json"
        )
        atomic_write_json(path, barrier.model_dump(mode="json"))
        return barrier
