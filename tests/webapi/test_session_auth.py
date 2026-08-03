"""Focused session-key lifecycle tests with no web application imports."""

import os
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from manyselves.webapi import session_auth


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes do not apply on Windows")
def test_session_key_and_auth_directory_are_owner_only_on_posix(tmp_path: Path) -> None:
    """Inherited group-readable auth storage would expose the signing credential."""
    key_path = tmp_path / "auth" / "session.key"

    session_auth.SessionSigner(key_path, ttl_seconds=3600)

    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


def test_windows_hardening_restricts_auth_directory_and_key_to_current_user(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Inheriting a broad NTFS DACL would let another local account forge sessions."""
    key_path = tmp_path / "auth" / "session.key"
    calls: list[list[str]] = []

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(command)
        output = "WORKSTATION\\alice\n" if command == ["whoami"] else ""
        return subprocess.CompletedProcess(command, 0, output, "")

    monkeypatch.setattr(session_auth, "_running_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(subprocess, "run", run)

    session_auth.SessionSigner(key_path, ttl_seconds=3600)

    icacls_calls = [command for command in calls if command[0] == "icacls"]
    assert [str(key_path.parent), str(key_path)] == [call[1] for call in icacls_calls if call[1] in {str(key_path.parent), str(key_path)}]
    assert all("/inheritance:r" in call for call in icacls_calls)
    assert all("WORKSTATION\\alice:(F)" in call for call in icacls_calls)


def test_windows_hardening_failure_prevents_key_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Continuing after an ACL failure would leave a signing key readable by other users."""
    key_path = tmp_path / "auth" / "session.key"

    def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        if command == ["whoami"]:
            return subprocess.CompletedProcess(command, 0, "WORKSTATION\\alice\n", "")
        raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(session_auth, "_running_on_windows", lambda: True, raising=False)
    monkeypatch.setattr(subprocess, "run", run)

    with pytest.raises(RuntimeError, match="securely configure session permissions"):
        session_auth.SessionSigner(key_path, ttl_seconds=3600)

    assert not key_path.exists()


def test_concurrent_creators_never_read_a_partially_written_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Publishing the final path before its bytes are durable makes a racing startup fail."""
    key_path = tmp_path / "auth" / "session.key"
    real_fdopen = session_auth.os.fdopen
    writing_started = threading.Event()
    allow_write = threading.Event()

    class DelayedFile:
        def __init__(self, file) -> None:
            self._file = file

        def __enter__(self):
            self._file.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._file.__exit__(*args)

        def write(self, value: bytes) -> int:
            writing_started.set()
            assert allow_write.wait(timeout=5)
            return self._file.write(value)

        def __getattr__(self, name: str):
            return getattr(self._file, name)

    def delayed_fdopen(descriptor: int, mode: str):
        return DelayedFile(real_fdopen(descriptor, mode))

    monkeypatch.setattr(session_auth.os, "fdopen", delayed_fdopen)

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(session_auth.SessionSigner, key_path, 3600)
        assert writing_started.wait(timeout=5)
        second = pool.submit(session_auth.SessionSigner, key_path, 3600)
        time.sleep(0.3)
        allow_write.set()
        first_signer = first.result(timeout=5)
        second_signer = second.result(timeout=5)

    value = first_signer.issue("admin")
    assert second_signer.verify(value) is not None


def test_failed_key_write_leaves_no_final_or_temporary_key(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A failed first write must not poison later startup with a zero-byte key file."""
    key_path = tmp_path / "auth" / "session.key"
    real_fdopen = session_auth.os.fdopen

    class FailingFile:
        def __init__(self, file) -> None:
            self._file = file

        def __enter__(self):
            self._file.__enter__()
            return self

        def __exit__(self, *args: object) -> None:
            self._file.__exit__(*args)

        def write(self, _value: bytes) -> int:
            raise OSError("simulated write failure")

    def failing_fdopen(descriptor: int, mode: str):
        return FailingFile(real_fdopen(descriptor, mode))

    monkeypatch.setattr(session_auth.os, "fdopen", failing_fdopen)

    with pytest.raises(OSError, match="simulated write failure"):
        session_auth.SessionSigner(key_path, ttl_seconds=3600)

    assert not key_path.exists()
    assert list(key_path.parent.iterdir()) == []
