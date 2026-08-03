"""Signed local-browser sessions backed by a process-independent key file."""

import base64
import hashlib
import hmac
import json
import os
import secrets
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
    """Read a stable key, creating its owner-only file exactly once."""
    key_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return _read_key(key_path)

    key = secrets.token_bytes(32)
    try:
        with os.fdopen(descriptor, "wb") as key_file:
            key_file.write(key)
            key_file.flush()
            os.fsync(key_file.fileno())
        if os.name != "nt":
            os.chmod(key_path, 0o600)
    except BaseException:
        raise
    return key


def _read_key(key_path: Path) -> bytes:
    """Read a concurrently created key after its writer has completed creation."""
    for _attempt in range(20):
        key = key_path.read_bytes()
        if len(key) == 32:
            return key
        time.sleep(0.01)
    raise ValueError("Session key file has an invalid length")
