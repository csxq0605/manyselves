"""Focused session-key lifecycle tests with no web application imports."""

import os
import re
import stat
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from manyselves.webapi import session_auth


def _windows_system_executable(name: str) -> str:
    """Locate a test setup tool without allowing PATH search."""
    return str(Path(os.environ["SystemRoot"]) / "System32" / name)


def _windows_current_user_sid() -> str:
    """Read the current SID for independent ACL assertions in this integration test."""
    result = subprocess.run(
        [_windows_system_executable("whoami.exe"), "/user"],
        capture_output=True,
        check=True,
        text=True,
    )
    match = re.search(r"S-\d+(?:-\d+)+", result.stdout)
    assert match is not None
    return match.group(0)


def _windows_dacl_aces(path: Path) -> list[tuple[int, int, str]]:
    """Enumerate the file DACL directly, independent of the application helper."""
    import ctypes
    from ctypes import POINTER, Structure, byref, c_void_p
    from ctypes import wintypes

    class AclSizeInformation(Structure):
        _fields_ = [
            ("ace_count", wintypes.DWORD),
            ("acl_bytes_in_use", wintypes.DWORD),
            ("acl_bytes_free", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    get_named_security_info = advapi32.GetNamedSecurityInfoW
    get_named_security_info.argtypes = [
        wintypes.LPWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        POINTER(c_void_p),
        POINTER(c_void_p),
        POINTER(c_void_p),
        POINTER(c_void_p),
        POINTER(c_void_p),
    ]
    get_named_security_info.restype = wintypes.DWORD
    get_acl_information = advapi32.GetAclInformation
    get_acl_information.argtypes = [c_void_p, c_void_p, wintypes.DWORD, wintypes.DWORD]
    get_acl_information.restype = wintypes.BOOL
    get_ace = advapi32.GetAce
    get_ace.argtypes = [c_void_p, wintypes.DWORD, POINTER(c_void_p)]
    get_ace.restype = wintypes.BOOL
    convert_sid = advapi32.ConvertSidToStringSidW
    convert_sid.argtypes = [c_void_p, POINTER(wintypes.LPWSTR)]
    convert_sid.restype = wintypes.BOOL
    local_free = kernel32.LocalFree
    local_free.argtypes = [c_void_p]
    local_free.restype = c_void_p

    dacl = c_void_p()
    descriptor = c_void_p()
    result = get_named_security_info(
        str(path), 1, 0x00000004, None, None, byref(dacl), None, byref(descriptor)
    )
    assert result == 0
    try:
        info = AclSizeInformation()
        assert get_acl_information(dacl, byref(info), ctypes.sizeof(info), 2)
        entries: list[tuple[int, int, str]] = []
        for index in range(info.ace_count):
            ace = c_void_p()
            assert get_ace(dacl, index, byref(ace))
            address = ace.value
            assert address is not None
            ace_type = ctypes.c_ubyte.from_address(address).value
            ace_flags = ctypes.c_ubyte.from_address(address + 1).value
            sid = c_void_p(address + 8)
            sid_text = wintypes.LPWSTR()
            assert convert_sid(sid, byref(sid_text))
            try:
                entries.append((ace_type, ace_flags, sid_text.value))
            finally:
                local_free(sid_text)
        return entries
    finally:
        local_free(descriptor)


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission modes do not apply on Windows")
def test_session_key_and_auth_directory_are_owner_only_on_posix(tmp_path: Path) -> None:
    """Inherited group-readable auth storage would expose the signing credential."""
    key_path = tmp_path / "auth" / "session.key"

    session_auth.SessionSigner(key_path, ttl_seconds=3600)

    assert stat.S_IMODE(key_path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(key_path.stat().st_mode) == 0o600


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics require NTFS")
def test_windows_hardening_replaces_an_explicit_other_allow_ace(tmp_path: Path) -> None:
    """Leaving an existing Everyone Allow ACE would expose the session signing key."""
    key_path = tmp_path / "auth" / "session.key"
    key_path.parent.mkdir()
    key_path.write_bytes(b"x" * 32)
    subprocess.run(
        [_windows_system_executable("icacls.exe"), str(key_path), "/grant:r", "*S-1-1-0:(R)"],
        capture_output=True,
        check=True,
        text=True,
    )
    assert any(sid == "S-1-1-0" for _type, _flags, sid in _windows_dacl_aces(key_path))

    session_auth._harden_permissions(key_path, is_directory=False)

    assert _windows_dacl_aces(key_path) == [(0, 0, _windows_current_user_sid())]


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL semantics require NTFS")
def test_windows_acl_verification_failure_prevents_key_creation(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Using a key after a failed DACL readback would expose an unverified secret."""
    key_path = tmp_path / "auth" / "session.key"
    monkeypatch.setattr(
        session_auth,
        "_windows_dacl_is_current_user_only",
        lambda _path, *, is_directory: False,
        raising=False,
    )

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
