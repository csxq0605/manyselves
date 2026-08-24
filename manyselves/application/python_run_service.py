"""Trusted, project-scoped Python execution with bounded captured output.

This service is intentionally not a security sandbox. Deployment must remain on
a trusted network and callers must already hold deployment access and control.
"""

import asyncio
import os
import signal
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal
from uuid import UUID

from ..interfaces.types import AgentStatus, StatusChange
from ..runtime.loops.bus import MessageBus
from .errors import CommandIdConflictError


class InvalidPythonPathError(ValueError):
    code = "INVALID_PYTHON_PATH"


class PythonOperationNotFoundError(LookupError):
    code = "PYTHON_OPERATION_NOT_FOUND"


class PythonRunUnsupportedError(RuntimeError):
    code = "PYTHON_RUN_UNSUPPORTED"

    def __init__(self) -> None:
        super().__init__("Python operations are supported only by the Linux server deployment")


@dataclass(slots=True)
class PythonOperation:
    operation_id: str
    path: str
    arguments: list[str]
    status: Literal["running", "completed", "failed", "timed_out", "interrupted"] = "running"
    stdout: bytes = b""
    stderr: bytes = b""
    stdout_truncated: bool = False
    stderr_truncated: bool = False
    return_code: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    launch_ready: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    interrupt_requested: bool = field(default=False, repr=False)


class PythonRunService:
    def __init__(
        self,
        workspace: Path,
        *,
        bus: MessageBus,
        timeout_seconds: float = 30.0,
        output_limit_bytes: int = 64 * 1024,
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.timeout_seconds = timeout_seconds
        self.output_limit_bytes = output_limit_bytes
        self._platform = os.name
        self._operations: dict[str, PythonOperation] = {}
        self._semantic: dict[str, tuple[str, tuple[str, ...]]] = {}

    @property
    def active(self) -> bool:
        return any(item.status == "running" for item in self._operations.values())

    def rebind(self, workspace: Path) -> None:
        """Point new operations at an activated project when none are active."""
        if self.active:
            raise RuntimeError("Cannot switch project while Python operations are active")
        self.workspace = Path(workspace).resolve()

    def start(self, command_id: UUID, path: str, arguments: list[str]) -> PythonOperation:
        if self._platform != "posix":
            raise PythonRunUnsupportedError()
        target, relative = self._resolve(path)
        operation_id = str(command_id)
        semantic = (f"{self.workspace}:{relative}", tuple(arguments))
        if operation_id in self._operations:
            if self._semantic[operation_id] != semantic:
                raise CommandIdConflictError()
            return self._operations[operation_id]
        operation = PythonOperation(operation_id, relative, list(arguments))
        self._operations[operation_id] = operation
        self._semantic[operation_id] = semantic
        operation.task = asyncio.create_task(
            self._execute(operation, target), name=f"manyselves-python-{operation_id}"
        )
        return operation

    def get(self, operation_id: str) -> PythonOperation:
        try:
            return self._operations[operation_id]
        except KeyError as exc:
            raise PythonOperationNotFoundError(operation_id) from exc

    async def interrupt(self, operation_id: str) -> PythonOperation:
        operation = self.get(operation_id)
        if operation.status != "running":
            raise ValueError("Python operation is not running")
        operation.interrupt_requested = True
        await operation.launch_ready.wait()
        if operation.process is not None:
            await self._terminate(operation.process)
        if operation.task is not None:
            await operation.task
        return operation

    async def close(self) -> None:
        active = [item for item in self._operations.values() if item.status == "running"]
        for item in active:
            item.interrupt_requested = True
            await item.launch_ready.wait()
            if item.process is not None:
                await self._terminate(item.process)
        await asyncio.gather(
            *(item.task for item in active if item.task is not None), return_exceptions=True
        )

    async def _execute(self, operation: PythonOperation, target: Path) -> None:
        await self._publish(operation)
        terminal_status: Literal[
            "completed", "failed", "timed_out", "interrupted"
        ] = "failed"
        try:
            process = await asyncio.create_subprocess_exec(
                sys.executable,
                str(target),
                *operation.arguments,
                cwd=self.workspace,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                start_new_session=True,
            )
            operation.process = process
            operation.launch_ready.set()
            stdout_task = asyncio.create_task(self._drain(process.stdout))
            stderr_task = asyncio.create_task(self._drain(process.stderr))
            try:
                async with asyncio.timeout(self.timeout_seconds):
                    operation.return_code = await process.wait()
                if operation.interrupt_requested:
                    terminal_status = "interrupted"
                else:
                    terminal_status = (
                        "completed" if operation.return_code == 0 else "failed"
                    )
            except TimeoutError:
                terminal_status = "timed_out"
                await self._terminate(process)
                operation.return_code = process.returncode
            except asyncio.CancelledError:
                terminal_status = "interrupted"
                operation.interrupt_requested = True
                await self._terminate(process)
                operation.return_code = process.returncode
            operation.stdout, operation.stdout_truncated = await stdout_task
            operation.stderr, operation.stderr_truncated = await stderr_task
            operation.status = terminal_status
        except Exception as exc:
            operation.status = "failed"
            operation.stderr = str(exc).encode("utf-8", errors="replace")[: self.output_limit_bytes]
        finally:
            operation.launch_ready.set()
            operation.completed_at = datetime.now(timezone.utc)
            operation.process = None
            await self._publish(operation)

    async def _drain(self, stream: asyncio.StreamReader | None) -> tuple[bytes, bool]:
        if stream is None:
            return b"", False
        output = bytearray()
        truncated = False
        while chunk := await stream.read(8192):
            remaining = self.output_limit_bytes - len(output)
            if remaining > 0:
                output.extend(chunk[:remaining])
            if len(chunk) > remaining:
                truncated = True
        return bytes(output), truncated

    async def _terminate(self, process: asyncio.subprocess.Process) -> None:
        if process.returncode is not None:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            return
        if self._platform != "posix":
            raise PythonRunUnsupportedError()
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
        try:
            async with asyncio.timeout(0.5):
                await process.wait()
        except TimeoutError:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        if process.returncode is None:
            await process.wait()

    def _resolve(self, raw: str) -> tuple[Path, str]:
        relative = Path(raw)
        if relative.is_absolute() or ".." in relative.parts or relative.suffix.casefold() != ".py":
            raise InvalidPythonPathError("Python path must be a project-relative .py file")
        target = (self.workspace / relative).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file():
            raise InvalidPythonPathError("Python path must name an existing project file")
        return target, target.relative_to(self.workspace).as_posix()

    async def _publish(self, operation: PythonOperation) -> None:
        status = AgentStatus.THINKING if operation.status == "running" else (
            AgentStatus.IDLE if operation.status == "completed" else AgentStatus.ERROR
        )
        await self.bus.publish(
            StatusChange(
                agent_type="python-operation",
                status=status,
                extra={"operation_id": operation.operation_id, "operation_status": operation.status},
            )
        )
