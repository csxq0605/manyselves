"""Run child processes that never outlive the call that started them.

Every child is started in its own process group.  On timeout, cancellation or
any other error the whole group gets SIGTERM, then SIGKILL after a short grace
period, and the call returns only after the child has exited.  This keeps a
cancelled task from leaving a process that keeps writing files while the next
task runs.
"""

import asyncio
import os
import signal
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

POSIX = os.name == "posix"

# Variables a child process needs to run normally.  Provider API keys,
# account passwords and other service secrets are deliberately absent.
SAFE_ENV_KEYS = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "LANG",
    "LANGUAGE",
    "TERM",
    "TMPDIR",
    "TZ",
    "PYTHONIOENCODING",
    "PYTHONUTF8",
)


def safe_child_env(extra_keys: Sequence[str] = ()) -> dict[str, str]:
    """Return a minimal environment for a child process, without secrets."""

    allowed = set(SAFE_ENV_KEYS) | set(extra_keys)
    return {
        key: value
        for key, value in os.environ.items()
        if key in allowed or key.startswith("LC_")
    }


@dataclass(slots=True)
class CapturedRun:
    stdout: bytes
    stderr: bytes
    returncode: int | None
    timed_out: bool = False


async def terminate_process_tree(
    process: asyncio.subprocess.Process,
    *,
    grace_seconds: float = 0.5,
) -> None:
    """Stop a child started by this module and everything it spawned."""

    if not POSIX:
        if process.returncode is None:
            process.kill()
            await process.wait()
        return
    if process.returncode is not None:
        # The leader is gone; descendants may still hold the group.
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        return
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        async with asyncio.timeout(grace_seconds):
            await process.wait()
    except TimeoutError:
        pass
    try:
        os.killpg(process.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass
    if process.returncode is None:
        await process.wait()


async def _shielded_terminate(process: asyncio.subprocess.Process) -> None:
    """Finish the termination even if the caller is cancelled again meanwhile."""

    task = asyncio.ensure_future(terminate_process_tree(process))
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                break
            continue
    task.result()


async def run_captured(
    command: Sequence[str] | str,
    *,
    cwd: Path | str | None = None,
    env: Mapping[str, str] | None = None,
    timeout: float | None = None,
    shell: bool = False,
) -> CapturedRun:
    """Run a command, capture its output, and reap it on every exit path."""

    kwargs = {
        "cwd": cwd,
        "env": dict(env) if env is not None else None,
        "stdout": asyncio.subprocess.PIPE,
        "stderr": asyncio.subprocess.PIPE,
        "start_new_session": POSIX,
    }
    if shell:
        if not isinstance(command, str):
            raise TypeError("a shell command must be a string")
        process = await asyncio.create_subprocess_shell(command, **kwargs)
    else:
        if isinstance(command, str):
            raise TypeError("an exec command must be a sequence of arguments")
        process = await asyncio.create_subprocess_exec(*command, **kwargs)

    try:
        async with asyncio.timeout(timeout):
            stdout, stderr = await process.communicate()
    except TimeoutError:
        await _shielded_terminate(process)
        return CapturedRun(b"", b"", process.returncode, timed_out=True)
    except BaseException:
        await _shielded_terminate(process)
        raise
    # The leader exited; make sure nothing it spawned keeps running.
    await terminate_process_tree(process)
    return CapturedRun(stdout, stderr, process.returncode)
