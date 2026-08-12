"""Durable contracts for isolated, recoverable reporting lanes.

The reporting workflow is still coordinated in-process, but these primitives
deliberately use project-local files and POSIX advisory locks so task identity,
lease ownership, and successful results do not depend on one Python object's
memory.  They are the compatibility boundary for later worker processes.
"""

from __future__ import annotations

import asyncio
import fcntl
import hashlib
import inspect
import json
import os
import re
import socket
import tempfile
import time
from dataclasses import dataclass, field
from contextlib import contextmanager
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any, Awaitable, Callable, Iterable, Literal, Mapping, TextIO
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

    # ------------------------------------------------------------------
    # Generic lane-attempt journal
    # ------------------------------------------------------------------
    # The result-attempt methods above predate the lane supervisor and are
    # intentionally kept wire-compatible.  The compact lane API uses a
    # separate append-only journal: a terminal record is immutable and a late
    # attempt can never replace an earlier record at the same path.
    def _lane_attempt_root(self, task_id: str) -> Path:
        _safe_component(task_id, field="task_id")
        return self.run_root / "lanes" / task_id / "attempts"

    def append(
        self,
        record: "LaneAttemptRecord",
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> Path:
        """Append one immutable lane attempt and return its workspace ref.

        ``payload`` is optional metadata (for example a completion object). It
        is stored beside, rather than inside, the strict attempt record so
        older readers can continue to validate the record itself.  Reusing an
        attempt id with different bytes is rejected instead of overwriting the
        old attempt.
        """

        if not isinstance(record, LaneAttemptRecord):
            record = LaneAttemptRecord.model_validate(record)
        task_id = record.task_id or record.lane_id
        if task_id is None:
            raise ValueError("lane attempt requires task_id or lane_id")
        # Make the task identity explicit in the journal even when callers
        # supplied only the legacy lane_id spelling.
        if record.task_id != task_id or record.lane_id is None:
            record = record.model_copy(
                update={
                    "task_id": task_id,
                    "lane_id": record.lane_id or task_id,
                }
            )
        root = self._lane_attempt_root(task_id)
        body: dict[str, Any] = {"record": record.model_dump(mode="json")}
        if payload is not None:
            body["payload"] = dict(payload)
        serialized = (
            json.dumps(body, ensure_ascii=False, indent=2).encode("utf-8") + b"\n"
        )
        with exclusive_file_lock(root / ".append.lock"):
            path = root / f"{record.task_attempt_id}.json"
            if path.is_file():
                if path.read_bytes() != serialized:
                    # A started claim and its terminal share one logical
                    # attempt id but are two append-only journal entries.  Use
                    # a content-derived suffix for the latter; never mutate
                    # the original claim in place.
                    suffix = _sha256_bytes(serialized)[:16]
                    path = root / f"{record.task_attempt_id}-{suffix}.json"
                    if path.is_file() and path.read_bytes() != serialized:
                        raise RuntimeError(
                            "immutable lane attempt already exists with different bytes: "
                            f"{path}"
                        )
            else:
                pass
            if not path.is_file():
                _atomic_write_bytes(path, serialized)
        return path

    def load_latest(
        self,
        task_id: str,
        *,
        include_payload: bool = False,
    ) -> "LaneAttemptRecord | tuple[LaneAttemptRecord, dict[str, Any] | None] | None":
        """Load the latest immutable attempt for ``task_id``.

        Ordering is deterministic and based on terminal/start timestamps,
        followed by the attempt id.  A malformed journal is an integrity
        failure, not a reason to silently replay a Provider call.
        """

        root = self._lane_attempt_root(task_id)
        if not root.is_dir():
            return None
        entries: list[tuple[tuple[int, int, str], LaneAttemptRecord, dict[str, Any] | None]] = []
        for path in sorted(root.glob("*.json")):
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise RuntimeError(f"lane attempt journal is unreadable: {path}") from exc
            if isinstance(raw, dict) and "record" in raw:
                record_raw = raw["record"]
                payload = raw.get("payload")
            else:
                # Accept the early direct-record shape for compatibility with
                # hand-written fixtures, while all new writes use the envelope.
                record_raw = raw
                payload = None
            try:
                record = LaneAttemptRecord.model_validate(record_raw)
            except Exception as exc:  # pydantic's concrete error type varies by version
                raise RuntimeError(f"lane attempt journal is invalid: {path}") from exc
            if (record.task_id or record.lane_id) != task_id:
                raise RuntimeError(f"lane attempt task identity mismatch: {path}")
            finished = record.finished_at_ns or 0
            entries.append(((finished, record.started_at_ns, record.task_attempt_id), record, payload))
        if not entries:
            return None
        _key, record, payload = max(entries, key=lambda item: item[0])
        if include_payload:
            return record, payload
        return record

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
    # The compact supervisor contract uses ``task_id/revision/payload_hash``;
    # the legacy module-lane fields remain optional so existing checkpoints
    # continue to deserialize without a migration.
    lane_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] | None = None
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    preparation_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    collaboration_bundle_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    task_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(default=0, ge=0)
    priority: int = 0
    ordinal: int = Field(default=0, ge=0)
    payload_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    schema_version: str = "1"

    @model_validator(mode="after")
    def normalize_identity(self) -> "LaneTaskSpec":
        if self.lane_id is None and self.task_id is None:
            raise ValueError("LaneTaskSpec requires lane_id or task_id")
        if self.lane_id is None:
            self.lane_id = self.task_id
        if self.task_id is None:
            self.task_id = self.lane_id
        if self.payload_hash is None:
            self.payload_hash = self.preparation_sha256 or self.semantic_key
        if self.run_id is not None:
            _safe_component(self.run_id, field="run_id")
        _safe_component(self.task_id or self.lane_id or "", field="task_id")
        return self


class LaneAttemptRecord(_StrictModel):
    lane_id: str | None = None
    task_id: str | None = None
    task_attempt_id: str
    lease_epoch: int = Field(default=1, ge=1)
    started_at_ns: int = Field(default_factory=time.time_ns, ge=1)
    finished_at_ns: int | None = Field(default=None, ge=1)
    # ``started`` is useful for a journal pre-claim; callers may omit it when
    # constructing a record for a successful one-shot lane in tests.
    status: Literal["started", "completed", "failed", "ambiguous", "deferred"] = "started"
    # The frozen API describes timestamps as one logical field.  Keep the
    # explicit nanosecond fields for existing checkpoints while accepting the
    # compact mapping as input.
    timestamps: dict[str, int] | None = None
    disposition: Literal[
        "completed",
        "failed",
        "deferred",
        "accepted_or_unknown",
        "needs_input",
        "disputed",
        "escalate",
        "unknown",
    ] | None = None
    result_ref: str | None = None
    result_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    result_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    error: str | None = None

    @model_validator(mode="after")
    def normalize_attempt_identity(self) -> "LaneAttemptRecord":
        if self.task_id is None and self.lane_id is None:
            raise ValueError("lane attempt requires task_id or lane_id")
        if self.task_id is None:
            self.task_id = self.lane_id
        if self.lane_id is None:
            self.lane_id = self.task_id
        _safe_component(self.task_id or "", field="task_id")
        _safe_component(self.task_attempt_id, field="task_attempt_id")
        if self.timestamps:
            started = self.timestamps.get("started_at_ns", self.timestamps.get("started"))
            finished = self.timestamps.get("finished_at_ns", self.timestamps.get("finished"))
            if started is not None:
                self.started_at_ns = int(started)
            if finished is not None:
                self.finished_at_ns = int(finished)
        if self.result_sha256 is None and self.result_hash is not None:
            self.result_sha256 = self.result_hash
        if self.result_hash is None and self.result_sha256 is not None:
            self.result_hash = self.result_sha256
        if self.disposition is None:
            self.disposition = {
                "started": "deferred" if self.status == "deferred" else "unknown",
                "completed": "completed",
                "failed": "failed",
                "deferred": "deferred",
                "ambiguous": "accepted_or_unknown",
            }[self.status]
        return self


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


# Public name used by the module-work contract.  Keep the original
# ``LaneCompletion`` spelling as a wire-compatible alias for checkpoints and
# older callers; both names validate the exact same immutable completion
# payload.
ModuleLaneCompletion = LaneCompletion


class LaneExceptionCandidate(_StrictModel):
    kind: Literal["lane_exception_candidate"] = "lane_exception_candidate"
    lane_id: str
    run_id: str
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    # ``accepted_or_unknown`` is deliberately terminal.  It records that a
    # Provider attempt may have been accepted even though the typed result was
    # not observed; resume must reconcile that attempt instead of replaying it.
    disposition: Literal[
        "needs_input",
        "disputed",
        "escalate",
        "failed",
        "accepted_or_unknown",
    ]
    reason: str = Field(min_length=1)
    task_attempt_id: str
    attempt_disposition: str | None = None


@dataclass(slots=True)
class CohortRunResult:
    """Terminal view returned by :class:`AllReadySupervisor`.

    The dictionaries are inserted in deterministic task order.  A barrier
    candidate is present only when every claimed lane completed; callers still
    decide whether the candidate is eligible for promotion (for example after
    validating artifact hashes and expected revisions).
    """

    terminals: dict[str, LaneAttemptRecord] = field(default_factory=dict)
    barrier_candidate: dict[str, Any] | None = None
    failures: dict[str, str] = field(default_factory=dict)
    deferred: tuple[str, ...] = ()
    recovered: tuple[str, ...] = ()


class AllReadySupervisor:
    """Run every ready lane concurrently, then reduce terminals once.

    There is deliberately no business concurrency cap here.  ``priority`` and
    ``ordinal`` only choose deterministic claim order; all ready specs are
    claimed before any worker starts, so an immediate failure cannot prevent a
    sibling from being admitted.  Ordinary failures are captured and drained;
    only an explicit hard/cost stop produces deferred terminals.
    """

    def __init__(
        self,
        workspace: Path | None = None,
        run_id: str | None = None,
        *,
        attempt_store: TaskAttemptStore | None = None,
    ) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.run_id = run_id
        self.attempt_store = attempt_store

    @staticmethod
    def _ordered_specs(specs: Iterable[LaneTaskSpec]) -> list[LaneTaskSpec]:
        ordered = list(specs)
        if len({spec.task_id for spec in ordered}) != len(ordered):
            raise ValueError("all-ready cohort requires unique task ids")
        # Higher priority first mirrors AdaptiveTaskScheduler; this is only a
        # claim-order tie break and never truncates the ready set.
        ordered.sort(key=lambda spec: (-spec.priority, spec.ordinal, spec.task_id or ""))
        return ordered

    @staticmethod
    async def _stop_requested(hard_stop: Any) -> bool:
        if hard_stop is None:
            return False
        if isinstance(hard_stop, asyncio.Event):
            return hard_stop.is_set()
        value = hard_stop() if callable(hard_stop) else hard_stop
        if inspect.isawaitable(value):
            value = await value
        return bool(value)

    @staticmethod
    async def _invoke(callback: Callable[..., Any], *args: Any) -> Any:
        """Call sync/async callbacks while tolerating legacy 2-arg reducers."""

        try:
            signature = inspect.signature(callback)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            has_varargs = any(
                parameter.kind == parameter.VAR_POSITIONAL
                for parameter in signature.parameters.values()
            )
            call_args = args if has_varargs else args[: len(positional)]
        except (TypeError, ValueError):
            call_args = args
        value = callback(*call_args)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _exception_disposition(exc: BaseException) -> str:
        value = getattr(exc, "attempt_disposition", None)
        if value is None:
            value = getattr(exc, "disposition", None)
        if value is not None and str(value) in {
            "accepted_or_unknown",
            "accepted_unknown",
            "ambiguous",
        }:
            return "accepted_or_unknown"
        if exc.__class__.__name__ == "ProviderAttemptRecoveryRequired":
            return "accepted_or_unknown"
        if bool(getattr(exc, "hard_stop", False)) or bool(
            getattr(exc, "cost_stop", False)
        ):
            return "deferred"
        return "failed"

    @staticmethod
    def _completion_payload(value: Any) -> tuple[Any, dict[str, Any] | None, str | None, str | None, str | None]:
        """Extract an optional completion and result identity from a lane value."""

        completion: Any = None
        result_ref: str | None = None
        result_hash: str | None = None
        disposition: str | None = None
        candidate = value
        if isinstance(value, tuple) and len(value) == 2:
            first, second = value
            if isinstance(first, str):
                result_ref = first
            value = second
            candidate = value
        if isinstance(value, (LaneCompletion, CrossOwnerCompletion)):
            completion = value
            subject = getattr(value, "subject", None)
            if subject is not None:
                result_ref = subject.ref
                result_hash = subject.sha256
        elif isinstance(value, Mapping):
            completion = value.get("completion") or value.get("result")
            if isinstance(completion, (LaneCompletion, CrossOwnerCompletion)):
                subject = getattr(completion, "subject", None)
                if subject is not None:
                    result_ref = result_ref or subject.ref
                    result_hash = result_hash or subject.sha256
            result_ref = result_ref or value.get("result_ref")
            result_hash = result_hash or value.get("result_sha256") or value.get("result_hash")
            disposition = value.get("disposition")
        return candidate, (
            completion.model_dump(mode="json")
            if isinstance(completion, (LaneCompletion, CrossOwnerCompletion))
            else (dict(completion) if isinstance(completion, Mapping) else None)
        ), result_ref, result_hash, disposition

    def _store_for(self, run_id: str) -> TaskAttemptStore:
        if self.attempt_store is not None:
            if self.attempt_store.run_id != run_id:
                raise ValueError("attempt store belongs to another run")
            return self.attempt_store
        self.attempt_store = TaskAttemptStore(self.workspace, run_id)
        return self.attempt_store

    async def run(
        self,
        specs: Iterable[LaneTaskSpec],
        run_lane: Callable[[LaneTaskSpec], Any],
        on_terminal: Callable[..., Any] | None = None,
        hard_stop: Any = None,
    ) -> CohortRunResult:
        ordered = self._ordered_specs(specs)
        if not ordered:
            return CohortRunResult(
                terminals={},
                barrier_candidate={
                    "target_task_ids": [],
                    "completion_refs": {},
                    "completion_hashes": {},
                },
            )
        run_ids = {spec.run_id for spec in ordered if spec.run_id is not None}
        if self.run_id is not None:
            run_ids.add(self.run_id)
        if len(run_ids) > 1:
            raise ValueError("all-ready cohort requires one run id")
        run_id = next(iter(run_ids), "run")
        self.run_id = run_id
        store = self._store_for(run_id)

        # Recovery is fail-closed: only a complete result identity or an
        # accepted/unknown Provider marker suppresses a new invocation.
        terminals: dict[str, LaneAttemptRecord] = {}
        recovered: list[str] = []
        ready: list[LaneTaskSpec] = []
        recovered_payloads: dict[str, Any] = {}
        for spec in ordered:
            latest = store.load_latest(spec.task_id or spec.lane_id or "", include_payload=True)
            if latest is not None:
                previous, payload = latest
                if (
                    previous.status == "completed"
                    and previous.result_ref
                    and previous.result_sha256
                ) or previous.disposition == "accepted_or_unknown":
                    terminals[spec.task_id or spec.lane_id or ""] = previous
                    recovered.append(spec.task_id or spec.lane_id or "")
                    recovered_payloads[spec.task_id or spec.lane_id or ""] = payload
                    continue
            ready.append(spec)

        # Pre-claim every ready lane before creating workers.  This is a
        # durable record of intent and keeps immediate failures from changing
        # the admitted cohort.
        claims: dict[str, LaneAttemptRecord] = {}
        for spec in ready:
            task_id = spec.task_id or spec.lane_id or ""
            claim = LaneAttemptRecord(
                lane_id=spec.lane_id or task_id,
                task_id=task_id,
                task_attempt_id=f"{task_id}-attempt-{uuid4().hex}",
                lease_epoch=1,
                status="started",
                disposition="unknown",
            )
            store.append(claim)
            claims[task_id] = claim

        failures: dict[str, str] = {}
        deferred: list[str] = []
        candidates: dict[str, Any] = {}

        async def run_one(spec: LaneTaskSpec) -> tuple[str, LaneAttemptRecord, Any, Any]:
            task_id = spec.task_id or spec.lane_id or ""
            claim = claims[task_id]
            completion_value: Any = None
            try:
                if await self._stop_requested(hard_stop):
                    terminal = claim.model_copy(
                        update={
                            "status": "deferred",
                            "disposition": "deferred",
                            "finished_at_ns": time.time_ns(),
                        }
                    )
                else:
                    completion_value = await self._invoke(run_lane, spec)
                    if isinstance(completion_value, LaneAttemptRecord):
                        terminal = completion_value.model_copy(
                            update={
                                "task_id": task_id,
                                "lane_id": spec.lane_id or task_id,
                                "task_attempt_id": claim.task_attempt_id,
                                "started_at_ns": claim.started_at_ns,
                                "finished_at_ns": completion_value.finished_at_ns
                                or time.time_ns(),
                            }
                        )
                    else:
                        _candidate, _payload, result_ref, result_hash, disposition = self._completion_payload(
                            completion_value
                        )
                        if disposition in {"accepted_or_unknown", "accepted_unknown", "ambiguous"}:
                            terminal = claim.model_copy(
                                update={
                                    "status": "ambiguous",
                                    "disposition": "accepted_or_unknown",
                                    "finished_at_ns": time.time_ns(),
                                }
                            )
                        else:
                            terminal = claim.model_copy(
                                update={
                                    "status": "completed",
                                    "disposition": "completed",
                                    "result_ref": result_ref,
                                    "result_sha256": result_hash,
                                    "result_hash": result_hash,
                                    "finished_at_ns": time.time_ns(),
                                }
                            )
            except BaseException as exc:  # ordinary lane failures must drain siblings
                disposition = self._exception_disposition(exc)
                status = "deferred" if disposition == "deferred" else (
                    "ambiguous" if disposition == "accepted_or_unknown" else "failed"
                )
                terminal = claim.model_copy(
                    update={
                        "status": status,
                        "disposition": disposition,
                        "error": f"{exc.__class__.__name__}: {exc}",
                        "finished_at_ns": time.time_ns(),
                    }
                )
            _candidate, payload, result_ref, result_hash, _disposition = self._completion_payload(
                completion_value
            )
            # Preserve a typed record returned by run_lane while still binding
            # the immutable result identity to this attempt.
            if terminal.result_ref is None and result_ref is not None:
                terminal = terminal.model_copy(update={"result_ref": result_ref})
            if terminal.result_sha256 is None and result_hash is not None:
                terminal = terminal.model_copy(
                    update={"result_sha256": result_hash, "result_hash": result_hash}
                )
            store.append(terminal, payload=payload)
            return task_id, terminal, completion_value, payload

        # ``gather(..., return_exceptions=True)`` is intentional even though
        # run_one classifies expected exceptions: a reducer/callback bug must
        # not cancel already-started siblings.
        results = await asyncio.gather(
            *(asyncio.create_task(run_one(spec)) for spec in ready),
            return_exceptions=True,
        )
        for result in results:
            if isinstance(result, BaseException):
                # This should only be a programming error outside the lane's
                # normal classification.  Keep the cohort terminal and avoid
                # silently turning it into a successful barrier.
                task_id = f"internal-{len(failures) + 1}"
                failures[task_id] = f"{result.__class__.__name__}: {result}"
                continue
            task_id, terminal, completion_value, payload = result
            terminals[task_id] = terminal
            if terminal.status == "failed" or terminal.disposition == "failed":
                failures[task_id] = terminal.error or "lane failed"
            elif terminal.status == "deferred" or terminal.disposition == "deferred":
                deferred.append(task_id)
            elif terminal.status == "completed":
                if completion_value is not None:
                    candidates[task_id] = completion_value
                elif payload is not None:
                    candidates[task_id] = payload
            if on_terminal is not None:
                await self._invoke(on_terminal, ordered[[s.task_id for s in ordered].index(task_id)], terminal, completion_value)

        # Recovered records are terminal too, and callbacks are intentionally
        # invoked after all new workers drain to keep the reducer single-writer.
        for spec in ordered:
            task_id = spec.task_id or spec.lane_id or ""
            if task_id not in recovered:
                continue
            terminal = terminals[task_id]
            payload = recovered_payloads.get(task_id)
            if terminal.status == "completed" and payload is not None:
                candidates[task_id] = payload
            if on_terminal is not None:
                await self._invoke(on_terminal, spec, terminal, payload)

        # Only an all-completed cohort can produce an exact barrier candidate.
        # Accepted/unknown and failed/deferred terminals remain durable but
        # deliberately block promotion and must be reconciled by the caller.
        ordered_ids = [spec.task_id or spec.lane_id or "" for spec in ordered]
        if (
            not failures
            and not deferred
            and all(terminals.get(task_id, None) is not None for task_id in ordered_ids)
            and all(terminals[task_id].status == "completed" for task_id in ordered_ids)
        ):
            barrier_candidate: dict[str, Any] | None = {
                "target_task_ids": ordered_ids,
                "completion_refs": {
                    task_id: terminals[task_id].result_ref
                    for task_id in ordered_ids
                    if terminals[task_id].result_ref
                },
                "completion_hashes": {
                    task_id: terminals[task_id].result_sha256
                    for task_id in ordered_ids
                    if terminals[task_id].result_sha256
                },
                "terminal_attempt_ids": {
                    task_id: terminals[task_id].task_attempt_id for task_id in ordered_ids
                },
            }
        else:
            barrier_candidate = None
        return CohortRunResult(
            terminals=terminals,
            barrier_candidate=barrier_candidate,
            failures=failures,
            deferred=tuple(sorted(deferred)),
            recovered=tuple(sorted(recovered)),
        )


class CohortBarrier(_StrictModel):
    kind: Literal["cohort_barrier"] = "cohort_barrier"
    run_id: str
    # Module barriers use ``target_modules``; the generic all-ready reducer
    # uses ``target_tasks``.  Keeping both fields lets old checkpoints parse
    # unchanged while allowing arbitrary semantic task ids.
    target_modules: list[str] = Field(default_factory=list)
    target_tasks: list[str] = Field(default_factory=list)
    completion_refs: dict[str, str]
    completion_hashes: dict[str, str]
    target_revisions: dict[str, int] = Field(default_factory=dict)
    barrier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["full", "partial"] = "full"
    status: Literal["committed", "failed", "deferred"] = "committed"


class CrossOwnerCompletion(_StrictModel):
    kind: Literal["cross_owner_completion"] = "cross_owner_completion"
    lane_id: str
    run_id: str
    review_round: int = Field(ge=0)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    semantic_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    # Keep the revision explicit in the completion rather than inferring it
    # solely from a filename.  The optional default keeps older persisted
    # completions readable; active writers always populate it.
    subject_revision: int | None = Field(default=None, ge=0)
    owner_input: ArtifactRef | None = Field(
        default=None,
        description="Hash-bound CrossOwnerInput consumed by this owner reviewer.",
    )
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
        # Preserve v1 completion hashes for old artifacts that predate the
        # explicit subject revision field.  Active v2 completions include the
        # revision in their immutable identity.
        if self.subject_revision is None:
            deterministic.pop("subject_revision", None)
        if self.owner_input is None:
            deterministic.pop("owner_input", None)
        return _sha256_bytes(_canonical_json_bytes(deterministic))


class CrossOwnerBarrier(_StrictModel):
    kind: Literal["cross_owner_barrier"] = "cross_owner_barrier"
    run_id: str
    review_round: int = Field(ge=0)
    target_modules: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]]
    completion_refs: dict[str, str]
    completion_hashes: dict[str, str]
    # Exact subject revisions are part of the barrier identity.  Legacy
    # barriers may omit this map and remain parseable, but new barriers always
    # bind one revision per owner lane.
    completion_revisions: dict[str, int] = Field(default_factory=dict)
    barrier_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class WorkflowReducer:
    """Single-writer deterministic promotion of successful module lanes."""

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = _safe_component(run_id, field="run_id")

    @staticmethod
    def _task_id(value: LaneTaskSpec | str) -> str:
        task_id = value.task_id if isinstance(value, LaneTaskSpec) else value
        if not task_id:
            raise ValueError("terminal promotion requires a task id")
        return _safe_component(task_id, field="task_id")

    @staticmethod
    def _revision(value: LaneTaskSpec | Mapping[str, Any] | Any) -> int | None:
        if isinstance(value, LaneTaskSpec):
            return value.revision
        if isinstance(value, Mapping):
            revision = value.get("revision")
            return int(revision) if revision is not None else None
        revision = getattr(value, "revision", None)
        return int(revision) if revision is not None else None

    @staticmethod
    def _terminal_payload(value: Any) -> tuple[dict[str, Any], str | None, str | None, int | None]:
        """Normalize a completion/attempt value for immutable promotion."""

        if isinstance(value, BaseModel):
            payload = value.model_dump(mode="json")
        elif isinstance(value, Mapping):
            payload = dict(value)
        else:
            payload = {"value": value}
        revision = WorkflowReducer._revision(value)
        ref = payload.get("result_ref") or payload.get("ref")
        result_hash = (
            payload.get("result_sha256")
            or payload.get("result_hash")
            or payload.get("sha256")
        )
        if isinstance(payload.get("subject"), Mapping):
            subject = payload["subject"]
            ref = ref or subject.get("ref")
            result_hash = result_hash or subject.get("sha256")
        return payload, ref, result_hash, revision

    def promote_terminal(
        self,
        spec: LaneTaskSpec | str,
        terminal: Any,
        completion: Any | None = None,
        *,
        expected_revision: int | None = None,
    ) -> Any:
        """Promote one lane-private terminal through a single-writer fence.

        The canonical terminal path is immutable.  A retry with the same
        semantic identity is idempotent; a late attempt with an older revision
        or different bytes is rejected and can never overwrite the winner.
        """

        validate_bound_project_write_lease(self.workspace)
        task_id = self._task_id(spec)
        payload_value = completion if completion is not None else terminal
        payload, result_ref, result_hash, terminal_revision = self._terminal_payload(
            payload_value
        )
        spec_revision = self._revision(spec)
        if expected_revision is not None:
            if terminal_revision is not None and terminal_revision != expected_revision:
                raise ValueError("terminal revision does not match expected revision")
            if spec_revision is not None and spec_revision != expected_revision:
                raise ValueError("task spec revision does not match expected revision")
            terminal_revision = expected_revision
        elif terminal_revision is None:
            terminal_revision = spec_revision
        semantic_key = (
            spec.semantic_key
            if isinstance(spec, LaneTaskSpec)
            else payload.get("semantic_key")
        )
        promoted = {
            "task_id": task_id,
            "semantic_key": semantic_key,
            "revision": terminal_revision,
            "result_ref": result_ref,
            "result_sha256": result_hash,
            "terminal": payload,
        }
        promoted["terminal_sha256"] = _sha256_bytes(
            _canonical_json_bytes(promoted)
        )
        path = self.workspace / "Work" / "runs" / self.run_id / "lanes" / task_id / "terminal.json"
        with exclusive_file_lock(path.with_suffix(".lock")):
            if path.is_file():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError, TypeError) as exc:
                    raise RuntimeError(f"promoted terminal is unreadable: {path}") from exc
                if existing == promoted:
                    return terminal
                old_revision = existing.get("revision")
                if (
                    terminal_revision is not None
                    and old_revision is not None
                    and terminal_revision < old_revision
                ):
                    raise RuntimeError("late terminal attempt has an older revision")
                raise RuntimeError("promoted terminal already exists with different identity")
            atomic_write_json(path, promoted)
        return terminal

    def _barrier_completions(
        self,
        targets: list[str],
        completions: Any | None,
    ) -> tuple[dict[str, str], dict[str, str]]:
        refs: dict[str, str] = {}
        hashes: dict[str, str] = {}
        if isinstance(completions, Mapping) and (
            "completion_refs" in completions or "completion_hashes" in completions
        ):
            refs.update({str(k): str(v) for k, v in completions.get("completion_refs", {}).items()})
            hashes.update({str(k): str(v) for k, v in completions.get("completion_hashes", {}).items()})
        elif completions is not None:
            if isinstance(completions, Mapping):
                items = list(completions.items())
            else:
                items = list(completions)
            for key, value in items:
                task_id = str(key)
                payload, ref, result_hash, _revision = self._terminal_payload(value)
                if ref:
                    refs[task_id] = ref
                if result_hash:
                    hashes[task_id] = result_hash
                if not ref and payload.get("completion_ref"):
                    refs[task_id] = str(payload["completion_ref"])
        # A reducer may be called after promotions with no explicit completion
        # list.  Read each immutable terminal as the source of truth.
        for task_id in targets:
            if task_id in refs and task_id in hashes:
                continue
            path = (
                self.workspace
                / "Work"
                / "runs"
                / self.run_id
                / "lanes"
                / task_id
                / "terminal.json"
            )
            if path.is_file():
                raw = json.loads(path.read_text(encoding="utf-8"))
                refs.setdefault(task_id, raw.get("result_ref"))
                hashes.setdefault(task_id, raw.get("result_sha256"))
        if set(refs) != set(targets) or set(hashes) != set(targets):
            raise ValueError("exact barrier requires one verified completion per target")
        return refs, hashes

    def commit_exact_barrier(
        self,
        specs: Iterable[LaneTaskSpec | str],
        completions: Any | None = None,
        *,
        expected_revision: int | Mapping[str, int] | None = None,
        scope: Literal["full", "partial"] = "full",
    ) -> CohortBarrier:
        """Commit an exact all-ready barrier with one reducer writer."""

        validate_bound_project_write_lease(self.workspace)
        values = list(specs)
        targets = sorted({self._task_id(value) for value in values})
        if len(targets) != len(values):
            raise ValueError("exact barrier target set contains duplicates")
        target_revisions: dict[str, int] = {}
        for value in values:
            task_id = self._task_id(value)
            revision = self._revision(value)
            expected = (
                expected_revision.get(task_id)
                if isinstance(expected_revision, Mapping)
                else expected_revision
            )
            if expected is not None:
                if revision is not None and revision != int(expected):
                    raise ValueError("task spec revision does not match expected revision")
                revision = int(expected)
            if revision is not None:
                target_revisions[task_id] = revision
        refs, hashes = self._barrier_completions(targets, completions)
        module_targets = [task_id for task_id in targets if task_id in {"2.1", "2.2", "2.3", "2.4", "2.5"}]
        if len(module_targets) != len(targets):
            module_targets = []
        payload = {
            "run_id": self.run_id,
            "target_modules": module_targets,
            "target_tasks": targets,
            "completion_refs": {task_id: refs[task_id] for task_id in targets},
            "completion_hashes": {task_id: hashes[task_id] for task_id in targets},
            "target_revisions": target_revisions,
            "scope": scope,
            "status": "committed",
        }
        barrier = CohortBarrier(
            **payload,
            barrier_sha256=_sha256_bytes(_canonical_json_bytes(payload)),
        )
        path = self.workspace / "Work" / "runs" / self.run_id / "lanes" / "cohort-barrier.json"
        with exclusive_file_lock(path.with_suffix(".lock")):
            if path.is_file():
                try:
                    existing = CohortBarrier.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except Exception as exc:
                    raise RuntimeError(f"cohort barrier is unreadable: {path}") from exc
                if existing == barrier:
                    return existing
                raise RuntimeError("exact cohort barrier already exists with different identity")
            atomic_write_json(path, barrier.model_dump(mode="json"))
        return barrier

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
        revisions: dict[str, int] = {}
        for module_id in ordered_targets:
            completion = by_module[module_id][1]
            revision = completion.subject_revision
            if revision is None:
                # v1 completion fixtures did not carry a typed revision.  Their
                # canonical subject refs still use ``-rN.json``; recover that
                # value for a strict barrier without changing the old wire
                # shape of the completion itself.
                match = re.search(r"-r([0-9]+)\.json$", completion.subject.ref)
                if match is None:
                    raise ValueError(
                        "Cross owner completion subject must bind a revision"
                    )
                revision = int(match.group(1))
            revisions[module_id] = revision
        payload = {
            "run_id": self.run_id,
            "review_round": review_round,
            "target_modules": ordered_targets,
            "completion_refs": refs,
            "completion_hashes": hashes,
            "completion_revisions": revisions,
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
