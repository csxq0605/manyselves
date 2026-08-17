"""Deterministic storage choices for report artifacts.

The reporting runtime writes two kinds of files:

* *materialized* files are ordinary, independently readable files.  They are
  written through a temporary file and are hash checked before publication.
* *CAS* files are ingested into :class:`ContentAddressedStore` and exposed at
  the requested path as a compatibility view.

This module intentionally contains policy only.  Existing callers of
``ContentAddressedStore.ingest_file`` keep their old behaviour; callers that
want the selective policy opt in through ``persist_with_policy``.
"""

from __future__ import annotations

import mimetypes
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, ClassVar


class StorageMode(str, Enum):
    """The two physical storage modes supported by the G1 policy."""

    MATERIALIZED = "materialized"
    CAS = "cas"

    # Lower-case aliases make the enum pleasant to use from configuration and
    # retain the spelling used in the design notes (``StorageMode.materialized``).
    materialized = MATERIALIZED
    cas = CAS


@dataclass(frozen=True, slots=True)
class StorageDecision:
    """A policy decision, including enough information for an audit record."""

    mode: StorageMode
    policy_version: str
    logical_role: str
    size_bytes: int
    media_type: str
    rationale: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", StorageMode(self.mode))
        role = str(self.logical_role).strip()
        if not role:
            raise ValueError("logical_role must be non-empty")
        object.__setattr__(self, "logical_role", role)
        version = str(self.policy_version).strip()
        if not version:
            raise ValueError("policy_version must be non-empty")
        object.__setattr__(self, "policy_version", version)
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        object.__setattr__(self, "media_type", _normalise_media_type(self.media_type))
        rationale = str(self.rationale).strip()
        if not rationale:
            raise ValueError("rationale must be non-empty")
        object.__setattr__(self, "rationale", rationale)

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible policy record."""

        return {
            "mode": self.mode.value,
            "policy_version": self.policy_version,
            "logical_role": self.logical_role,
            "size_bytes": self.size_bytes,
            "media_type": self.media_type,
            "rationale": self.rationale,
        }

    # ``to_record`` is deliberately a tiny convenience rather than a second
    # schema; it is useful to callers persisting a decision beside a receipt.
    to_record = as_dict


@dataclass(frozen=True, slots=True)
class StoredArtifact:
    """Result returned by an explicit policy-backed persistence operation."""

    storage_mode: StorageMode
    sha256: str
    size_bytes: int
    view_path: Path
    blob_ref: Path | None = None
    opaque_ref: str | None = None
    policy_version: str = "cas-policy-v1"

    def __post_init__(self) -> None:
        object.__setattr__(self, "storage_mode", StorageMode(self.storage_mode))
        object.__setattr__(self, "view_path", Path(self.view_path))
        digest = str(self.sha256).casefold()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "sha256", digest)
        if (
            not isinstance(self.size_bytes, int)
            or isinstance(self.size_bytes, bool)
            or self.size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")
        object.__setattr__(self, "policy_version", str(self.policy_version).strip())
        if not self.policy_version:
            raise ValueError("policy_version must be non-empty")
        if self.blob_ref is not None:
            object.__setattr__(self, "blob_ref", Path(self.blob_ref))
        if self.storage_mode is StorageMode.MATERIALIZED and (
            self.blob_ref is not None or self.opaque_ref is not None
        ):
            raise ValueError("materialized artifacts must not carry CAS references")

    @property
    def mode(self) -> StorageMode:
        """Compatibility spelling for callers that use ``decision.mode``."""

        return self.storage_mode

    def as_dict(self) -> dict[str, Any]:
        return {
            "storage_mode": self.storage_mode.value,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "view_path": self.view_path.as_posix(),
            "blob_ref": self.blob_ref.as_posix() if self.blob_ref is not None else None,
            "opaque_ref": self.opaque_ref,
            "policy_version": self.policy_version,
        }


def _normalise_role(value: str) -> str:
    text = str(value or "").strip().casefold()
    for separator in ("-", " ", "/", ".", ":"):
        text = text.replace(separator, "_")
    while "__" in text:
        text = text.replace("__", "_")
    return text.strip("_")


def _normalise_suffix(value: str | Path | None) -> str:
    suffix = Path(value).suffix if value is not None else ""
    # ``suffix`` is a public keyword and callers often pass ``"docx"`` rather
    # than ``".docx"``.  Path("docx").suffix is empty, so handle both forms.
    if value is not None and not suffix:
        suffix = str(value).strip()
    suffix = str(suffix).casefold()
    return suffix if suffix.startswith(".") else f".{suffix}" if suffix else ""


def _normalise_media_type(value: str | None) -> str:
    media_type = str(value or "").strip().casefold()
    return media_type or "application/octet-stream"


def _media_type_for_source(source: Path | None, suffix: str) -> str:
    if source is not None and source.is_file():
        guessed, _encoding = mimetypes.guess_type(str(source), strict=False)
        if guessed:
            return _normalise_media_type(guessed)
    guessed, _encoding = mimetypes.guess_type(f"artifact{suffix}", strict=False)
    return _normalise_media_type(guessed)


@dataclass(frozen=True, slots=True)
class CasPolicy:
    """Default selective-CAS policy.

    Role rules are evaluated first.  A known lifecycle role therefore cannot
    accidentally become a CAS blob merely because a JSON state file is large.
    Unknown roles then use the size and suffix rules.  The threshold is kept
    deliberately explicit and versioned so a receipt can explain why a file
    was stored one way or the other.
    """

    policy_version: str = "cas-policy-v1"
    large_size_bytes: int = 1024 * 1024

    # Roles whose bytes are valuable immutable inputs or user-facing outputs.
    _cas_roles: ClassVar[frozenset[str]] = frozenset(
        {
            "input",
            "input_snapshot",
            "input_snapshots",
            "source_snapshot",
            "template",
            "template_snapshot",
            "template_asset",
            "photo",
            "photos",
            "image",
            "images",
            "photo_asset",
            "final_docx",
            "final_report",
            "final_output",
            "delivery_docx",
        }
    )
    # Lifecycle metadata remains independently readable even if a caller
    # passes a generous size.  ``result`` and ``evidence`` are intentionally
    # absent: only their large forms belong in CAS by default.
    _materialized_roles: ClassVar[frozenset[str]] = frozenset(
        {
            "json",
            "state",
            "ledger",
            "review",
            "barrier",
            "receipt",
            "manifest",
            "checkpoint",
            "event",
            "events",
            "trace",
            "metadata",
            "usage",
        }
    )

    # These suffixes identify immutable/binary content even when the caller
    # uses a generic role.  JSON/state suffixes are handled below the size
    # threshold and large files still follow the size rule.
    _always_cas_suffixes: ClassVar[frozenset[str]] = frozenset(
        {
            ".docx",
            ".docm",
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".webp",
            ".tif",
            ".tiff",
            ".ico",
        }
    )
    _large_content_suffixes: ClassVar[frozenset[str]] = frozenset(
        {
            ".bin",
            ".dat",
            ".pdf",
            ".xlsx",
            ".xlsm",
            ".xls",
            ".zip",
            ".gz",
            ".tar",
            ".7z",
            ".rar",
        }
    )
    _materialized_suffixes: ClassVar[frozenset[str]] = frozenset(
        {
            ".json",
            ".jsonl",
            ".ndjson",
            ".state",
            ".ledger",
            ".review",
            ".barrier",
            ".receipt",
            ".manifest",
            ".yaml",
            ".yml",
            ".toml",
        }
    )

    def __post_init__(self) -> None:
        version = str(self.policy_version).strip()
        if not version:
            raise ValueError("policy_version must be non-empty")
        object.__setattr__(self, "policy_version", version)
        if (
            not isinstance(self.large_size_bytes, int)
            or isinstance(self.large_size_bytes, bool)
            or self.large_size_bytes < 0
        ):
            raise ValueError("large_size_bytes must be a non-negative integer")

    def decide(
        self,
        source: Path | None = None,
        *,
        logical_role: str,
        size_bytes: int | None = None,
        media_type: str | None = None,
        suffix: str | None = None,
    ) -> StorageDecision:
        """Choose a mode without reading or writing any artifact bytes."""

        source_path = Path(source) if source is not None else None
        role = _normalise_role(logical_role)
        if not role:
            raise ValueError("logical_role must be non-empty")

        if size_bytes is None and source_path is not None and source_path.is_file():
            size_bytes = source_path.stat().st_size
        if size_bytes is None:
            size_bytes = 0
        if (
            not isinstance(size_bytes, int)
            or isinstance(size_bytes, bool)
            or size_bytes < 0
        ):
            raise ValueError("size_bytes must be a non-negative integer")

        normalized_suffix = _normalise_suffix(
            suffix if suffix is not None else (source_path.suffix if source_path else "")
        )
        normalized_media_type = _normalise_media_type(
            media_type
            if media_type is not None
            else _media_type_for_source(source_path, normalized_suffix)
        )

        if role in self._cas_roles:
            return self._decision(
                StorageMode.CAS,
                role,
                size_bytes,
                normalized_media_type,
                f"logical_role={role}:immutable-content",
            )
        if role in self._materialized_roles:
            return self._decision(
                StorageMode.MATERIALIZED,
                role,
                size_bytes,
                normalized_media_type,
                f"logical_role={role}:lifecycle-metadata",
            )

        # A large result/evidence/binary is CAS regardless of whether it uses
        # a text or binary suffix.  This is intentionally before suffix rules:
        # a large JSON evidence payload is still expensive duplicate content.
        if size_bytes >= self.large_size_bytes and size_bytes > 0:
            return self._decision(
                StorageMode.CAS,
                role,
                size_bytes,
                normalized_media_type,
                f"size_bytes>={self.large_size_bytes}",
            )
        if normalized_suffix in self._always_cas_suffixes or normalized_media_type.startswith(
            "image/"
        ):
            return self._decision(
                StorageMode.CAS,
                role,
                size_bytes,
                normalized_media_type,
                f"suffix={normalized_suffix or normalized_media_type}:immutable-content",
            )
        if normalized_suffix in self._materialized_suffixes:
            return self._decision(
                StorageMode.MATERIALIZED,
                role,
                size_bytes,
                normalized_media_type,
                f"suffix={normalized_suffix}:small-metadata",
            )
        # Binary/document suffixes below the threshold are kept materialized;
        # size, not extension alone, controls the expensive-content decision.
        if normalized_suffix in self._large_content_suffixes:
            return self._decision(
                StorageMode.MATERIALIZED,
                role,
                size_bytes,
                normalized_media_type,
                f"suffix={normalized_suffix}:below-size-threshold",
            )
        return self._decision(
            StorageMode.MATERIALIZED,
            role,
            size_bytes,
            normalized_media_type,
            "default:small-or-unknown-content",
        )

    __call__ = decide

    def _decision(
        self,
        mode: StorageMode,
        role: str,
        size_bytes: int,
        media_type: str,
        rationale: str,
    ) -> StorageDecision:
        return StorageDecision(
            mode=mode,
            policy_version=self.policy_version,
            logical_role=role,
            size_bytes=size_bytes,
            media_type=media_type,
            rationale=rationale,
        )


__all__ = [
    "CasPolicy",
    "StorageDecision",
    "StorageMode",
    "StoredArtifact",
]
