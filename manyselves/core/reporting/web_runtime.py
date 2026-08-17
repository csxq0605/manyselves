"""Authorized Web MVP over the headless reporting runtime.

The module has no framework or Qt dependency. ``ReportingASGIApp`` implements
the small ASGI surface directly so server deployments may choose any ASGI host.
"""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import os
import re
import tempfile
import time
import uuid
from pathlib import Path, PurePosixPath
from typing import Any, Literal

from pydantic import Field

from .agentic_models import StrictModel
from .distributed_runtime import LocalEventStore
from .headless_runtime import HeadlessReportingRuntime
from .models import (
    REPORT_MODULE_IDS,
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)
from .parallel_runtime import atomic_write_json, exclusive_file_lock
from .production_runtime import ProductionPolicy, SecurityAuditLog

Role = Literal["viewer", "editor", "owner"]


class AuthenticatedUser(StrictModel):
    user_id: str = Field(min_length=1)


class LocalAuthorizationService:
    """Opaque local tokens plus durable per-project membership."""

    def __init__(self, state_root: Path) -> None:
        self.root = Path(state_root).resolve() / "authorization"
        self.path = self.root / "principals.json"
        self.lock_path = self.root / "principals.lock"

    @staticmethod
    def _token_hash(token: str) -> str:
        if len(token) < 16:
            raise ValueError("access token must contain at least 16 characters")
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _load_unlocked(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"schema_version": 1, "users": {}}
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if payload.get("schema_version") != 1:
            raise ValueError("unsupported authorization store version")
        return payload

    def bootstrap_member(
        self,
        *,
        user_id: str,
        token: str,
        project_id: str,
        role: Role = "owner",
    ) -> None:
        if not user_id.strip() or not project_id.strip():
            raise ValueError("user and project identity are required")
        self.root.mkdir(parents=True, exist_ok=True)
        with exclusive_file_lock(self.lock_path):
            payload = self._load_unlocked()
            users = payload.setdefault("users", {})
            existing = users.get(user_id)
            record = {
                "token_sha256": self._token_hash(token),
                "projects": {**(existing or {}).get("projects", {}), project_id: role},
            }
            if existing is not None and existing.get("token_sha256") != record["token_sha256"]:
                raise ValueError("user token identity is immutable")
            users[user_id] = record
            atomic_write_json(self.path, payload)

    def authenticate(self, token: str) -> AuthenticatedUser:
        digest = self._token_hash(token)
        with exclusive_file_lock(self.lock_path):
            payload = self._load_unlocked()
        matches = [
            user_id
            for user_id, record in payload.get("users", {}).items()
            if record.get("token_sha256") == digest
        ]
        if len(matches) != 1:
            raise PermissionError("invalid access token")
        return AuthenticatedUser(user_id=matches[0])

    def require_project(
        self,
        user: AuthenticatedUser,
        project_id: str,
        *,
        write: bool = False,
    ) -> Role:
        with exclusive_file_lock(self.lock_path):
            payload = self._load_unlocked()
        role = (
            payload.get("users", {})
            .get(user.user_id, {})
            .get("projects", {})
            .get(project_id)
        )
        if role not in {"viewer", "editor", "owner"}:
            raise PermissionError("user is not a project member")
        if write and role == "viewer":
            raise PermissionError("project membership is read-only")
        return role


class PublicProjectAccessPolicy:
    READ_ROOTS = ("Inputs", "Knowledge", "Templates", "Outputs/Modules", "Outputs/Reports")
    WRITE_ROOTS = ("Inputs", "Knowledge", "Templates")

    def __init__(self, workspace: Path) -> None:
        self.workspace = Path(workspace).resolve()

    def resolve(self, logical_ref: str, *, write: bool = False) -> Path:
        pure = PurePosixPath(logical_ref)
        if pure.is_absolute() or ".." in pure.parts or pure.as_posix() != logical_ref:
            raise PermissionError("file path is not a canonical project reference")
        allowed = self.WRITE_ROOTS if write else self.READ_ROOTS
        if not any(
            logical_ref == root or logical_ref.startswith(root + "/")
            for root in allowed
        ):
            raise PermissionError("internal reporting files are not public")
        target = self.workspace.joinpath(*pure.parts)
        if not target.absolute().is_relative_to(self.workspace):
            raise PermissionError("file path escapes project")
        if target.exists():
            resolved = target.resolve()
            controlled_cas = (
                not write
                and logical_ref.startswith("Outputs/")
                and resolved.is_relative_to(
                    (self.workspace / "Work/content/sha256").resolve()
                )
            )
            if (target.is_symlink() and not controlled_cas) or not resolved.is_relative_to(
                self.workspace
            ):
                raise PermissionError("public file path cannot follow a symlink")
        return target


class LocalConversationStore:
    def __init__(self, state_root: Path, project_id: str) -> None:
        self.root = Path(state_root).resolve() / "projects" / project_id / "conversations"
        self.lock_path = self.root / "conversations.lock"

    def create(self, user_id: str) -> dict[str, str]:
        session_id = f"session-{uuid.uuid4().hex}"
        payload = {
            "schema_version": 1,
            "session_id": session_id,
            "owner_user_id": user_id,
            "created_at_ns": time.time_ns(),
            "messages": [],
        }
        with exclusive_file_lock(self.lock_path):
            atomic_write_json(self.root / f"{session_id}.json", payload)
        return {"session_id": session_id, "owner_user_id": user_id}

    def read(self, session_id: str, user_id: str) -> dict[str, Any]:
        if not session_id or Path(session_id).name != session_id:
            raise ValueError("session id is invalid")
        path = self.root / f"{session_id}.json"
        if not path.is_file():
            raise FileNotFoundError("conversation does not exist")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("owner_user_id") != user_id:
            raise PermissionError("conversation belongs to another user")
        return payload

    def append(
        self,
        session_id: str,
        user_id: str,
        *,
        role: Literal["user", "assistant"],
        content: str,
    ) -> dict[str, Any]:
        if not content.strip():
            raise ValueError("conversation message cannot be empty")
        with exclusive_file_lock(self.lock_path):
            payload = self.read(session_id, user_id)
            message = {
                "message_id": f"message-{uuid.uuid4().hex}",
                "role": role,
                "content": content,
                "created_at_ns": time.time_ns(),
            }
            payload.setdefault("messages", []).append(message)
            atomic_write_json(self.root / f"{session_id}.json", payload)
        return message


class ReportingApi:
    """Application service for files, progress, Main conversation, and commands."""

    def __init__(
        self,
        runtime: HeadlessReportingRuntime,
        authorization: LocalAuthorizationService,
        *,
        production_policy: ProductionPolicy | None = None,
        audit_log: SecurityAuditLog | None = None,
    ) -> None:
        self.runtime = runtime
        self.authorization = authorization
        self.project_id = runtime.jobs.project_id
        self.production_policy = production_policy
        self.audit_log = audit_log
        self.files = PublicProjectAccessPolicy(runtime.paths.project_storage_root)
        self.conversations = LocalConversationStore(
            runtime.paths.service_state_root, self.project_id
        )

    def _user(self, token: str, project_id: str, *, write: bool = False):
        if project_id != self.project_id:
            raise FileNotFoundError("unknown project")
        user = self.authorization.authenticate(token)
        self.authorization.require_project(user, project_id, write=write)
        return user

    def _audit(
        self,
        event_type: str,
        user: AuthenticatedUser,
        details: dict[str, Any],
    ) -> None:
        if self.audit_log is not None:
            self.audit_log.append(
                event_type,
                actor_id=user.user_id,
                project_id=self.project_id,
                details=details,
            )

    def create_run(self, token: str, project_id: str, payload: dict) -> dict:
        user = self._user(token, project_id, write=True)
        job = self.runtime.submit(ReportRequest.model_validate(payload))
        self._audit("run.created", user, {"run_id": job.run_id})
        return {"run_id": job.run_id, "status": job.status}

    def create_revision(self, token: str, project_id: str, payload: dict) -> dict:
        user = self._user(token, project_id, write=True)
        job = self.runtime.submit_revision(RevisionRequest.model_validate(payload))
        self._audit("revision.created", user, {"run_id": job.run_id})
        return {"run_id": job.run_id, "status": job.status}

    def resume_run(self, token: str, project_id: str, run_id: str) -> dict:
        user = self._user(token, project_id, write=True)
        job = self.runtime.resume(run_id)
        self._audit("run.resumed", user, {"run_id": run_id})
        return {"run_id": job.run_id, "status": job.status}

    def cancel_run(self, token: str, project_id: str, run_id: str) -> dict:
        user = self._user(token, project_id, write=True)
        job = self.runtime.jobs.request_cancel(run_id)
        self._audit("run.cancel_requested", user, {"run_id": run_id})
        return {
            "run_id": job.run_id,
            "status": job.status,
            "cancel_requested": job.cancel_requested,
        }

    def resolve_evidence_decision(
        self,
        token: str,
        project_id: str,
        run_id: str,
        payload: dict[str, Any],
    ) -> dict:
        user = self._user(token, project_id, write=True)
        decision_id = str(payload.get("decision_id") or "")
        action = str(payload.get("action") or "")
        decision = self.runtime.service.decisions.load(decision_id)
        if decision.run_id != run_id or action not in decision.allowed_actions:
            raise ValueError("evidence decision does not match run or allowed action")
        if decision.status == "resolved" and decision.selected_action != action:
            raise ValueError("evidence decision was already resolved differently")
        decision = (
            decision
            if decision.status == "resolved"
            else self.runtime.service.decisions.resolve(decision_id, action)
        )
        self.runtime.service.store.write_json(
            f"Work/runs/{run_id}/evidence-choice.json",
            decision.model_dump(mode="json"),
        )
        if action == "stop":
            job = self.runtime.jobs.stop_waiting(
                run_id, reason="user stopped at evidence decision"
            )
            self._audit("run.decision", user, {"run_id": run_id, "action": action})
            return {"run_id": run_id, "status": job.status}
        request_path = (
            self.runtime.paths.project_storage_root
            / f"Work/runs/{run_id}/request.json"
        )
        request = ReportRequest.model_validate_json(
            request_path.read_text(encoding="utf-8")
        )
        supplements = [
            UserSupplement.model_validate(item)
            for item in payload.get("supplements", [])
        ]
        if supplements and action != "supplement":
            raise ValueError("supplements require action=supplement")
        resumed = ReportRequest.model_validate(
            {
                **request.model_dump(mode="json"),
                "missing_evidence_policy": (
                    "ask" if action == "supplement" else action
                ),
                "user_supplements": [
                    *request.user_supplements,
                    *supplements,
                ],
            }
        )
        self.runtime.service.store.write_json(
            f"Work/runs/{run_id}/request.json",
            resumed.model_dump(mode="json"),
        )
        job = self.runtime.jobs.requeue(run_id)
        self._audit("run.decision", user, {"run_id": run_id, "action": action})
        return {"run_id": run_id, "status": job.status}

    @staticmethod
    def _module_id(event) -> str | None:
        candidate = str(event.payload.get("module_id") or "")
        if candidate in REPORT_MODULE_IDS:
            return candidate
        for module_id in REPORT_MODULE_IDS:
            if re.search(
                rf"(?<![\d.]){re.escape(module_id)}(?![\d.])",
                str(event.task_id or ""),
            ):
                return module_id
        return None

    @staticmethod
    def _task_phase(task_id: str | None) -> str:
        value = str(task_id or "").replace("_", "-")
        if "cross" in value:
            return "cross_review"
        if "chief" in value:
            return "chief_edit"
        if "final" in value:
            return "final_review"
        if "revision" in value:
            return "module_revision"
        if "author" in value:
            return "module_authoring"
        if "review" in value or "audit" in value:
            return "module_review"
        return "module_task"

    def _checkpoint_projection(self, run_id: str) -> dict[str, Any]:
        path = (
            self.runtime.paths.project_storage_root
            / "Work"
            / "runs"
            / run_id
            / "workflow-state.json"
        )
        if not path.is_file():
            return {}
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return payload if isinstance(payload, dict) else {}

    def _verified_module_checkpoint_refs(
        self,
        run_id: str,
        checkpoint: dict[str, Any],
        field: str,
        *,
        expected_kind: str | None = None,
        expected_lifecycle: str | None = None,
    ) -> set[str]:
        refs = checkpoint.get(field, {}) or {}
        if not isinstance(refs, dict):
            return set()
        run_root = (
            self.runtime.paths.project_storage_root / "Work" / "runs" / run_id
        ).resolve()
        verified: set[str] = set()
        for module_id, ref in refs.items():
            if module_id not in REPORT_MODULE_IDS or not isinstance(ref, str):
                continue
            path = (self.runtime.paths.project_storage_root / ref).resolve()
            if not path.is_relative_to(run_root) or not path.is_file():
                continue
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if (
                payload.get("run_id") != run_id
                or (
                    expected_kind is not None
                    and payload.get("kind") != expected_kind
                )
                or (
                    expected_lifecycle is not None
                    and payload.get("lifecycle") != expected_lifecycle
                )
                or (
                    expected_kind is not None
                    and payload.get("module_id") != module_id
                )
            ):
                continue
            verified.add(module_id)
        return verified

    def progress(self, token: str, project_id: str, run_id: str) -> dict:
        self._user(token, project_id)
        job = self.runtime.jobs.get_run(run_id)
        module_task_states = {
            module_id: {"status": "not_started", "phase": "not_started"}
            for module_id in REPORT_MODULE_IDS
        }
        event_status = {
            "TaskDispatched": "pending",
            "AttemptStarted": "running",
            "TypedResultAccepted": "completed",
            "TaskFailed": "failed",
        }
        for event in LocalEventStore(self.runtime.paths.project_storage_root, run_id).read():
            if event.event_type not in event_status:
                continue
            module_id = self._module_id(event)
            if module_id is None:
                continue
            module_task_states[module_id] = {
                "status": event_status[event.event_type],
                "phase": self._task_phase(event.task_id),
            }
        checkpoint = self._checkpoint_projection(run_id)
        review_refs = self._verified_module_checkpoint_refs(
            run_id,
            checkpoint,
            "module_review_completion_refs",
            expected_lifecycle="module",
        )
        modules = []
        for module_id in REPORT_MODULE_IDS:
            if module_id in review_refs:
                status = "completed"
                phase = "completed"
            else:
                task_state = module_task_states[module_id]
                status = (
                    "running"
                    if task_state["status"] == "completed"
                    else task_state["status"]
                )
                phase = task_state["phase"]
            modules.append(
                {
                    "module_id": module_id,
                    "role": f"Module {module_id} Specialist",
                    "status": status,
                    "phase": phase,
                    "task_status": module_task_states[module_id]["status"],
                    "review_status": (
                        "completed" if module_id in review_refs else "not_started"
                    ),
                }
            )
        return {
            "run_id": run_id,
            "status": job.status,
            "modules": modules,
        }

    def events(
        self, token: str, project_id: str, run_id: str, cursor: int = 0
    ) -> dict:
        self._user(token, project_id)
        events = LocalEventStore(
            self.runtime.paths.project_storage_root, run_id
        ).read()
        public: list[dict[str, Any]] = []
        for event in events:
            if event.sequence <= cursor:
                continue
            module_id = self._module_id(event)
            if module_id is not None and event.event_type in {
                "TaskDispatched",
                "AttemptStarted",
                "TypedResultAccepted",
                "TaskFailed",
            }:
                status = {
                    "TaskDispatched": "pending",
                    "AttemptStarted": "running",
                    "TypedResultAccepted": "completed",
                    "TaskFailed": "failed",
                }[event.event_type]
                public.append(
                    {
                        "sequence": event.sequence,
                        "type": "module_status",
                        "module_id": module_id,
                        # Task completion is not module completion.  The progress
                        # snapshot promotes a module only from its review proof.
                        "status": (
                            "running"
                            if event.event_type == "TypedResultAccepted"
                            else status
                        ),
                        "task_status": status,
                        "phase": self._task_phase(event.task_id),
                    }
                )
            elif event.event_type.startswith("Run") or event.event_type in {
                "CancelRequested",
                "StageReady",
                "StageCompleted",
            }:
                public.append(
                    {
                        "sequence": event.sequence,
                        "type": event.event_type,
                        "stage": event.stage_id,
                    }
                )
        return {
            "run_id": run_id,
            "cursor": events[-1].sequence if events else cursor,
            "events": public,
        }

    def list_files(self, token: str, project_id: str) -> list[dict[str, Any]]:
        self._user(token, project_id)
        rows = []
        for root in self.files.READ_ROOTS:
            directory = self.runtime.paths.project_storage_root / root
            if not directory.is_dir():
                continue
            for path in sorted(directory.rglob("*")):
                if path.is_file():
                    try:
                        self.files.resolve(
                            path.relative_to(
                                self.runtime.paths.project_storage_root
                            ).as_posix()
                        )
                    except PermissionError:
                        continue
                    rows.append(
                        {
                            "ref": path.relative_to(
                                self.runtime.paths.project_storage_root
                            ).as_posix(),
                            "size": path.stat().st_size,
                        }
                    )
        return rows

    def read_file(self, token: str, project_id: str, logical_ref: str) -> tuple[bytes, str]:
        user = self._user(token, project_id)
        path = self.files.resolve(logical_ref)
        if not path.is_file():
            raise FileNotFoundError("public project file does not exist")
        content = path.read_bytes()
        self._audit("file.downloaded", user, {"ref": logical_ref, "size": len(content)})
        return content, mimetypes.guess_type(path.name)[0] or "application/octet-stream"

    def upload_file(
        self,
        token: str,
        project_id: str,
        logical_ref: str,
        content: bytes,
    ) -> dict[str, Any]:
        user = self._user(token, project_id, write=True)
        if self.production_policy is not None:
            self.production_policy.validate_upload(logical_ref, len(content))
        path = self.files.resolve(logical_ref, write=True)
        snapshot_lock = (
            self.runtime.paths.project_storage_root
            / "Work/leases/run-input-snapshot.lock"
        )
        with exclusive_file_lock(snapshot_lock):
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}-",
                suffix=".upload",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
        self._audit("file.uploaded", user, {"ref": logical_ref, "size": len(content)})
        return {
            "ref": logical_ref,
            "size": len(content),
            "sha256": hashlib.sha256(content).hexdigest(),
        }

    def create_conversation(self, token: str, project_id: str) -> dict[str, str]:
        user = self._user(token, project_id, write=True)
        return self.conversations.create(user.user_id)

    def append_conversation(
        self,
        token: str,
        project_id: str,
        session_id: str,
        content: str,
    ) -> dict[str, Any]:
        user = self._user(token, project_id, write=True)
        return self.conversations.append(
            session_id, user.user_id, role="user", content=content
        )

    def read_conversation(
        self, token: str, project_id: str, session_id: str
    ) -> dict[str, Any]:
        user = self._user(token, project_id)
        return self.conversations.read(session_id, user.user_id)


_INDEX_HTML = b"""<!doctype html><html><head><meta charset='utf-8'><title>Manyselves</title>
<style>body{font:14px system-ui;margin:0;display:grid;grid-template-columns:1fr 1fr 1fr;height:100vh}
section{padding:18px;border-right:1px solid #ddd;overflow:auto}h1{font-size:18px}</style></head>
<body><section id='files'><h1>Files</h1></section><section id='progress'><h1>Progress</h1></section>
<section id='conversation'><h1>Main conversation</h1></section></body></html>"""


class ReportingASGIApp:
    """Minimal JSON API and three-pane browser shell."""

    def __init__(self, api: ReportingApi) -> None:
        self.api = api

    @staticmethod
    async def _body(receive) -> bytes:
        chunks = []
        while True:
            message = await receive()
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                return b"".join(chunks)

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            return
        path = scope.get("path", "/")
        method = scope.get("method", "GET")
        headers = {
            key.decode("latin1").casefold(): value.decode("latin1")
            for key, value in scope.get("headers", [])
        }
        try:
            if path == "/" and method == "GET":
                await self._respond(send, 200, _INDEX_HTML, "text/html; charset=utf-8")
                return
            if path == "/healthz" and method == "GET":
                await self._json(send, 200, {"status": "ok"})
                return
            authorization = headers.get("authorization", "")
            if not authorization.startswith("Bearer "):
                raise PermissionError("Bearer token is required")
            token = authorization.removeprefix("Bearer ")
            parts = [part for part in path.split("/") if part]
            if len(parts) < 4 or parts[:2] != ["v1", "projects"]:
                raise FileNotFoundError("unknown API route")
            project_id = parts[2]
            payload = json.loads((await self._body(receive)) or b"{}")
            if parts[3:] == ["runs"] and method == "POST":
                result = self.api.create_run(token, project_id, payload)
                await self._json(send, 202, result)
                return
            if parts[3:] == ["revisions"] and method == "POST":
                await self._json(
                    send,
                    202,
                    self.api.create_revision(token, project_id, payload),
                )
                return
            if parts[3:] == ["files"] and method == "GET":
                await self._json(
                    send, 200, self.api.list_files(token, project_id)
                )
                return
            if parts[3:] == ["files"] and method == "POST":
                result = self.api.upload_file(
                    token,
                    project_id,
                    str(payload["ref"]),
                    base64.b64decode(str(payload["content_base64"]), validate=True),
                )
                await self._json(send, 201, result)
                return
            if len(parts) > 4 and parts[3] == "files" and method == "GET":
                content, media_type = self.api.read_file(
                    token, project_id, "/".join(parts[4:])
                )
                await self._respond(send, 200, content, media_type)
                return
            if parts[3:] == ["conversations"] and method == "POST":
                await self._json(
                    send,
                    201,
                    self.api.create_conversation(token, project_id),
                )
                return
            if len(parts) == 5 and parts[3] == "conversations":
                session_id = parts[4]
                if method == "GET":
                    await self._json(
                        send,
                        200,
                        self.api.read_conversation(
                            token, project_id, session_id
                        ),
                    )
                    return
                if method == "POST":
                    await self._json(
                        send,
                        201,
                        self.api.append_conversation(
                            token,
                            project_id,
                            session_id,
                            str(payload["content"]),
                        ),
                    )
                    return
            if len(parts) >= 5 and parts[3] == "runs":
                run_id = parts[4]
                action = parts[5] if len(parts) > 5 else "progress"
                if action == "progress" and method == "GET":
                    await self._json(send, 200, self.api.progress(token, project_id, run_id))
                    return
                if action == "events" and method == "GET":
                    query = scope.get("query_string", b"").decode("ascii")
                    cursor = int(dict(part.split("=", 1) for part in query.split("&") if "=" in part).get("cursor", 0))
                    await self._json(send, 200, self.api.events(token, project_id, run_id, cursor))
                    return
                if action == "cancel" and method == "POST":
                    await self._json(send, 202, self.api.cancel_run(token, project_id, run_id))
                    return
                if action == "resume" and method == "POST":
                    await self._json(send, 202, self.api.resume_run(token, project_id, run_id))
                    return
                if action == "decision" and method == "POST":
                    await self._json(
                        send,
                        202,
                        self.api.resolve_evidence_decision(
                            token, project_id, run_id, payload
                        ),
                    )
                    return
            raise FileNotFoundError("unknown API route")
        except PermissionError as exc:
            await self._json(send, 403, {"error": str(exc)})
        except (FileNotFoundError, ValueError) as exc:
            await self._json(send, 404, {"error": str(exc)})

    @staticmethod
    async def _respond(send, status: int, body: bytes, content_type: str) -> None:
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", content_type.encode("ascii"))],
            }
        )
        await send({"type": "http.response.body", "body": body})

    async def _json(self, send, status: int, payload: Any) -> None:
        await self._respond(
            send,
            status,
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
        )
