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
        if record.run_id not in {"unknown", self.run_id}:
            raise ValueError("lane attempt belongs to another run")
        # Make the task identity explicit in the journal even when callers
        # supplied only the legacy lane_id spelling.
        if (
            record.task_id != task_id
            or record.lane_id is None
            or record.run_id != self.run_id
        ):
            record = record.model_copy(
                update={
                    "task_id": task_id,
                    "lane_id": record.lane_id or task_id,
                    "run_id": self.run_id,
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
    """Business identity for one recoverable lane.

    ``semantic_key``/``payload_hash`` and the preparation digests are retained
    as compatibility metadata for old checkpoints.  They are deliberately
    optional and are never needed to decide whether a result can be resumed;
    the active recovery contract is the run/stage/lane/revision tuple below.
    """
    lane_id: str | None = Field(default=None, min_length=1)
    run_id: str | None = Field(default=None, min_length=1)
    stage: str = Field(default="lane", min_length=1)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] | None = None
    semantic_key: str | None = None
    preparation_sha256: str | None = None
    collaboration_bundle_sha256: str | None = None
    task_id: str | None = Field(default=None, min_length=1)
    revision: int = Field(default=0, ge=0)
    priority: int = 0
    ordinal: int = Field(default=0, ge=0)
    payload_hash: str | None = None
    schema_version: str = "1"

    @model_validator(mode="after")
    def normalize_identity(self) -> "LaneTaskSpec":
        if self.lane_id is None and self.task_id is None:
            raise ValueError("LaneTaskSpec requires lane_id or task_id")
        if self.lane_id is None:
            self.lane_id = self.task_id
        if self.task_id is None:
            self.task_id = self.lane_id
        if self.run_id is not None:
            _safe_component(self.run_id, field="run_id")
        _safe_component(self.task_id or self.lane_id or "", field="task_id")
        return self


class LaneAttemptRecord(_StrictModel):
    """Append-only business-state record for one lane dispatch.

    The first seven fields (run/stage/lane/attempt/revision/status/result/error)
    are the active recovery contract. Provider-attempt forensics are separate
    from recovery state.
    """
    run_id: str = "unknown"
    stage: str = "lane"
    lane_id: str | None = None
    task_id: str | None = None
    attempt: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    task_attempt_id: str | None = None
    lease_epoch: int = Field(default=1, ge=1)
    started_at_ns: int = Field(default_factory=time.time_ns, ge=1)
    finished_at_ns: int | None = Field(default=None, ge=1)
    # ``started`` is useful for a journal pre-claim; callers may omit it when
    # constructing a record for a successful one-shot lane in tests.
    status: Literal[
        "started",
        "running",
        "completed",
        "failed",
        "blocked",
        "invalidated",
        "deferred",
    ] = "started"
    # The frozen API describes timestamps as one logical field.  Keep the
    # explicit nanosecond fields for existing checkpoints while accepting the
    # compact mapping as input.
    timestamps: dict[str, int] | None = None
    disposition: Literal[
        "completed",
        "failed",
        "deferred",
        "needs_input",
        "disputed",
        "escalate",
        "blocked",
        "invalidated",
        "unknown",
    ] | None = None
    result_ref: str | None = None
    # Hashes belong to the append-only attempt journal and are not recovery gates.
    result_sha256: str | None = None
    result_hash: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def normalize_attempt_identity(self) -> "LaneAttemptRecord":
        if self.task_id is None and self.lane_id is None:
            raise ValueError("lane attempt requires task_id or lane_id")
        if self.task_id is None:
            self.task_id = self.lane_id
        if self.lane_id is None:
            self.lane_id = self.task_id
        _safe_component(self.run_id, field="run_id")
        if not self.stage:
            raise ValueError("lane attempt stage is required")
        _safe_component(self.task_id or "", field="task_id")
        if self.task_attempt_id is None:
            self.task_attempt_id = (
                f"{self.task_id}-attempt-{self.attempt}-{uuid4().hex}"
            )
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
                "running": "unknown",
                "completed": "completed",
                "failed": "failed",
                "blocked": "blocked",
                "invalidated": "invalidated",
                "deferred": "deferred",
            }[self.status]
        return self


class LaneCompletion(_StrictModel):
    kind: Literal["lane_completion"] = "lane_completion"
    lane_id: str
    run_id: str
    stage: str = "lane"
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] | None = None
    attempt: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    status: Literal["completed", "failed", "blocked", "invalidated"] = "completed"
    result_ref: str | None = None
    error: str | None = None
    # Legacy completion metadata.  These are optional because active lane
    # recovery only needs the business-state fields above.
    semantic_key: str | None = None
    subject: ArtifactRef | str | None = None
    review_completion: ArtifactRef | str | None = None
    author_task_attempt_id: str | None = None
    reviewer_session_id: str | None = None
    lease_epoch: int = Field(default=1, ge=1)
    schema_version: str = "1"

    @model_validator(mode="after")
    def validate_business_identity(self) -> "LaneCompletion":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.lane_id, field="lane_id")
        if not self.stage:
            raise ValueError("lane completion stage is required")
        return self

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
    disposition: Literal[
        "needs_input",
        "disputed",
        "escalate",
        "failed",
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
    blocked: tuple[str, ...] = ()
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
        recovery_store: "RecoveryStateStore" | None = None,
        business_gate: Callable[..., Any] | None = None,
    ) -> None:
        self.workspace = Path(workspace or Path.cwd()).resolve()
        self.run_id = run_id
        self.attempt_store = attempt_store
        self.recovery_store = recovery_store
        self.business_gate = business_gate

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
            if isinstance(subject, ArtifactRef):
                result_ref = subject.ref
                result_hash = subject.sha256
            elif isinstance(subject, str):
                result_ref = subject
            result_ref = result_ref or getattr(value, "result_ref", None)
            result_hash = result_hash or getattr(value, "result_sha256", None)
        elif isinstance(value, Mapping):
            completion = value.get("completion") or value.get("result")
            if isinstance(completion, (LaneCompletion, CrossOwnerCompletion)):
                subject = getattr(completion, "subject", None)
                if isinstance(subject, ArtifactRef):
                    result_ref = result_ref or subject.ref
                    result_hash = result_hash or subject.sha256
                elif isinstance(subject, str):
                    result_ref = result_ref or subject
                result_ref = result_ref or getattr(completion, "result_ref", None)
                result_hash = result_hash or getattr(completion, "result_sha256", None)
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

    def _recovery_store_for(self, run_id: str) -> "RecoveryStateStore":
        if self.recovery_store is not None:
            if self.recovery_store.run_id != run_id:
                raise ValueError("recovery store belongs to another run")
            return self.recovery_store
        self.recovery_store = RecoveryStateStore(self.workspace, run_id)
        return self.recovery_store

    def _terminal_reusable(self, spec: LaneTaskSpec, record: LaneAttemptRecord) -> bool:
        recovery = self._recovery_store_for(self.run_id or spec.run_id or "run")
        state = LaneState(
            run_id=self.run_id or spec.run_id or "run",
            stage=spec.stage,
            lane_id=spec.task_id or spec.lane_id or "",
            attempt=record.attempt,
            revision=record.revision if record.revision else spec.revision,
            status="completed" if record.status == "completed" else "failed",
            result_ref=record.result_ref,
            error=record.error,
        )
        return recovery.result_is_reusable(
            state,
            expected_stage=spec.stage,
            expected_lane_id=spec.task_id or spec.lane_id or "",
            expected_revision=spec.revision,
            business_gate=self.business_gate,
        )

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

        recovery = self._recovery_store_for(run_id)
        # Recovery is business-state based: only a completed, readable,
        # ownership/revision-valid result suppresses a new invocation.
        # Only a valid completed business result suppresses dispatch; every
        # other prior attempt remains evidence while explicit resume starts fresh.
        terminals: dict[str, LaneAttemptRecord] = {}
        recovered: list[str] = []
        blocked_ids: list[str] = []
        ready: list[LaneTaskSpec] = []
        recovered_payloads: dict[str, Any] = {}
        previous_attempts: dict[str, LaneAttemptRecord] = {}
        for spec in ordered:
            task_id = spec.task_id or spec.lane_id or ""
            latest = store.load_latest(task_id, include_payload=True)
            # RecoveryStateStore is the business-state source of truth; the
            # attempt journal remains an append-only execution record.
            if latest is None:
                state = recovery.load_lane_state(spec.stage, task_id)
                if state is not None:
                    latest = (
                        LaneAttemptRecord(
                            run_id=state.run_id,
                            stage=state.stage,
                            lane_id=state.lane_id,
                            task_id=state.lane_id,
                            attempt=max(1, state.attempt),
                            revision=state.revision,
                            status=state.status,
                            disposition=(
                                state.status
                                if state.status in {"completed", "failed", "blocked", "deferred", "invalidated"}
                                else "unknown"
                            ),
                            result_ref=state.result_ref,
                            error=state.error,
                        ),
                        None,
                    )
            if latest is not None:
                previous, payload = latest
                previous_attempts[task_id] = previous
                if previous.status == "completed" and self._terminal_reusable(spec, previous):
                    terminals[task_id] = previous
                    recovered.append(spec.task_id or spec.lane_id or "")
                    recovered_payloads[task_id] = payload
                    continue
                # Entering this supervisor is already an explicit same-run
                # recovery action. Failed, blocked, deferred, or corrupt
                # completed records remain immutable evidence, but none of
                # them may suppress a fresh higher-numbered attempt.
                ready.append(spec)
                continue
            ready.append(spec)

        # Pre-claim every ready lane before creating workers.  This is a
        # durable record of intent and keeps immediate failures from changing
        # the admitted cohort.
        claims: dict[str, LaneAttemptRecord] = {}
        for spec in ready:
            task_id = spec.task_id or spec.lane_id or ""
            claim = LaneAttemptRecord(
                run_id=run_id,
                stage=spec.stage,
                lane_id=spec.lane_id or task_id,
                task_id=task_id,
                attempt=(previous_attempts[task_id].attempt + 1 if task_id in previous_attempts else 1),
                revision=spec.revision,
                task_attempt_id=f"{task_id}-attempt-{uuid4().hex}",
                lease_epoch=1,
                status="started",
                disposition="unknown",
            )
            store.append(claim)
            recovery.record_lane_attempt(claim)
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
                        _candidate, _payload, result_ref, result_hash, _disposition = self._completion_payload(
                            completion_value
                        )
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
                status = "deferred" if disposition == "deferred" else "failed"
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
            recovery.record_lane_attempt(terminal, payload=payload)
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
            and not blocked_ids
            and not deferred
            and all(terminals.get(task_id, None) is not None for task_id in ordered_ids)
            and all(terminals[task_id].status == "completed" for task_id in ordered_ids)
            and all(
                self._terminal_reusable(
                    spec,
                    terminals[spec.task_id or spec.lane_id or ""],
                )
                for spec in ordered
            )
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
            blocked=tuple(sorted(blocked_ids)),
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
    completion_refs: dict[str, str] = Field(default_factory=dict)
    # Legacy forensic hashes remain readable but are not required for an
    # active barrier or recovery decision.
    completion_hashes: dict[str, str] = Field(default_factory=dict)
    target_revisions: dict[str, int] = Field(default_factory=dict)
    barrier_sha256: str | None = None
    scope: Literal["full", "partial"] = "full"
    status: Literal["committed", "failed", "deferred"] = "committed"


class CrossOwnerCompletion(_StrictModel):
    kind: Literal["cross_owner_completion"] = "cross_owner_completion"
    lane_id: str
    run_id: str
    stage: str = "cross"
    review_round: int = Field(default=0, ge=0)
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] | None = None
    attempt: int = Field(default=1, ge=1)
    revision: int = Field(default=0, ge=0)
    status: Literal["completed", "failed", "blocked", "invalidated"] = "completed"
    result_ref: str | None = None
    error: str | None = None
    semantic_key: str | None = None
    # Keep the revision explicit in the completion rather than inferring it
    # solely from a filename.  The optional default keeps older persisted
    # completions readable; active writers always populate it.
    subject_revision: int | None = Field(default=None, ge=0)
    owner_input: ArtifactRef | None = Field(
        default=None,
        description="Hash-bound CrossOwnerInput consumed by this owner reviewer.",
    )
    initial_result: ArtifactRef | None = Field(
        default=None,
        description=(
            "Hash-bound initial Cross-owner finding/synthesis result. Active v2 "
            "pipeline completions always populate this field."
        ),
    )
    verdict_result: ArtifactRef | None = Field(
        default=None,
        description=(
            "Hash-bound same-owner recheck verdict. It is absent only when the "
            "initial owner result contained no findings."
        ),
    )
    subject: ArtifactRef | str | None = None
    local_review_completion: ArtifactRef | str | None = None
    author_task_attempt_id: str | None = None
    reviewer_session_id: str | None = None
    lease_epoch: int = Field(default=1, ge=1)
    schema_version: str = "1"

    @model_validator(mode="after")
    def validate_business_identity(self) -> "CrossOwnerCompletion":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.lane_id, field="lane_id")
        if not self.stage:
            raise ValueError("Cross owner completion stage is required")
        return self

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
        if self.initial_result is None:
            deterministic.pop("initial_result", None)
        if self.verdict_result is None:
            deterministic.pop("verdict_result", None)
        return _sha256_bytes(_canonical_json_bytes(deterministic))


class CrossOwnerBarrier(_StrictModel):
    kind: Literal["cross_owner_barrier"] = "cross_owner_barrier"
    run_id: str
    review_round: int = Field(default=0, ge=0)
    target_modules: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(default_factory=list)
    completion_refs: dict[str, str] = Field(default_factory=dict)
    completion_hashes: dict[str, str] = Field(default_factory=dict)
    # Exact subject revisions are part of the barrier identity.  Legacy
    # barriers may omit this map and remain parseable, but new barriers always
    # bind one revision per owner lane.
    completion_revisions: dict[str, int] = Field(default_factory=dict)
    barrier_sha256: str | None = None


# ---------------------------------------------------------------------------
# Simple business-state recovery records
# ---------------------------------------------------------------------------


class StageState(_StrictModel):
    """Durable state of one reporting stage.

    This is intentionally a small business record.  Content hashes, CAS
    handles, and provider attempt metadata belong to forensic journals and are
    not part of the recovery decision.
    """

    run_id: str
    stage: str
    revision: int = Field(default=0, ge=0)
    status: Literal[
        "pending",
        "running",
        "completed",
        "failed",
        "blocked",
        "invalidated",
        "rolled_back",
    ] = "pending"
    result_ref: str | None = None
    error: str | None = None
    updated_at_ns: int = Field(default_factory=time.time_ns, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "StageState":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.stage, field="stage")
        return self


class LaneState(_StrictModel):
    """Current business state for one lane (not a content identity)."""

    run_id: str
    stage: str
    lane_id: str
    attempt: int = Field(default=0, ge=0)
    revision: int = Field(default=0, ge=0)
    status: Literal[
        "pending",
        "running",
        "completed",
        "failed",
        "blocked",
        "invalidated",
        "deferred",
    ] = "pending"
    result_ref: str | None = None
    error: str | None = None
    updated_at_ns: int = Field(default_factory=time.time_ns, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "LaneState":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.stage, field="stage")
        _safe_component(self.lane_id, field="lane_id")
        return self


class AggregateState(_StrictModel):
    """Current and historical state of a stage aggregate/barrier."""

    run_id: str
    stage: str
    aggregate_id: str = "default"
    revision: int = Field(default=0, ge=0)
    status: Literal[
        "pending",
        "running",
        "completed",
        "failed",
        "blocked",
        "invalidated",
        "rolled_back",
    ] = "pending"
    result_ref: str | None = None
    error: str | None = None
    lane_ids: list[str] = Field(default_factory=list)
    prior_result_ref: str | None = None
    updated_at_ns: int = Field(default_factory=time.time_ns, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "AggregateState":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.stage, field="stage")
        _safe_component(self.aggregate_id, field="aggregate_id")
        for lane_id in self.lane_ids:
            _safe_component(lane_id, field="lane_id")
        return self


class RecoveryPlan(_StrictModel):
    """An explicit, auditable recovery action for a worker/coordinator."""

    plan_id: str = Field(default_factory=lambda: f"recovery-{uuid4().hex}")
    run_id: str
    stage: str
    action: Literal[
        "retry_failed_lanes",
        "retry_aggregate",
        "invalidate_lane",
        "rollback_aggregate",
    ]
    lane_ids: list[str] = Field(default_factory=list)
    aggregate_id: str | None = None
    target_revision: int | None = Field(default=None, ge=0)
    result_ref: str | None = None
    reason: str = ""
    status: Literal["planned", "applied", "cancelled"] = "planned"
    created_at_ns: int = Field(default_factory=time.time_ns, ge=1)

    @model_validator(mode="after")
    def validate_identity(self) -> "RecoveryPlan":
        _safe_component(self.run_id, field="run_id")
        _safe_component(self.stage, field="stage")
        for lane_id in self.lane_ids:
            _safe_component(lane_id, field="lane_id")
        if self.aggregate_id is not None:
            _safe_component(self.aggregate_id, field="aggregate_id")
        return self


class RecoveryStateStore:
    """Persist and query simple lane/stage/aggregate business state.

    The store only treats a completed lane as reusable when its referenced
    result file exists, contains valid JSON, belongs to this run/stage/lane,
    matches the expected revision (when supplied), and passes an optional
    business gate. Attempt-journal hashes are never compared here.
    """

    _TERMINAL_FAILURES = {
        "failed",
        "blocked",
        "invalidated",
        "deferred",
    }
    _LANE_TERMINAL = {
        "completed",
        "failed",
        "blocked",
        "invalidated",
        "deferred",
    }
    _LANE_ACTIVE = {"pending", "running"}

    def __init__(self, workspace: Path, run_id: str) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = _safe_component(run_id, field="run_id")
        self.root = self.workspace / "Work" / "runs" / self.run_id / "recovery"
        self.stage_root = self.root / "stages"
        self.lane_root = self.root / "lanes"
        self.aggregate_root = self.root / "aggregates"
        self.plan_root = self.root / "plans"

    @staticmethod
    def _stage_key(stage: str) -> str:
        return _safe_component(stage, field="stage")

    @staticmethod
    def _lane_key(lane_id: str) -> str:
        return _safe_component(lane_id, field="lane_id")

    @staticmethod
    def _aggregate_key(aggregate_id: str) -> str:
        return _safe_component(aggregate_id, field="aggregate_id")

    def _ensure_owned(self, run_id: str, stage: str) -> None:
        if run_id != self.run_id:
            raise ValueError("recovery record belongs to another run")
        self._stage_key(stage)

    def _result_path(self, result_ref: str) -> Path | None:
        if not result_ref:
            return None
        candidate = Path(result_ref)
        if candidate.is_absolute():
            try:
                candidate = candidate.resolve().relative_to(self.workspace)
            except ValueError:
                return None
        if ".." in candidate.parts or candidate.as_posix() != result_ref:
            return None
        return self.workspace / candidate

    def _read_result(self, result_ref: str | None) -> Any | None:
        path = self._result_path(result_ref or "")
        if path is None or not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    @staticmethod
    def _run_gate(
        gate: Callable[..., Any] | None,
        value: Any,
        record: LaneState,
    ) -> bool:
        if gate is None:
            return True
        try:
            signature = inspect.signature(gate)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            args = (value, record)[: len(positional)]
        except (TypeError, ValueError):
            args = (value, record)
        try:
            result = gate(*args)
        except Exception:
            return False
        return bool(result)

    @classmethod
    def _lane_transition_error(
        cls,
        current: LaneState | None,
        incoming: LaneState,
        *,
        allow_invalidate: bool = False,
    ) -> str | None:
        """Return a deterministic business-state conflict, if any.

        A lane attempt is append-only: a new dispatch must use a strictly
        larger attempt number.  The one legal same-attempt transition is the
        normal started/running -> terminal completion (or an explicit
        invalidation).  This prevents a late attempt=1 record from replacing
        a completed attempt=1 result while retaining normal lifecycle updates.
        """

        if current is None:
            return None
        if incoming.revision < current.revision:
            return (
                f"lane revision regressed from {current.revision} to {incoming.revision}"
            )
        if incoming.attempt < current.attempt:
            return (
                f"lane attempt regressed from {current.attempt} to {incoming.attempt}"
            )
        if incoming.attempt > current.attempt:
            return None
        if incoming.status == "invalidated" and allow_invalidate:
            return None
        if current.status in cls._LANE_TERMINAL:
            if (
                incoming.status == current.status
                and incoming.result_ref == current.result_ref
                and incoming.error == current.error
                and incoming.revision == current.revision
            ):
                return None
            return (
                f"lane attempt {incoming.attempt} is already terminal as {current.status}; "
                "a retry must use a larger attempt"
            )
        # A lifecycle update may advance an active attempt, but it may not
        # move it backwards (for example running -> started).
        rank = {"pending": 0, "started": 1, "running": 2}
        current_rank = rank.get(current.status, 0)
        incoming_rank = rank.get(incoming.status, 0)
        if incoming.status in cls._LANE_TERMINAL:
            return None
        if incoming_rank < current_rank:
            return (
                f"lane status regressed from {current.status} to {incoming.status} "
                f"within attempt {incoming.attempt}"
            )
        if incoming_rank == current_rank and incoming.status == current.status:
            return (
                f"duplicate lane status {incoming.status} for attempt {incoming.attempt}; "
                "use a larger attempt for a new dispatch"
            )
        return None

    def _assert_lane_transition(
        self,
        current: LaneState | None,
        incoming: LaneState,
        *,
        allow_invalidate: bool = False,
    ) -> None:
        error = self._lane_transition_error(
            current,
            incoming,
            allow_invalidate=allow_invalidate,
        )
        if error:
            raise ValueError(error)

    @staticmethod
    def _aggregate_business_view(state: AggregateState) -> dict[str, Any]:
        return state.model_dump(mode="json", exclude={"updated_at_ns"})

    def _assert_aggregate_transition(
        self,
        current: AggregateState | None,
        incoming: AggregateState,
    ) -> None:
        if current is None:
            return
        if incoming.revision < current.revision:
            raise ValueError(
                f"aggregate revision regressed from {current.revision} to {incoming.revision}"
            )
        if incoming.revision == current.revision:
            if self._aggregate_business_view(incoming) != self._aggregate_business_view(current):
                raise ValueError(
                    f"aggregate revision {incoming.revision} conflicts with the existing state"
                )

    def _validate_successful_aggregate(self, state: AggregateState) -> None:
        """Validate aggregate result and every declared lane at the barrier."""

        if state.status != "completed":
            return
        if not state.result_ref:
            raise ValueError("completed aggregate requires result_ref")
        payload = self._read_result(state.result_ref)
        if payload is None:
            raise ValueError("completed aggregate result is missing or unreadable")
        if isinstance(payload, Mapping):
            if payload.get("run_id") not in (None, self.run_id):
                raise ValueError("completed aggregate result belongs to another run")
            if payload.get("stage") not in (None, state.stage):
                raise ValueError("completed aggregate result belongs to another stage")
            if payload.get("status") in self._TERMINAL_FAILURES:
                raise ValueError("completed aggregate result reports a failed status")
        for lane_id in sorted(set(state.lane_ids)):
            lane = self.load_lane_state(state.stage, lane_id)
            if lane is None or not self.result_is_reusable(
                lane,
                expected_stage=state.stage,
                expected_lane_id=lane_id,
                expected_revision=lane.revision,
            ):
                raise ValueError(
                    f"completed aggregate requires a readable completed lane result: {lane_id}"
                )

    def result_is_reusable(
        self,
        record: LaneState | LaneAttemptRecord | LaneCompletion,
        *,
        expected_stage: str | None = None,
        expected_lane_id: str | None = None,
        expected_revision: int | None = None,
        business_gate: Callable[..., Any] | None = None,
    ) -> bool:
        """Return whether a completed lane result is safe to reuse."""

        status = getattr(record, "status", None)
        if status != "completed":
            return False
        run_id = getattr(record, "run_id", None)
        stage = getattr(record, "stage", None)
        lane_id = getattr(record, "lane_id", None)
        revision = getattr(record, "revision", None)
        result_ref = getattr(record, "result_ref", None)
        if run_id != self.run_id:
            return False
        if expected_stage is not None and stage != expected_stage:
            return False
        if expected_lane_id is not None and lane_id != expected_lane_id:
            return False
        if expected_revision is not None and revision != expected_revision:
            return False
        payload = self._read_result(result_ref)
        if payload is None:
            return False
        if isinstance(payload, Mapping):
            # Embedded business identity is optional for old artifacts, but
            # when present it must agree with the recovery record.
            if payload.get("run_id") not in (None, self.run_id):
                return False
            if stage is not None and payload.get("stage") not in (None, stage):
                return False
            payload_lane = payload.get("lane_id", payload.get("task_id"))
            if lane_id is not None and payload_lane not in (None, lane_id):
                return False
            if revision is not None and payload.get("revision") not in (None, revision):
                return False
            payload_status = payload.get("status")
            if payload_status in self._TERMINAL_FAILURES:
                return False
        state = LaneState(
            run_id=self.run_id,
            stage=stage or expected_stage or "lane",
            lane_id=lane_id or expected_lane_id or "lane",
            attempt=max(0, int(getattr(record, "attempt", 0))),
            revision=max(0, int(revision or 0)),
            status="completed",
            result_ref=result_ref,
        )
        return self._run_gate(business_gate, payload, state)

    def _stage_path(self, stage: str) -> Path:
        return self.stage_root / f"{self._stage_key(stage)}.json"

    def _lane_state_path(self, stage: str, lane_id: str) -> Path:
        return self.lane_root / self._stage_key(stage) / f"{self._lane_key(lane_id)}.json"

    def record_stage_state(self, state: StageState | Mapping[str, Any], **updates: Any) -> StageState:
        if not isinstance(state, StageState):
            state = StageState.model_validate(state)
        if updates:
            state = state.model_copy(update={**updates, "updated_at_ns": time.time_ns()})
        self._ensure_owned(state.run_id, state.stage)
        path = self._stage_path(state.stage)
        with exclusive_file_lock(path.with_suffix(".lock")):
            atomic_write_json(path, state.model_dump(mode="json"))
        return state

    def load_stage_state(self, stage: str) -> StageState | None:
        path = self._stage_path(stage)
        if not path.is_file():
            return None
        try:
            return StageState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            raise RuntimeError(f"stage state is unreadable: {path}")

    def _lane_from_any(
        self,
        record: LaneState | LaneAttemptRecord | LaneCompletion | Mapping[str, Any],
        *,
        stage: str | None = None,
        lane_id: str | None = None,
    ) -> LaneState:
        if isinstance(record, LaneState):
            state = record
        elif isinstance(record, LaneAttemptRecord):
            lane_status = (
                "running" if record.status in {"started", "running"} else record.status
            )
            state = LaneState(
                run_id=self.run_id if record.run_id == "unknown" else record.run_id,
                stage=stage or record.stage,
                lane_id=lane_id or record.lane_id or record.task_id or "",
                attempt=record.attempt,
                revision=record.revision,
                status=lane_status,
                result_ref=record.result_ref,
                error=record.error,
                updated_at_ns=record.finished_at_ns or record.started_at_ns,
            )
        elif isinstance(record, LaneCompletion):
            state = LaneState(
                run_id=record.run_id,
                stage=stage or record.stage,
                lane_id=lane_id or record.lane_id,
                attempt=record.attempt,
                revision=record.revision,
                status=record.status,
                result_ref=record.result_ref
                or (
                    record.subject.ref
                    if isinstance(record.subject, ArtifactRef)
                    else record.subject
                ),
                error=record.error,
            )
        else:
            state = LaneState.model_validate(
                {**record, **({"stage": stage} if stage is not None else {}), **({"lane_id": lane_id} if lane_id is not None else {})}
            )
        if state.run_id == "unknown":
            state = state.model_copy(update={"run_id": self.run_id})
        self._ensure_owned(state.run_id, state.stage)
        return state

    def record_lane_attempt(
        self,
        record: LaneState | LaneAttemptRecord | Mapping[str, Any],
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> LaneState:
        """Append one lane attempt and update its current business state."""

        if isinstance(record, LaneState):
            state = record
            attempt_record = LaneAttemptRecord(
                run_id=state.run_id,
                stage=state.stage,
                lane_id=state.lane_id,
                task_id=state.lane_id,
                attempt=max(1, state.attempt),
                revision=state.revision,
                status=state.status,
                result_ref=state.result_ref,
                error=state.error,
            )
        else:
            attempt_record = record if isinstance(record, LaneAttemptRecord) else LaneAttemptRecord.model_validate(record)
            state = self._lane_from_any(attempt_record)
        self._ensure_owned(state.run_id, state.stage)
        lane_dir = self.lane_root / self._stage_key(state.stage) / self._lane_key(state.lane_id) / "attempts"
        lane_dir.mkdir(parents=True, exist_ok=True)
        body: dict[str, Any] = {"record": attempt_record.model_dump(mode="json")}
        if payload is not None:
            body["payload"] = dict(payload)
        path = lane_dir / f"{attempt_record.attempt}-{time.time_ns()}-{uuid4().hex[:8]}.json"
        with exclusive_file_lock(lane_dir / ".append.lock"):
            atomic_write_json(path, body)
        self._save_lane_state(state)
        return state

    def _save_lane_state(
        self,
        state: LaneState,
        *,
        allow_invalidate: bool = False,
    ) -> LaneState:
        path = self._lane_state_path(state.stage, state.lane_id)
        with exclusive_file_lock(path.with_suffix(".lock")):
            current: LaneState | None = None
            if path.is_file():
                try:
                    current = LaneState.model_validate_json(path.read_text(encoding="utf-8"))
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"lane state is unreadable: {path}") from exc
            self._assert_lane_transition(
                current,
                state,
                allow_invalidate=allow_invalidate,
            )
            if current is not None and self._lane_transition_error(
                current,
                state,
                allow_invalidate=allow_invalidate,
            ) is None and current.attempt == state.attempt and current.status in self._LANE_TERMINAL:
                # Exact idempotent replay should not perturb the projection
                # timestamp or replace an immutable terminal state.
                if self._lane_business_view(current) == self._lane_business_view(state):
                    return current
            atomic_write_json(path, state.model_dump(mode="json"))
        return state

    @staticmethod
    def _lane_business_view(state: LaneState) -> dict[str, Any]:
        return state.model_dump(mode="json", exclude={"updated_at_ns"})

    def record_lane_completion(
        self,
        completion: LaneCompletion | LaneState | Mapping[str, Any],
        *,
        payload: Mapping[str, Any] | None = None,
    ) -> LaneState:
        """Persist a typed completion and project its business state."""

        state = self._lane_from_any(completion)
        if state.status != "completed":
            raise ValueError("lane completion must have status=completed")
        lane_dir = self.lane_root / self._stage_key(state.stage) / self._lane_key(state.lane_id) / "completions"
        lane_dir.mkdir(parents=True, exist_ok=True)
        body: dict[str, Any]
        if isinstance(completion, LaneCompletion):
            body = {"completion": completion.model_dump(mode="json")}
        else:
            body = {"completion": state.model_dump(mode="json")}
        if payload is not None:
            body["payload"] = dict(payload)
        path = lane_dir / f"r{state.revision}-a{state.attempt}-{time.time_ns()}-{uuid4().hex[:8]}.json"
        with exclusive_file_lock(lane_dir / ".append.lock"):
            atomic_write_json(path, body)
        self._save_lane_state(state)
        return state

    def load_lane_state(self, stage: str, lane_id: str) -> LaneState | None:
        path = self._lane_state_path(stage, lane_id)
        if not path.is_file():
            return None
        try:
            state = LaneState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"lane state is unreadable: {path}") from exc
        if state.run_id != self.run_id or state.stage != stage or state.lane_id != lane_id:
            raise RuntimeError(f"lane state ownership mismatch: {path}")
        return state

    def load_completed_lanes(
        self,
        stage: str,
        lane_ids: Iterable[str] | None = None,
        *,
        expected_revisions: Mapping[str, int] | None = None,
        business_gate: Callable[..., Any] | None = None,
    ) -> dict[str, LaneState]:
        """Load only completed lanes whose result files pass business gates."""

        stage = self._stage_key(stage)
        candidates = list(lane_ids) if lane_ids is not None else []
        stage_dir = self.lane_root / stage
        if not candidates and stage_dir.is_dir():
            candidates = [path.stem for path in stage_dir.glob("*.json")]
        completed: dict[str, LaneState] = {}
        for lane_id in sorted(set(candidates)):
            state = self.load_lane_state(stage, lane_id)
            if state is None:
                continue
            expected = expected_revisions.get(lane_id) if expected_revisions else None
            if self.result_is_reusable(
                state,
                expected_stage=stage,
                expected_lane_id=lane_id,
                expected_revision=expected,
                business_gate=business_gate,
            ):
                completed[lane_id] = state
        return completed

    def retry_lane_attempt(
        self,
        stage: str,
        lane_id: str,
        *,
        revision: int = 0,
        reason: str = "explicit retry",
    ) -> LaneAttemptRecord:
        """Record a new explicit dispatch attempt for a lane."""

        current = self.load_lane_state(stage, lane_id)
        attempt = (current.attempt + 1) if current is not None else 1
        record = LaneAttemptRecord(
            run_id=self.run_id,
            stage=stage,
            lane_id=lane_id,
            task_id=lane_id,
            attempt=attempt,
            revision=revision if current is None else max(revision, current.revision),
            status="started",
            error=reason,
        )
        self.record_lane_attempt(record)
        return record

    def retry_failed_lanes(
        self,
        stage: str,
        lane_ids: Iterable[str] | None = None,
        *,
        reason: str = "retry failed or missing lanes",
        expected_revisions: Mapping[str, int] | None = None,
    ) -> RecoveryPlan:
        """Create an explicit plan for failed/blocked/missing lane dispatches."""

        requested = list(lane_ids or [])
        if not requested:
            stage_dir = self.lane_root / self._stage_key(stage)
            requested = [path.stem for path in stage_dir.glob("*.json")] if stage_dir.is_dir() else []
        selected: list[str] = []
        for lane_id in sorted(set(requested)):
            state = self.load_lane_state(stage, lane_id)
            expected = expected_revisions.get(lane_id) if expected_revisions else None
            if state is None:
                selected.append(lane_id)
                continue
            if state.status in self._TERMINAL_FAILURES:
                selected.append(lane_id)
                continue
            if state.status == "completed" and not self.result_is_reusable(
                state,
                expected_stage=stage,
                expected_lane_id=lane_id,
                expected_revision=expected,
            ):
                selected.append(lane_id)
        plan = RecoveryPlan(
            run_id=self.run_id,
            stage=stage,
            action="retry_failed_lanes",
            lane_ids=selected,
            reason=reason,
        )
        self._persist_plan(plan)
        return plan

    def retry_lanes(
        self,
        stage: str,
        lane_ids: Iterable[str],
        runner: Callable[..., Any],
        *,
        revision: int = 0,
        reason: str = "explicit lane retry",
    ) -> Any:
        """Dispatch explicitly selected lanes and return lane results.

        ``runner`` may be synchronous or asynchronous and may accept either a
        lane id or ``(lane_id, attempt_record)``.  If called from an existing
        event loop the returned value is an awaitable; outside a loop it is
        executed before returning.  A completed callback result is recorded as
        a completion, while failed/blocked outcomes remain explicit attempt
        evidence for a subsequent plan.
        """

        ids = sorted(set(lane_ids))

        async def _run() -> dict[str, Any]:
            results: dict[str, Any] = {}
            for lane_id in ids:
                attempt = self.retry_lane_attempt(
                    stage,
                    lane_id,
                    revision=revision,
                    reason=reason,
                )
                try:
                    value = await self._invoke_recovery_callback(runner, lane_id, attempt)
                except BaseException as exc:
                    failed = attempt.model_copy(
                        update={
                            "status": "failed",
                            "disposition": "failed",
                            "error": f"{exc.__class__.__name__}: {exc}",
                            "finished_at_ns": time.time_ns(),
                        }
                    )
                    self.record_lane_attempt(failed)
                    results[lane_id] = failed
                    continue
                results[lane_id] = value
                state_or_record = self._callback_lane_record(
                    value,
                    stage=stage,
                    lane_id=lane_id,
                    attempt=attempt,
                    revision=max(revision, attempt.revision),
                )
                if state_or_record is not None:
                    if isinstance(state_or_record, LaneCompletion):
                        self.record_lane_completion(state_or_record)
                    elif isinstance(state_or_record, LaneAttemptRecord):
                        self.record_lane_attempt(state_or_record)
                    else:
                        self._save_lane_state(state_or_record)
            return results

        coroutine = _run()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        return coroutine

    @staticmethod
    async def _invoke_recovery_callback(callback: Callable[..., Any], lane_id: str, attempt: LaneAttemptRecord) -> Any:
        try:
            signature = inspect.signature(callback)
            positional = [
                parameter
                for parameter in signature.parameters.values()
                if parameter.kind
                in (parameter.POSITIONAL_ONLY, parameter.POSITIONAL_OR_KEYWORD)
            ]
            args = (lane_id, attempt)[: len(positional)]
        except (TypeError, ValueError):
            args = (lane_id, attempt)
        value = callback(*args)
        if inspect.isawaitable(value):
            return await value
        return value

    @staticmethod
    def _callback_lane_record(
        value: Any,
        *,
        stage: str,
        lane_id: str,
        attempt: LaneAttemptRecord,
        revision: int,
    ) -> LaneCompletion | LaneAttemptRecord | LaneState | None:
        if isinstance(value, LaneCompletion):
            return value.model_copy(
                update={
                    "run_id": attempt.run_id,
                    "stage": stage,
                    "lane_id": lane_id,
                    "attempt": max(value.attempt, attempt.attempt),
                    "revision": max(value.revision, revision),
                }
            )
        if isinstance(value, LaneAttemptRecord):
            return value.model_copy(
                update={
                    "run_id": attempt.run_id,
                    "stage": stage,
                    "lane_id": lane_id,
                    "task_id": lane_id,
                    "attempt": max(value.attempt, attempt.attempt),
                    "revision": max(value.revision, revision),
                    "task_attempt_id": value.task_attempt_id or attempt.task_attempt_id,
                    "finished_at_ns": value.finished_at_ns or time.time_ns(),
                }
            )
        if isinstance(value, Mapping):
            status = value.get("status")
            result_ref = value.get("result_ref")
            if status in {"failed", "blocked", "invalidated", "deferred"}:
                return attempt.model_copy(
                    update={
                        "status": status,
                        "disposition": status,
                        "result_ref": result_ref,
                        "error": value.get("error"),
                        "finished_at_ns": time.time_ns(),
                    }
                )
            if status == "completed" or result_ref:
                return LaneCompletion(
                    lane_id=lane_id,
                    run_id=attempt.run_id,
                    stage=stage,
                    attempt=attempt.attempt,
                    revision=revision,
                    status="completed",
                    result_ref=str(result_ref) if result_ref else None,
                )
        return None

    def record_aggregate(
        self,
        aggregate: AggregateState | Mapping[str, Any],
    ) -> AggregateState:
        state = aggregate if isinstance(aggregate, AggregateState) else AggregateState.model_validate(aggregate)
        self._ensure_owned(state.run_id, state.stage)
        # Validate success before touching the projection.  Reducer failures
        # therefore leave the last successful aggregate intact and are safe to
        # retry without replaying any lane.
        self._validate_successful_aggregate(state)
        aggregate_dir = self.aggregate_root / self._stage_key(state.stage) / self._aggregate_key(state.aggregate_id)
        history = aggregate_dir / "history"
        history.mkdir(parents=True, exist_ok=True)
        current = aggregate_dir / "current.json"
        with exclusive_file_lock(current.with_suffix(".lock")):
            current_state: AggregateState | None = None
            if current.is_file():
                try:
                    current_state = AggregateState.model_validate_json(
                        current.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError) as exc:
                    raise RuntimeError(f"aggregate state is unreadable: {current}") from exc
            self._assert_aggregate_transition(current_state, state)
            if current_state is not None and current_state.revision == state.revision:
                # Idempotent reducer replay is a no-op; never rewrite the
                # current business state with a different timestamp.
                return current_state
            atomic_write_json(current, state.model_dump(mode="json"))
            atomic_write_json(
                history / f"r{state.revision}-{state.updated_at_ns}-{uuid4().hex[:8]}.json",
                state.model_dump(mode="json"),
            )
        return state

    def aggregate(self, stage: str, reducer: Callable[..., Any], *, aggregate_id: str = "default") -> Any:
        """Reduce currently completed lanes and persist an aggregate result.

        The reducer receives ``dict[lane_id, LaneState]``.  It may return an
        ``AggregateState``/mapping, or an arbitrary result payload containing
        ``result_ref``; asynchronous reducers are supported with the same
        outside/inside event-loop behavior as :meth:`retry_lanes`.
        """

        async def _run() -> Any:
            lanes = self.load_completed_lanes(stage)
            value = await self._invoke_recovery_callback(reducer, lanes, LaneAttemptRecord(
                run_id=self.run_id,
                stage=stage,
                lane_id="aggregate",
                task_id="aggregate",
                attempt=1,
                revision=0,
                status="completed",
            ))
            if isinstance(value, AggregateState):
                state = value
            elif isinstance(value, Mapping):
                state = AggregateState.model_validate(
                    {
                        "run_id": self.run_id,
                        "stage": stage,
                        "aggregate_id": aggregate_id,
                        **dict(value),
                    }
                )
            else:
                state = AggregateState(
                    run_id=self.run_id,
                    stage=stage,
                    aggregate_id=aggregate_id,
                    status="completed",
                    result_ref=str(value) if isinstance(value, str) else None,
                    lane_ids=sorted(lanes),
                )
            if state.run_id != self.run_id or state.stage != stage:
                raise ValueError("aggregate reducer returned state for another run/stage")
            if not state.lane_ids:
                state = state.model_copy(update={"lane_ids": sorted(lanes)})
            self.record_aggregate(state)
            return state

        coroutine = _run()
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(coroutine)
        return coroutine

    def load_aggregate(self, stage: str, aggregate_id: str = "default") -> AggregateState | None:
        path = self.aggregate_root / self._stage_key(stage) / self._aggregate_key(aggregate_id) / "current.json"
        if not path.is_file():
            return None
        try:
            return AggregateState.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"aggregate state is unreadable: {path}") from exc

    def load_previous_successful_aggregate(
        self,
        stage: str,
        aggregate_id: str = "default",
        *,
        before_revision: int | None = None,
        business_gate: Callable[..., Any] | None = None,
    ) -> AggregateState | None:
        history = self.aggregate_root / self._stage_key(stage) / self._aggregate_key(aggregate_id) / "history"
        if not history.is_dir():
            return None
        states: list[AggregateState] = []
        for path in history.glob("*.json"):
            try:
                state = AggregateState.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if state.status != "completed" or (before_revision is not None and state.revision >= before_revision):
                continue
            payload = self._read_result(state.result_ref)
            if payload is None:
                continue
            if isinstance(payload, Mapping):
                if payload.get("run_id") not in (None, self.run_id):
                    continue
                if payload.get("stage") not in (None, stage):
                    continue
                if payload.get("status") in self._TERMINAL_FAILURES:
                    continue
                if payload.get("revision") not in (None, state.revision):
                    continue
            states.append(state)
        if not states:
            return None
        states.sort(key=lambda item: (item.revision, item.updated_at_ns))
        candidate = states[-1]
        if business_gate is not None and not self._run_gate(business_gate, self._read_result(candidate.result_ref), LaneState(
            run_id=self.run_id,
            stage=stage,
            lane_id=aggregate_id,
            attempt=0,
            revision=candidate.revision,
            status="completed",
            result_ref=candidate.result_ref,
        )):
            return None
        return candidate

    def retry_aggregate(
        self,
        stage: str,
        aggregate_id: str = "default",
        *,
        reason: str = "explicit aggregate retry",
    ) -> RecoveryPlan:
        plan = RecoveryPlan(
            run_id=self.run_id,
            stage=stage,
            action="retry_aggregate",
            aggregate_id=aggregate_id,
            reason=reason,
        )
        self._persist_plan(plan)
        return plan

    def load_recovery_plans(
        self,
        *,
        stage: str | None = None,
        action: str | None = None,
    ) -> list[RecoveryPlan]:
        """Read durable recovery decisions without workflow-state projections."""

        if not self.plan_root.is_dir():
            return []
        plans: list[RecoveryPlan] = []
        for path in self.plan_root.glob("*.json"):
            try:
                plan = RecoveryPlan.model_validate_json(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if stage is not None and plan.stage != stage:
                continue
            if action is not None and plan.action != action:
                continue
            plans.append(plan)
        return sorted(plans, key=lambda item: (item.created_at_ns, item.plan_id))

    def recover_aggregate_failure(
        self,
        stage: str,
        lane_ids: Iterable[str],
        *,
        previous_stage: str | None,
        reason: str,
    ) -> RecoveryPlan:
        """Retry the reducer once, then restart from the prior stage boundary."""

        current = self.load_aggregate(stage)
        prior_retries = self.load_recovery_plans(
            stage=stage,
            action="retry_aggregate",
        )
        if current is not None and current.status == "completed":
            # A success after an older retry closes that incident.  Only a
            # retry recorded after the current aggregate counts as the first
            # failure of this reducer attempt.
            prior_retries = [
                plan
                for plan in prior_retries
                if plan.created_at_ns > current.updated_at_ns
            ]
        if not prior_retries or previous_stage is None:
            return self.retry_aggregate(stage, reason=reason)
        boundary = self.load_aggregate(previous_stage)
        if boundary is None or boundary.status != "completed":
            raise ValueError(
                f"no completed previous aggregate boundary for {stage}: {previous_stage}"
            )
        self._validate_successful_aggregate(boundary)
        ids = sorted(set(lane_ids))
        self.invalidate_lanes(
            stage,
            ids,
            reason=f"rollback to {previous_stage} after repeated aggregate failure",
        )
        plan = RecoveryPlan(
            run_id=self.run_id,
            stage=stage,
            action="rollback_aggregate",
            lane_ids=ids,
            aggregate_id=previous_stage,
            target_revision=boundary.revision,
            result_ref=boundary.result_ref,
            reason=reason,
            status="applied",
        )
        return self._persist_plan(plan)

    def invalidate_lanes(
        self,
        stage: str,
        lane_ids: Iterable[str],
        *,
        reason: str = "lane invalidated",
    ) -> RecoveryPlan:
        ids = sorted(set(lane_ids))
        for lane_id in ids:
            current = self.load_lane_state(stage, lane_id)
            state = current or LaneState(run_id=self.run_id, stage=stage, lane_id=lane_id)
            self._save_lane_state(
                state.model_copy(
                    update={"status": "invalidated", "error": reason, "updated_at_ns": time.time_ns()}
                ),
                allow_invalidate=True,
            )
        plan = RecoveryPlan(
            run_id=self.run_id,
            stage=stage,
            action="invalidate_lane",
            lane_ids=ids,
            reason=reason,
        )
        self._persist_plan(plan)
        return plan

    # Singular spelling is convenient for workflow workers and preserves the
    # explicit plural operation above for batch invalidation.
    def invalidate_lane(self, stage: str, lane_id: str, *, reason: str = "lane invalidated") -> RecoveryPlan:
        return self.invalidate_lanes(stage, [lane_id], reason=reason)

    def rollback_aggregate(
        self,
        stage: str,
        aggregate_id: str = "default",
        *,
        before_revision: int | None = None,
        reason: str = "rollback to prior successful aggregate",
    ) -> RecoveryPlan:
        current = self.load_aggregate(stage, aggregate_id)
        if before_revision is None and current is not None:
            before_revision = current.revision
        prior = self.load_previous_successful_aggregate(
            stage,
            aggregate_id,
            before_revision=before_revision,
        )
        if prior is None:
            raise ValueError("no prior successful aggregate is available")
        plan = RecoveryPlan(
            run_id=self.run_id,
            stage=stage,
            action="rollback_aggregate",
            aggregate_id=aggregate_id,
            target_revision=prior.revision,
            result_ref=prior.result_ref,
            reason=reason,
        )
        self._persist_plan(plan)
        return plan

    def rollback_to_aggregate(
        self,
        stage: str,
        aggregate_id: str = "default",
        *,
        before_revision: int | None = None,
    ) -> AggregateState:
        """Restore the current aggregate projection to its prior success.

        Historical records are never edited.  A new current record points at
        the prior successful result, making the rollback an ordinary durable
        state transition that can itself be audited and recovered.
        """

        current = self.load_aggregate(stage, aggregate_id)
        if before_revision is None and current is not None:
            before_revision = current.revision
        prior = self.load_previous_successful_aggregate(
            stage,
            aggregate_id,
            before_revision=before_revision,
        )
        if prior is None:
            raise ValueError("no prior successful aggregate is available")
        next_revision = (current.revision + 1) if current is not None else prior.revision + 1
        rolled = prior.model_copy(
            update={
                "revision": next_revision,
                "status": "completed",
                "prior_result_ref": current.result_ref if current is not None else None,
                "updated_at_ns": time.time_ns(),
            }
        )
        self.record_aggregate(rolled)
        return rolled

    # Explicit aliases used by workers that call the operation by its intent.
    rollback_to_previous_successful_aggregate = rollback_aggregate

    def _persist_plan(self, plan: RecoveryPlan) -> RecoveryPlan:
        self.plan_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(self.plan_root / f"{plan.plan_id}.json", plan.model_dump(mode="json"))
        return plan


# Short aliases make the state contract discoverable without coupling workers
# to the historical ``TaskAttemptStore`` naming.
LaneAttempt = LaneAttemptRecord
LaneCompletionRecord = LaneCompletion
RecoveryStore = RecoveryStateStore
LaneRecoveryStore = RecoveryStateStore


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
        # A simple business terminal must point at an existing, parseable
        # result.  Legacy semantic-key completions remain readable for old
        # checkpoints, but their hashes are not consulted by this active path.
        legacy_completion = payload.get("semantic_key") is not None
        status = payload.get("status", "completed")
        payload_run = payload.get("run_id")
        payload_lane = payload.get("lane_id", payload.get("task_id"))
        if payload_run not in (None, self.run_id):
            raise ValueError("terminal run ownership mismatch")
        if payload_lane not in (None, task_id):
            raise ValueError("terminal lane ownership mismatch")
        if status != "completed":
            raise ValueError("only completed lane terminals can be promoted")
        if not legacy_completion:
            if not result_ref:
                raise ValueError("completed terminal requires result_ref")
            result_path = RecoveryStateStore(self.workspace, self.run_id)._result_path(result_ref)
            if result_path is None or not result_path.is_file():
                raise ValueError("completed terminal result file is missing")
            try:
                result_payload = json.loads(result_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError) as exc:
                raise ValueError("completed terminal result is not valid JSON") from exc
            if isinstance(result_payload, Mapping):
                if result_payload.get("run_id") not in (None, self.run_id):
                    raise ValueError("completed result run ownership mismatch")
                if result_payload.get("lane_id", result_payload.get("task_id")) not in (None, task_id):
                    raise ValueError("completed result lane ownership mismatch")
                if terminal_revision is not None and result_payload.get("revision") not in (None, terminal_revision):
                    raise ValueError("completed result revision mismatch")
        promoted = {
            "run_id": self.run_id,
            "task_id": task_id,
            "lane_id": payload_lane or task_id,
            "stage": payload.get("stage", "lane"),
            "revision": terminal_revision,
            "status": "completed",
            "result_ref": result_ref,
            "terminal": payload,
        }
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
            if task_id in refs:
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
                raw_ref = raw.get("result_ref")
                raw_hash = raw.get("result_sha256")
                if raw_ref:
                    refs.setdefault(task_id, str(raw_ref))
                if raw_hash:
                    hashes.setdefault(task_id, str(raw_hash))
        if set(refs) != set(targets):
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
