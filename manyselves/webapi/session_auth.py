"""Signed local-browser sessions backed by a process-independent key file."""

import base64
import ctypes
import hashlib
import hmac
import json
import os
import secrets
import time
from ctypes import wintypes
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """The non-secret identity carried by a valid browser session."""

    username: str
    expires_at: int


class SessionSigner:
    """Issue and validate compact HMAC-SHA256 session values."""

    def __init__(self, key_path: Path, ttl_seconds: int, *, now=time.time) -> None:
        self._key = _load_or_create_key(key_path)
        self._ttl_seconds = ttl_seconds
        self._now = now

    def issue(self, username: str) -> str:
        """Sign an expiring session value for one administrative username."""
        payload = json.dumps(
            {"exp": int(self._now()) + self._ttl_seconds, "sub": username},
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        encoded_payload = _encode(payload)
        signature = hmac.new(self._key, encoded_payload.encode("ascii"), hashlib.sha256).digest()
        return f"{encoded_payload}.{_encode(signature)}"

    def verify(self, value: str) -> SessionPrincipal | None:
        """Return the principal only when the value is intact and unexpired."""
        try:
            encoded_payload, encoded_signature = value.split(".")
            supplied_signature = _decode(encoded_signature)
            expected_signature = hmac.new(
                self._key, encoded_payload.encode("ascii"), hashlib.sha256
            ).digest()
            if not secrets.compare_digest(supplied_signature, expected_signature):
                return None
            payload = json.loads(_decode(encoded_payload))
            username = payload["sub"]
            expires_at = payload["exp"]
            if (
                not isinstance(username, str)
                or not isinstance(expires_at, int)
                or expires_at <= int(self._now())
            ):
                return None
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return None
        return SessionPrincipal(username=username, expires_at=expires_at)


def _encode(value: bytes) -> str:
    """Encode bytes as an unpadded URL-safe base64 string."""
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    """Decode a URL-safe base64 string while rejecting malformed padding."""
    if not value or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_" for character in value):
        raise ValueError("Invalid base64url value")
    return base64.b64decode(value + "=" * (-len(value) % 4), altchars=b"-_", validate=True)


def _load_or_create_key(key_path: Path) -> bytes:
    """Read a stable key or atomically publish one complete private key."""
    key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _harden_permissions(key_path.parent, is_directory=True)
    try:
        if key_path.exists():
            _harden_permissions(key_path, is_directory=False)
            return _read_key(key_path)
    except OSError as error:
        raise RuntimeError("Unable to securely access the session key") from error

    temporary_path = _create_private_temporary_key(key_path)
    key = secrets.token_bytes(32)
    published = False
    try:
        _write_key(temporary_path, key)
        _harden_permissions(temporary_path, is_directory=False)
        try:
            os.link(temporary_path, key_path)
        except FileExistsError:
            _remove_temporary_key(temporary_path)
            _harden_permissions(key_path, is_directory=False)
            return _read_key(key_path)
        published = True
        _remove_temporary_key(temporary_path)
        _harden_permissions(key_path, is_directory=False)
        return key
    except BaseException:
        _remove_temporary_key(temporary_path)
        if published:
            _remove_published_key(key_path)
        raise


def _create_private_temporary_key(key_path: Path) -> Path:
    """Reserve a same-directory private temporary path without publishing a final key."""
    for _attempt in range(10):
        temporary_path = key_path.with_name(f".{key_path.name}.{secrets.token_hex(16)}.tmp")
        try:
            descriptor = os.open(temporary_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        except FileExistsError:
            continue
        os.close(descriptor)
        return temporary_path
    raise RuntimeError("Unable to securely create a temporary session key")


def _write_key(key_path: Path, key: bytes) -> None:
    """Write all key bytes durably before its path can be published."""
    descriptor = os.open(key_path, os.O_WRONLY | os.O_TRUNC)
    with os.fdopen(descriptor, "wb") as key_file:
        key_file.write(key)
        key_file.flush()
        os.fsync(key_file.fileno())


def _remove_temporary_key(key_path: Path) -> None:
    """Remove an unpublished key, failing safely if cleanup cannot complete."""
    try:
        key_path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError("Unable to remove a temporary session key") from error


def _remove_published_key(key_path: Path) -> None:
    """Remove a key this process published when its final ACL cannot be verified."""
    try:
        key_path.unlink()
    except FileNotFoundError:
        return
    except OSError as error:
        raise RuntimeError("Unable to remove an unverified session key") from error


def _harden_permissions(path: Path, *, is_directory: bool) -> None:
    """Restrict session storage to the current user or fail before use."""
    if _running_on_windows():
        _harden_windows_permissions(path)
        return
    try:
        os.chmod(path, 0o700 if is_directory else 0o600)
    except OSError as error:
        raise RuntimeError("Unable to securely configure session permissions") from error


def _running_on_windows() -> bool:
    """Keep the platform boundary injectable for focused permission tests."""
    return os.name == "nt"


def _harden_windows_permissions(path: Path) -> None:
    """Replace and verify a protected DACL that grants only the process token SID."""
    try:
        _replace_windows_dacl(path, is_directory=path.is_dir())
        if not _windows_dacl_is_current_user_only(path, is_directory=path.is_dir()):
            raise RuntimeError("Windows DACL verification failed")
    except (OSError, RuntimeError) as error:
        raise RuntimeError("Unable to securely configure session permissions") from error


def _replace_windows_dacl(path: Path, *, is_directory: bool) -> None:
    """Write a protected DACL with exactly one current-user allow ACE."""
    advapi32, _kernel32 = _windows_security_libraries()
    sid_buffer = _windows_current_user_sid(advapi32, _kernel32)
    sid = ctypes.c_void_p(ctypes.addressof(sid_buffer))
    sid_length = advapi32.GetLengthSid(sid)
    if sid_length == 0:
        _raise_windows_error("GetLengthSid")
    ace_size = _align_dword(8 + sid_length)
    acl_buffer = ctypes.create_string_buffer(8 + ace_size)
    acl = ctypes.c_void_p(ctypes.addressof(acl_buffer))
    if not advapi32.InitializeAcl(acl, ctypes.sizeof(acl_buffer), 2):
        _raise_windows_error("InitializeAcl")
    ace_flags = 0x03 if is_directory else 0
    if not advapi32.AddAccessAllowedAceEx(acl, 2, ace_flags, 0x10000000, sid):
        _raise_windows_error("AddAccessAllowedAceEx")
    result = advapi32.SetNamedSecurityInfoW(
        str(path), 1, 0x00000004 | 0x80000000, None, None, acl, None
    )
    if result != 0:
        raise OSError(result, "SetNamedSecurityInfoW failed")


def _windows_dacl_is_current_user_only(path: Path, *, is_directory: bool) -> bool:
    """Read back every ACE and accept only the expected current-user protected DACL."""
    advapi32, kernel32 = _windows_security_libraries()
    sid_buffer = _windows_current_user_sid(advapi32, kernel32)
    current_sid = ctypes.c_void_p(ctypes.addressof(sid_buffer))
    dacl = ctypes.c_void_p()
    descriptor = ctypes.c_void_p()
    result = advapi32.GetNamedSecurityInfoW(
        str(path), 1, 0x00000004, None, None, ctypes.byref(dacl), None, ctypes.byref(descriptor)
    )
    if result != 0:
        raise OSError(result, "GetNamedSecurityInfoW failed")
    try:
        if not dacl.value:
            return False
        info = _AclSizeInformation()
        if not advapi32.GetAclInformation(dacl, ctypes.byref(info), ctypes.sizeof(info), 2):
            _raise_windows_error("GetAclInformation")
        if info.ace_count == 0:
            return False
        ace_flags: list[int] = []
        for index in range(info.ace_count):
            ace = ctypes.c_void_p()
            if not advapi32.GetAce(dacl, index, ctypes.byref(ace)):
                _raise_windows_error("GetAce")
            if not ace.value:
                return False
            ace_type = ctypes.c_ubyte.from_address(ace.value).value
            flags = ctypes.c_ubyte.from_address(ace.value + 1).value
            access_mask = ctypes.c_uint32.from_address(ace.value + 4).value
            ace_sid = ctypes.c_void_p(ace.value + 8)
            if (
                ace_type != 0
                or access_mask == 0
                or not advapi32.EqualSid(current_sid, ace_sid)
            ):
                return False
            ace_flags.append(flags)
        if not is_directory:
            return ace_flags == [0]
        return 0 in ace_flags and any(flags & 0x03 for flags in ace_flags)
    finally:
        if descriptor.value:
            kernel32.LocalFree(descriptor)


class _AclSizeInformation(ctypes.Structure):
    _fields_ = [
        ("ace_count", wintypes.DWORD),
        ("acl_bytes_in_use", wintypes.DWORD),
        ("acl_bytes_free", wintypes.DWORD),
    ]


def _windows_security_libraries():
    """Load only the system security DLLs; no executable lookup is involved."""
    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _configure_windows_security_api(advapi32, kernel32)
    return advapi32, kernel32


def _configure_windows_security_api(advapi32, kernel32) -> None:
    """Declare pointer-safe signatures for the Windows Security API calls used here."""
    void_pointer = ctypes.c_void_p
    dword = wintypes.DWORD
    advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, dword, ctypes.POINTER(void_pointer)]
    advapi32.OpenProcessToken.restype = wintypes.BOOL
    advapi32.GetTokenInformation.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        void_pointer,
        dword,
        ctypes.POINTER(dword),
    ]
    advapi32.GetTokenInformation.restype = wintypes.BOOL
    advapi32.GetLengthSid.argtypes = [void_pointer]
    advapi32.GetLengthSid.restype = dword
    advapi32.CopySid.argtypes = [dword, void_pointer, void_pointer]
    advapi32.CopySid.restype = wintypes.BOOL
    advapi32.InitializeAcl.argtypes = [void_pointer, dword, dword]
    advapi32.InitializeAcl.restype = wintypes.BOOL
    advapi32.AddAccessAllowedAceEx.argtypes = [void_pointer, dword, dword, dword, void_pointer]
    advapi32.AddAccessAllowedAceEx.restype = wintypes.BOOL
    advapi32.SetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        dword,
        dword,
        void_pointer,
        void_pointer,
        void_pointer,
        void_pointer,
    ]
    advapi32.SetNamedSecurityInfoW.restype = dword
    advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPWSTR,
        dword,
        dword,
        ctypes.POINTER(void_pointer),
        ctypes.POINTER(void_pointer),
        ctypes.POINTER(void_pointer),
        ctypes.POINTER(void_pointer),
        ctypes.POINTER(void_pointer),
    ]
    advapi32.GetNamedSecurityInfoW.restype = dword
    advapi32.GetAclInformation.argtypes = [void_pointer, void_pointer, dword, dword]
    advapi32.GetAclInformation.restype = wintypes.BOOL
    advapi32.GetAce.argtypes = [void_pointer, dword, ctypes.POINTER(void_pointer)]
    advapi32.GetAce.restype = wintypes.BOOL
    advapi32.EqualSid.argtypes = [void_pointer, void_pointer]
    advapi32.EqualSid.restype = wintypes.BOOL
    kernel32.GetCurrentProcess.argtypes = []
    kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.LocalFree.argtypes = [void_pointer]
    kernel32.LocalFree.restype = void_pointer


def _windows_current_user_sid(advapi32, kernel32):
    """Copy the current process token's user SID into Python-owned memory."""
    token = wintypes.HANDLE()
    if not advapi32.OpenProcessToken(kernel32.GetCurrentProcess(), 0x0008, ctypes.byref(token)):
        _raise_windows_error("OpenProcessToken")
    try:
        required_size = wintypes.DWORD()
        advapi32.GetTokenInformation(token, 1, None, 0, ctypes.byref(required_size))
        if required_size.value == 0:
            _raise_windows_error("GetTokenInformation")
        token_user = ctypes.create_string_buffer(required_size.value)
        if not advapi32.GetTokenInformation(
            token,
            1,
            ctypes.cast(token_user, ctypes.c_void_p),
            required_size,
            ctypes.byref(required_size),
        ):
            _raise_windows_error("GetTokenInformation")
        source_sid = ctypes.cast(token_user, ctypes.POINTER(ctypes.c_void_p)).contents.value
        if not source_sid:
            raise RuntimeError("Current Windows user SID is unavailable")
        sid_length = advapi32.GetLengthSid(ctypes.c_void_p(source_sid))
        if sid_length == 0:
            _raise_windows_error("GetLengthSid")
        sid_buffer = ctypes.create_string_buffer(sid_length)
        if not advapi32.CopySid(
            sid_length,
            ctypes.c_void_p(ctypes.addressof(sid_buffer)),
            ctypes.c_void_p(source_sid),
        ):
            _raise_windows_error("CopySid")
        return sid_buffer
    finally:
        kernel32.CloseHandle(token)


def _align_dword(value: int) -> int:
    """Round a variable-size ACL record to the Windows DWORD boundary."""
    return (value + 3) & ~3


def _raise_windows_error(operation: str) -> None:
    """Raise the Windows API failure while preserving its system error code."""
    raise OSError(ctypes.get_last_error(), f"{operation} failed")


def _read_key(key_path: Path) -> bytes:
    """Read an atomically published session key with no partial-file retry path."""
    try:
        key = key_path.read_bytes()
    except OSError as error:
        raise RuntimeError("Unable to securely read the session key") from error
    if len(key) != 32:
        raise ValueError("Session key file has an invalid length")
    return key
