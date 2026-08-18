"""Context-driven sanitization for public event payloads."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from enum import Enum, auto
from pathlib import PosixPath, WindowsPath
from typing import Any

from pydantic import BaseModel, SecretBytes, SecretStr

REDACTED = "[REDACTED]"
MAX_SANITIZE_DEPTH = 32

TOKEN_METRIC_KEYS = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "prompttokens",
        "completiontokens",
        "totaltokens",
        "cachedtokens",
        "reasoningtokens",
        "maxtokens",
        "maxtotaltokens",
        "workingmemorytokens",
        "tokensin",
        "tokensout",
        "tokencount",
        "agentcumulativeinputtokens",
        "agentcumulativeoutputtokens",
    }
)
AUTH_METADATA_KEYS = frozenset(
    {"method", "scheme", "type", "provider", "description", "configured", "enabled"}
)
AUTH_CONTAINER_TYPES = (dict, list, tuple, set, frozenset)


class _Context(Enum):
    GENERIC = auto()
    AUTHENTICATION = auto()
    TOKEN_USAGE = auto()


class EventPayloadSanitizer:
    """Sanitize one message dump using explicit field contexts."""

    def sanitize_mapping(self, value: dict[object, object]) -> dict[str, Any]:
        result = self._sanitize(value, context=_Context.GENERIC, depth=0, active=set())
        if type(result) is not dict:
            raise TypeError("Sanitized event payload must be an object")
        return result

    def sanitize_field(self, name: object, value: object) -> Any:
        return self._sanitize_field(name, value, depth=0, active=set())

    def _sanitize_field(
        self,
        name: object,
        value: object,
        *,
        depth: int,
        active: set[int],
    ) -> Any:
        if depth >= MAX_SANITIZE_DEPTH:
            return REDACTED
        key = _normalized_key(name)
        if key is None:
            return REDACTED
        if _is_secret_key(key):
            return REDACTED
        if key == "tokenusage":
            if value is None:
                return None
            if type(value) in (dict, list, tuple):
                return self._sanitize(
                    value,
                    context=_Context.TOKEN_USAGE,
                    depth=depth,
                    active=active,
                )
            return REDACTED
        if "token" in key:
            if key in TOKEN_METRIC_KEYS and _is_metric_value(value):
                return value
            return REDACTED
        if _is_auth_boundary(name):
            if type(value) in AUTH_CONTAINER_TYPES:
                return self._sanitize(
                    value,
                    context=_Context.AUTHENTICATION,
                    depth=depth,
                    active=active,
                )
            return REDACTED
        return self._sanitize(
            value,
            context=_Context.GENERIC,
            depth=depth,
            active=active,
        )

    def _sanitize(
        self,
        value: object,
        *,
        context: _Context,
        depth: int,
        active: set[int],
    ) -> Any:
        if depth >= MAX_SANITIZE_DEPTH:
            return REDACTED
        if type(value) in AUTH_CONTAINER_TYPES and id(value) in active:
            return REDACTED
        if context is _Context.AUTHENTICATION:
            return self._sanitize_auth(value, depth=depth, active=active)
        if context is _Context.TOKEN_USAGE:
            return self._sanitize_token_usage(value, depth=depth, active=active)
        return self._sanitize_generic(value, depth=depth, active=active)

    def _sanitize_generic(
        self,
        value: object,
        *,
        depth: int,
        active: set[int],
    ) -> Any:
        if isinstance(value, (SecretStr, SecretBytes)):
            return REDACTED
        if isinstance(value, BaseModel):
            if id(value) in active:
                return REDACTED
            active.add(id(value))
            try:
                dumped = value.model_dump(mode="python")
                return self._sanitize(
                    dumped,
                    context=_Context.GENERIC,
                    depth=depth + 1,
                    active=active,
                )
            except Exception:
                return REDACTED
            finally:
                active.remove(id(value))
        if type(value) is dict:
            return self._sanitize_generic_mapping(value, depth=depth, active=active)
        if type(value) in (list, tuple):
            return self._sanitize_sequence(
                value,
                context=_Context.GENERIC,
                depth=depth,
                active=active,
            )
        if type(value) in (set, frozenset):
            sanitized = self._sanitize_sequence(
                value,
                context=_Context.GENERIC,
                depth=depth,
                active=active,
            )
            return sorted(sanitized, key=_deterministic_sort_key)
        if isinstance(value, datetime):
            return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
        if isinstance(value, Enum):
            return self._sanitize(
                value.value,
                context=_Context.GENERIC,
                depth=depth + 1,
                active=active,
            )
        if type(value) in (PosixPath, WindowsPath):
            return str(value)
        if type(value) is str:
            return _redact_credential_text(value)
        if type(value) is bytes:
            return _redact_credential_text(value.decode("utf-8", errors="replace"))
        if value is None or type(value) in (bool, int, float):
            return value
        return REDACTED

    def _sanitize_generic_mapping(
        self,
        value: dict[object, object],
        *,
        depth: int,
        active: set[int],
    ) -> dict[str, Any]:
        active.add(id(value))
        reserved = {key for key in value if type(key) is str}
        result: dict[str, Any] = {}
        try:
            for name, item in value.items():
                output_name = _output_key(name, reserved | set(result))
                result[output_name] = self._sanitize_field(
                    name,
                    item,
                    depth=depth + 1,
                    active=active,
                )
        finally:
            active.remove(id(value))
        return result

    def _sanitize_auth(
        self,
        value: object,
        *,
        depth: int,
        active: set[int],
    ) -> Any:
        if type(value) is dict:
            return self._sanitize_sensitive_mapping(
                value,
                context=_Context.AUTHENTICATION,
                depth=depth,
                active=active,
            )
        if type(value) in (list, tuple, set, frozenset):
            sanitized = self._sanitize_sensitive_sequence(
                value,
                context=_Context.AUTHENTICATION,
                depth=depth,
                active=active,
            )
            if type(value) in (set, frozenset):
                return sorted(sanitized, key=_deterministic_sort_key)
            return sanitized
        return REDACTED

    def _sanitize_token_usage(
        self,
        value: object,
        *,
        depth: int,
        active: set[int],
    ) -> Any:
        if type(value) is dict:
            return self._sanitize_sensitive_mapping(
                value,
                context=_Context.TOKEN_USAGE,
                depth=depth,
                active=active,
            )
        if type(value) in (list, tuple):
            return self._sanitize_sensitive_sequence(
                value,
                context=_Context.TOKEN_USAGE,
                depth=depth,
                active=active,
            )
        return REDACTED

    def _sanitize_sensitive_mapping(
        self,
        value: dict[object, object],
        *,
        context: _Context,
        depth: int,
        active: set[int],
    ) -> dict[str, Any]:
        active.add(id(value))
        reserved = {key for key in value if type(key) is str}
        result: dict[str, Any] = {}
        try:
            for name, item in value.items():
                output_name = _output_key(name, reserved | set(result))
                key = _normalized_key(name)
                scalar_within_depth = depth + 1 < MAX_SANITIZE_DEPTH
                allowed_containers = (
                    AUTH_CONTAINER_TYPES
                    if context is _Context.AUTHENTICATION
                    else (dict, list, tuple)
                )
                if context is _Context.TOKEN_USAGE and key in TOKEN_METRIC_KEYS:
                    result[output_name] = (
                        item
                        if scalar_within_depth and _is_metric_value(item)
                        else REDACTED
                    )
                elif type(item) in allowed_containers:
                    result[output_name] = self._sanitize(
                        item,
                        context=context,
                        depth=depth + 1,
                        active=active,
                    )
                elif context is _Context.AUTHENTICATION:
                    result[output_name] = (
                        _sanitize_auth_metadata(name, item)
                        if scalar_within_depth
                        else REDACTED
                    )
                else:
                    result[output_name] = REDACTED
        finally:
            active.remove(id(value))
        return result

    def _sanitize_sequence(
        self,
        value: list[object] | tuple[object, ...] | set[object] | frozenset[object],
        *,
        context: _Context,
        depth: int,
        active: set[int],
    ) -> list[Any]:
        active.add(id(value))
        try:
            return [
                self._sanitize(
                    item,
                    context=context,
                    depth=depth + 1,
                    active=active,
                )
                for item in value
            ]
        finally:
            active.remove(id(value))

    def _sanitize_sensitive_sequence(
        self,
        value: list[object] | tuple[object, ...] | set[object] | frozenset[object],
        *,
        context: _Context,
        depth: int,
        active: set[int],
    ) -> list[Any]:
        active.add(id(value))
        try:
            return [
                self._sanitize(
                    item,
                    context=context,
                    depth=depth + 1,
                    active=active,
                )
                if type(item) in AUTH_CONTAINER_TYPES
                else REDACTED
                for item in value
            ]
        finally:
            active.remove(id(value))


_SECRET_MARKERS = (
    "authorization",
    "header",
    "jwt",
    "password",
    "secret",
    "credential",
    "cookie",
    "apikey",
    "privatekey",
    "accesskey",
    "clientkey",
    "bearer",
)
_CREDENTIAL_TEXT = re.compile(
    r"(?i)\b(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+"
    r"|(?<![A-Za-z0-9])sk-[A-Za-z0-9_-]{8,}"
    r"|\bAKIA[A-Z0-9]{16}\b"
)


def _normalized_key(value: object) -> str | None:
    if type(value) is not str:
        return None
    return "".join(character for character in value.casefold() if character.isalnum())


def _is_auth_boundary(value: object) -> bool:
    if type(value) is not str:
        return False
    folded = value.casefold()
    for stem in ("auth", "authentication"):
        if folded == stem:
            return True
        if folded.startswith(tuple(f"{stem}{delimiter}" for delimiter in "_-.:/")):
            return True
        stem_prefix = value[: len(stem)]
        suffix = value[len(stem) :]
        if stem_prefix in {stem, stem.capitalize()} and suffix[:1].isupper():
            return True
        if (
            stem_prefix == stem.upper()
            and suffix[:1].isupper()
            and suffix[1:2].islower()
        ):
            return True
    return False


def _is_secret_key(key: str) -> bool:
    return not key or any(marker in key for marker in _SECRET_MARKERS)


def _is_metric_value(value: object) -> bool:
    return value is None or type(value) in (int, float)


def _is_auth_metadata_value(value: object) -> bool:
    return value is None or type(value) in (str, bool, int, float)


def _output_key(name: object, unavailable: set[str]) -> str:
    if type(name) is str:
        return name
    candidate = REDACTED
    suffix = 2
    while candidate in unavailable:
        candidate = f"{REDACTED}#{suffix}"
        suffix += 1
    return candidate


def _sanitize_auth_metadata(name: object, value: object) -> Any:
    if _normalized_key(name) not in AUTH_METADATA_KEYS or not _is_auth_metadata_value(value):
        return REDACTED
    return _redact_credential_text(value) if type(value) is str else value


def _redact_credential_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        parts = match.group(0).split(maxsplit=1)
        if len(parts) == 2 and parts[1].rstrip(".,;:!?").casefold() in {
            "authentication",
            "authorization",
            "scheme",
        }:
            return match.group(0)
        return REDACTED

    return _CREDENTIAL_TEXT.sub(replace, value)


def _deterministic_sort_key(value: Any) -> str:
    return repr(value)
