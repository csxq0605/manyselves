"""Signed local-browser sessions backed by a process-independent key file."""

import base64
import hashlib
import hmac
import json
import os
import secrets
import subprocess
import time
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
    try:
        _write_key(temporary_path, key)
        _harden_permissions(temporary_path, is_directory=False)
        try:
            os.link(temporary_path, key_path)
        except FileExistsError:
            _remove_temporary_key(temporary_path)
            _harden_permissions(key_path, is_directory=False)
            return _read_key(key_path)
        _remove_temporary_key(temporary_path)
        _harden_permissions(key_path, is_directory=False)
        return key
    except BaseException:
        _remove_temporary_key(temporary_path)
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
    """Replace inherited NTFS permissions with a DACL for the current user only."""
    try:
        current_user = subprocess.run(
            ["whoami"], capture_output=True, check=True, text=True
        ).stdout.strip()
        if not current_user:
            raise RuntimeError("Current Windows user is unavailable")
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"{current_user}:(F)"],
            capture_output=True,
            check=True,
            text=True,
        )
    except (OSError, subprocess.SubprocessError, RuntimeError) as error:
        raise RuntimeError("Unable to securely configure session permissions") from error


def _read_key(key_path: Path) -> bytes:
    """Read an atomically published session key with no partial-file retry path."""
    try:
        key = key_path.read_bytes()
    except OSError as error:
        raise RuntimeError("Unable to securely read the session key") from error
    if len(key) != 32:
        raise ValueError("Session key file has an invalid length")
    return key
