"""HTTP-safe wrapper over the existing reporting controller and persisted store."""

import asyncio
import hashlib
import json
import os
from collections import OrderedDict
from pathlib import Path
from typing import Any, Callable
from uuid import UUID

from ..core.reporting.models import (
    EvidenceDecisionAction,
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)
from .errors import CommandIdConflictError


class ReportingNotFoundError(LookupError):
    code = "REPORT_RUN_NOT_FOUND"


class ReportingInvalidTransitionError(ValueError):
    code = "REPORT_INVALID_TRANSITION"


class ReportingStateInvalidError(RuntimeError):
    code = "REPORT_STATE_INVALID"


class ReportingFacade:
    """Use ReportingRunController commands and read its unchanged durable files."""

    def __init__(self, workspace: Path, controller: Any | None) -> None:
        self.workspace = Path(workspace).resolve()
        self.controller = controller
        self._commands: OrderedDict[UUID, tuple[tuple[Any, ...], dict[str, Any]]] = OrderedDict()

    @classmethod
    def from_runtime(
        cls, host: Any, *, workspace: Path | None = None
    ) -> "ReportingFacade":
        controller = cls._runtime_controller(host)
        selected_workspace = workspace or getattr(host, "workspace", None)
        if selected_workspace is None:
            raise RuntimeError("Runtime workspace is unavailable")
        return cls(selected_workspace, controller)

    def rebind(self, host: Any, workspace: Path) -> None:
        """Follow the reporting boundary recreated by a project activation."""
        self.workspace = Path(workspace).resolve()
        self.controller = self._runtime_controller(host)

    @staticmethod
    def _runtime_controller(host: Any) -> Any | None:
        controller = None
        manager = getattr(host, "loop_manager", None)
        get_loop = getattr(manager, "get_loop", None)
        loop = get_loop("main") if callable(get_loop) else None
        tools = getattr(loop, "tools", None)
        tool = tools.get("run_reporting_workflow") if tools is not None else None
        if tool is not None:
            controller = getattr(tool, "controller", None)
        return controller

    @property
    def active(self) -> bool:
        tasks = getattr(self.controller, "_tasks", {}) if self.controller is not None else {}
        return any(not task.done() for task in tasks.values())

    def list_runs(self) -> list[dict[str, Any]]:
        try:
            runs_root = self._runs_root()
        except ReportingNotFoundError:
            return []
        ids = {
            path.name
            for path in runs_root.iterdir()
            if path.is_dir() and not path.is_symlink()
        } if runs_root.exists() else set()
        if runs_root.exists():
            ids.update(path.stem for path in runs_root.glob("*.json"))
        if self.controller is not None:
            ids.update(str(run_id) for run_id in getattr(self.controller, "_tasks", {}))
        result = []
        for run_id in sorted(ids, reverse=True):
            try:
                result.append(self.snapshot(run_id)["run"])
            except ReportingNotFoundError:
                continue
        return result

    def snapshot(self, run_id: str) -> dict[str, Any]:
        self._safe_id(run_id)
        runs_root = self._runs_root()
        root = runs_root / run_id
        if root.is_symlink() or (root.exists() and not root.is_dir()):
            raise ReportingNotFoundError(run_id)
        self._require_contained(root, runs_root, invalid=False)
        result_path = runs_root / f"{run_id}.json"
        result = self._json(result_path, runs_root)
        state_path = root / "workflow-state.json"
        state = self._json(state_path, root)
        request_path = root / "request.json"
        self._json(request_path, root)
        decisions_root = root / "decisions"
        if decisions_root.is_symlink():
            raise ReportingStateInvalidError("Reporting decisions path is invalid")
        if decisions_root.exists() and not decisions_root.is_dir():
            raise ReportingStateInvalidError("Reporting decisions path is invalid")
        self._require_contained(decisions_root, root)
        decision_paths = (
            sorted(decisions_root.glob("*.json")) if decisions_root.is_dir() else []
        )
        live = self.controller.status(run_id) if self.controller is not None else {}
        live_recognized = (
            live.get("source") == "live"
            and live.get("status") not in {None, "not_found", "unknown"}
        )
        recognized = any(
            path.is_file()
            for path in (
                result_path,
                state_path,
                request_path,
                root / "evidence-choice.json",
                root / "revision-request.json",
                *decision_paths,
            )
        ) or live_recognized
        if not recognized:
            raise ReportingNotFoundError(run_id)
        run = {"run_id": run_id, **result}
        run["active"] = bool(live.get("active", False))
        for key in ("status", "task_id", "source"):
            if live.get(key) is not None:
                run[key] = live[key]
        decisions = [self._json(path, decisions_root) for path in decision_paths]
        outputs = []
        for raw in result.get("output_paths", []):
            relative = Path(str(raw))
            if relative.is_absolute() or ".." in relative.parts:
                continue
            target = (self.workspace / relative).resolve()
            if not target.is_relative_to(self.workspace):
                continue
            exists = target.is_file()
            outputs.append(
                {
                    "path": relative.as_posix(),
                    "exists": exists,
                    "size": target.stat().st_size if exists else 0,
                    "sha256": self._sha256(target) if exists else None,
                }
            )
        return {
            "run": run,
            "state": state,
            "waitingInput": [item for item in decisions if item.get("status") == "pending"],
            "checkpoint": state,
            "evidence": self._json(root / "evidence-choice.json", root),
            "revision": self._json(root / "revision-request.json", root),
            "outputs": outputs,
        }

    def start(self, command_id: UUID, request: ReportRequest) -> dict[str, Any]:
        return self._command(command_id, ("start", request.model_dump_json()), lambda: self._require().start(request))

    def cancel(self, command_id: UUID, run_id: str) -> dict[str, Any]:
        self._safe_id(run_id)

        def invoke() -> dict[str, Any]:
            self.snapshot(run_id)
            if not self._require().cancel(run_id):
                raise ReportingInvalidTransitionError("Report run is not active")
            return {"status": "running", "run_id": run_id}
        return self._command(command_id, ("cancel", run_id), invoke)

    def resume_run(
        self,
        command_id: UUID,
        run_id: str,
        *,
        max_provider_attempts: int | None,
        max_total_tokens: int | None,
        supplements: list[UserSupplement],
    ) -> dict[str, Any]:
        self._safe_id(run_id)
        payload = (
            "resume_run", run_id, max_provider_attempts, max_total_tokens,
            tuple(item.model_dump_json() for item in supplements),
        )
        return self._command(
            command_id,
            payload,
            lambda: self._require().resume_run(
                run_id,
                max_provider_attempts=max_provider_attempts,
                max_total_tokens=max_total_tokens,
                supplements=supplements,
            ),
        )

    def resume_decision(
        self,
        command_id: UUID,
        decision_id: str,
        action: EvidenceDecisionAction,
        supplements: list[UserSupplement],
    ) -> dict[str, Any]:
        payload = ("resume_decision", decision_id, action, tuple(item.model_dump_json() for item in supplements))
        return self._command(
            command_id,
            payload,
            lambda: self._require().resume_decision(decision_id, action, supplements),
        )

    def revise(self, command_id: UUID, request: RevisionRequest) -> dict[str, Any]:
        return self._command(
            command_id,
            ("revise", request.model_dump_json()),
            lambda: self._require().revise(request),
        )

    def flush(self) -> None:
        runs = self.workspace / "Work/runs"
        for path in runs.rglob("*") if runs.exists() else ():
            if path.is_file() and not path.is_symlink() and path.name != ".active.lock":
                with path.open("rb") as handle:
                    os.fsync(handle.fileno())
        if os.name == "posix" and runs.exists():
            directories = [
                runs,
                *(path for path in runs.rglob("*") if path.is_dir() and not path.is_symlink()),
            ]
            for path in sorted(
                directories, key=lambda item: len(item.parts), reverse=True
            ):
                descriptor = os.open(
                    path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
                )
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)

    async def close(self) -> None:
        """Cancel and join controller-owned reporting tasks before runtime stop."""
        tasks: list[asyncio.Task[Any]] = []
        if self.controller is not None:
            for attribute in ("_tasks", "_status_watchers"):
                owned = getattr(self.controller, attribute, {})
                tasks.extend(
                    task for task in owned.values() if isinstance(task, asyncio.Task)
                )
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.flush()

    def _command(
        self,
        command_id: UUID,
        semantic: tuple[Any, ...],
        invoke: Callable[[], dict[str, Any]],
    ) -> dict[str, Any]:
        semantic = (str(self.workspace), *semantic)
        cached = self._commands.get(command_id)
        if cached is not None:
            if cached[0] != semantic:
                raise CommandIdConflictError()
            return cached[1]
        try:
            response = invoke()
        except FileNotFoundError as exc:
            raise ReportingNotFoundError(str(exc)) from exc
        self._commands[command_id] = (semantic, response)
        while len(self._commands) > 256:
            self._commands.popitem(last=False)
        return response

    def _require(self) -> Any:
        if self.controller is None:
            raise RuntimeError("Reporting controller is unavailable")
        return self.controller

    @staticmethod
    def _safe_id(value: str) -> None:
        if not value or Path(value).name != value or value in {".", ".."}:
            raise ReportingNotFoundError(value)

    def _json(self, path: Path, root: Path) -> dict[str, Any]:
        self._require_contained(path, root)
        if not path.is_file():
            return {}
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ReportingStateInvalidError("Persisted reporting state is invalid") from exc
        if not isinstance(value, dict):
            raise ReportingStateInvalidError("Persisted reporting state is invalid")
        return value

    def _runs_root(self) -> Path:
        work = self.workspace / "Work"
        runs = work / "runs"
        if work.is_symlink() or runs.is_symlink():
            raise ReportingNotFoundError("Work/runs")
        if runs.exists() and not runs.is_dir():
            raise ReportingNotFoundError("Work/runs")
        self._require_contained(runs, self.workspace, invalid=False)
        return runs

    def _require_contained(
        self, path: Path, root: Path, *, invalid: bool = True
    ) -> None:
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            if invalid:
                raise ReportingStateInvalidError("Reporting state escapes its root") from exc
            raise ReportingNotFoundError(str(path)) from exc
        current = root
        for part in relative.parts:
            current = current / part
            if current.is_symlink():
                if invalid:
                    raise ReportingStateInvalidError("Reporting state contains a symlink")
                raise ReportingNotFoundError(str(path))
        if not path.resolve(strict=False).is_relative_to(root.resolve(strict=False)):
            if invalid:
                raise ReportingStateInvalidError("Reporting state escapes its root")
            raise ReportingNotFoundError(str(path))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
