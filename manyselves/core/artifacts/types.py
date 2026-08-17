"""Artifact identity and format contracts.

The artifact tools deliberately make format decisions from bytes before a
caller is allowed to decode text.  Keeping the MIME/extension table here
gives the gateway, parsers and file tools one small, deterministic authority
and prevents a binary workbook or image from reaching ``Path.read_text``.
"""

from __future__ import annotations

import hashlib
import mimetypes
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Mapping


ArtifactKind = Literal[
    "text",
    "document",
    "spreadsheet",
    "pdf",
    "image",
    "binary",
    "manual_required",
]


# MIME values used by the project.  ``mimetypes`` is platform dependent (and
# often missing xlsx/docx entries), so known project formats take precedence.
EXTENSION_MEDIA_TYPES: dict[str, str] = {
    ".txt": "text/plain",
    ".text": "text/plain",
    ".md": "text/markdown",
    ".markdown": "text/markdown",
    ".rst": "text/x-rst",
    ".log": "text/plain",
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".ndjson": "application/x-ndjson",
    ".csv": "text/csv",
    ".tsv": "text/tab-separated-values",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".xml": "application/xml",
    ".html": "text/html",
    ".htm": "text/html",
    ".py": "text/x-python",
    ".js": "text/javascript",
    ".ts": "text/typescript",
    ".toml": "application/toml",
    ".ini": "text/plain",
    ".cfg": "text/plain",
    ".conf": "text/plain",
    ".sql": "application/sql",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".docm": "application/vnd.ms-word.document.macroEnabled.12",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.12",
    ".xls": "application/vnd.ms-excel",
    ".pdf": "application/pdf",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".tif": "image/tiff",
    ".tiff": "image/tiff",
    ".ico": "image/x-icon",
    ".svg": "image/svg+xml",
    ".zip": "application/zip",
    ".gz": "application/gzip",
    ".tar": "application/x-tar",
    ".7z": "application/x-7z-compressed",
    ".rar": "application/vnd.rar",
    ".bin": "application/octet-stream",
}


MAGIC_MEDIA_TYPES: tuple[tuple[bytes, str], ...] = (
    (b"%PDF-", "application/pdf"),
    (b"\x89PNG\r\n\x1a\n", "image/png"),
    (b"\xff\xd8\xff", "image/jpeg"),
    (b"GIF87a", "image/gif"),
    (b"GIF89a", "image/gif"),
    (b"BM", "image/bmp"),
    (b"II*\x00", "image/tiff"),
    (b"MM\x00*", "image/tiff"),
    (b"PK\x03\x04", "application/zip"),
    (b"PK\x05\x06", "application/zip"),
    (b"PK\x07\x08", "application/zip"),
    (b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1", "application/x-ole-storage"),
)

# Compatibility names used in audit/fixture code.
MIME_BY_EXTENSION = EXTENSION_MEDIA_TYPES
MAGIC_MIME_TYPES = MAGIC_MEDIA_TYPES


TEXT_MEDIA_TYPES = frozenset(
    {
        "text/plain",
        "text/markdown",
        "text/x-rst",
        "text/csv",
        "text/tab-separated-values",
        "text/html",
        "text/javascript",
        "text/typescript",
        "text/x-python",
        "application/json",
        "application/x-ndjson",
        "application/yaml",
        "application/xml",
        "application/toml",
        "application/sql",
    }
)


_KNOWN_OPERATIONS: frozenset[str] = frozenset(
    {
        "open_artifact",
        "search_text",
        "inspect_document",
        "inspect_image",
        "inspect_pdf",
        "manual_review",
    }
)


def _normalise_media_type(media_type: str) -> str:
    value = str(media_type or "").strip().casefold()
    return value or "application/octet-stream"


def media_type_for_path(path: str | Path, data: bytes | None = None) -> str:
    """Return a deterministic MIME type using magic bytes and extension.

    Magic signatures win for unambiguous formats.  Office Open XML files use
    a ZIP container, so a known Office extension wins over the generic ZIP
    magic value.  A caller may pass ``data`` to avoid a second read when it
    already has bytes; no text decoding occurs here.
    """

    target = Path(path)
    extension_type = EXTENSION_MEDIA_TYPES.get(target.suffix.casefold())
    prefix = data[:64] if data is not None else b""
    if data is None and target.is_file():
        try:
            with target.open("rb") as handle:
                prefix = handle.read(64)
        except OSError:
            prefix = b""

    for magic, media_type in MAGIC_MEDIA_TYPES:
        if prefix.startswith(magic):
            # OOXML is a ZIP container; retain its semantic MIME by suffix.
            if media_type == "application/zip" and extension_type in {
                EXTENSION_MEDIA_TYPES.get(".docx"),
                EXTENSION_MEDIA_TYPES.get(".docm"),
                EXTENSION_MEDIA_TYPES.get(".xlsx"),
                EXTENSION_MEDIA_TYPES.get(".xlsm"),
            }:
                return extension_type  # type: ignore[return-value]
            return media_type
    if extension_type:
        return extension_type
    guessed, _encoding = mimetypes.guess_type(str(target), strict=False)
    return _normalise_media_type(guessed)


# Short aliases are useful to callers that used the older audit vocabulary.
detect_media_type = media_type_for_path
mime_for_path = media_type_for_path


def _looks_like_utf8_text(data: bytes) -> bool:
    # NUL-containing payloads are binary even when a decoder happens to accept
    # them.  Decode strictly so malformed ``.txt`` files do not enter text
    # tools and produce the old 103-error UTF-8 loop.
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def kind_for_media_type(media_type: str, *, path: str | Path | None = None, data: bytes | None = None) -> ArtifactKind:
    """Map a MIME value (and optional extension/bytes) to an artifact kind."""

    media_type = _normalise_media_type(media_type)
    suffix = Path(path).suffix.casefold() if path is not None else ""
    if media_type == "application/pdf":
        return "pdf"
    if media_type.startswith("image/"):
        return "image"
    if media_type in {
        EXTENSION_MEDIA_TYPES[".xlsx"],
        EXTENSION_MEDIA_TYPES[".xlsm"],
        EXTENSION_MEDIA_TYPES[".xls"],
    } or suffix in {".xlsx", ".xlsm", ".xls"}:
        return "spreadsheet"
    if media_type in {
        EXTENSION_MEDIA_TYPES[".docx"],
        EXTENSION_MEDIA_TYPES[".docm"],
    } or suffix in {".docx", ".docm", ".odt"}:
        return "document"
    if media_type in TEXT_MEDIA_TYPES or media_type.startswith("text/"):
        if data is None or _looks_like_utf8_text(data):
            return "text"
        return "binary"
    if data is not None and _looks_like_utf8_text(data) and suffix not in {
        ".zip",
        ".gz",
        ".tar",
        ".7z",
        ".rar",
        ".bin",
    }:
        return "text"
    if suffix in {
        ".bin",
        ".pyc",
        ".pyd",
        ".so",
        ".dll",
        ".exe",
        ".zip",
        ".gz",
        ".tar",
        ".7z",
        ".rar",
        ".mp3",
        ".mp4",
        ".wav",
        ".avi",
        ".mov",
        ".ttf",
        ".otf",
        ".woff",
        ".woff2",
    }:
        return "binary"
    return "manual_required" if media_type == "application/octet-stream" else "binary"


artifact_kind_for_media_type = kind_for_media_type


def default_allowed_operations(kind: ArtifactKind) -> tuple[str, ...]:
    """Return the registered tools that are meaningful for *kind*.

    The gateway still enforces the operation at execution time.  This list is
    a capability descriptor for prompt/schema construction, not a global
    allowlist.  Document and spreadsheet entries retain ``open_artifact`` and
    ``search_text`` for legacy declarations; their format-aware readers must
    be selected before a text decode is attempted.
    """

    if kind == "text":
        return ("open_artifact", "search_text")
    if kind in {"document", "spreadsheet", "pdf"}:
        return ("open_artifact", "search_text", "inspect_document")
    if kind == "image":
        return ("inspect_image",)
    return ()


def infer_logical_role(path: str | Path | None, *, kind: ArtifactKind = "text") -> str:
    """Derive a non-authoritative role label from project path components."""

    if path is None:
        return "internal_artifact"
    parts = [part.casefold() for part in Path(path).parts]
    if "inputs" in parts or "input" in parts:
        return "input"
    if "knowledge" in parts:
        return "knowledge"
    if "templates" in parts or "template" in parts:
        return "template"
    if "context" in parts:
        return "context"
    if "reviews" in parts or "review" in parts:
        return "review"
    if "outputs" in parts or "output" in parts:
        return "output"
    if "artifacts" in parts or "tool-results" in parts:
        return "tool_result"
    return "binary" if kind in {"binary", "manual_required"} else "artifact"


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    """Strict, immutable identity and capability metadata for one artifact."""

    canonical_ref: str = ""
    path: str | None = None
    media_type: str = "application/octet-stream"
    kind: ArtifactKind = "manual_required"
    logical_role: str = "artifact"
    allowed_operations: tuple[str, ...] = ()
    sha256: str = "0" * 64
    size_bytes: int = 0
    required_tool: str | None = None

    def __post_init__(self) -> None:
        if not str(self.canonical_ref).strip():
            raise ValueError("canonical_ref is required")
        if self.path is not None and not str(self.path).strip():
            raise ValueError("path must be non-empty when provided")
        if self.path is not None:
            object.__setattr__(self, "path", str(self.path))
        if self.kind not in {
            "text",
            "document",
            "spreadsheet",
            "pdf",
            "image",
            "binary",
            "manual_required",
        }:
            raise ValueError(f"unsupported artifact kind: {self.kind!r}")
        media_type = _normalise_media_type(self.media_type)
        object.__setattr__(self, "media_type", media_type)
        operations = tuple(str(op).strip() for op in self.allowed_operations if str(op).strip())
        unknown = sorted(set(operations) - _KNOWN_OPERATIONS)
        if unknown:
            raise ValueError(f"unknown artifact operations: {unknown}")
        if len(set(operations)) != len(operations):
            raise ValueError("allowed_operations must not contain duplicates")
        object.__setattr__(self, "allowed_operations", operations)
        if not isinstance(self.size_bytes, int) or isinstance(self.size_bytes, bool) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")
        digest = str(self.sha256).casefold()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("sha256 must be a 64-character hexadecimal digest")
        object.__setattr__(self, "sha256", digest)
        if self.required_tool is not None and self.required_tool not in _KNOWN_OPERATIONS:
            raise ValueError(f"unknown required_tool: {self.required_tool}")

    @property
    def opaque(self) -> bool:
        return self.canonical_ref.startswith("artifact:v1:")

    def allows(self, operation: str) -> bool:
        aliases = {
            "open": "open_artifact",
            "search": "search_text",
            "inspect": self.required_tool or "inspect_document",
        }
        return aliases.get(operation, operation) in self.allowed_operations

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_ref": self.canonical_ref,
            "path": self.path,
            "media_type": self.media_type,
            "kind": self.kind,
            "logical_role": self.logical_role,
            "allowed_operations": list(self.allowed_operations),
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
            "required_tool": self.required_tool,
        }

    to_dict = as_dict

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> "ArtifactDescriptor":
        values = dict(payload)
        values["allowed_operations"] = tuple(values.get("allowed_operations") or ())
        return cls(**values)


def descriptor_for_path(
    path: str | Path,
    *,
    canonical_ref: str | None = None,
    logical_role: str | None = None,
    data: bytes | None = None,
) -> ArtifactDescriptor:
    """Build an immutable descriptor for an existing path.

    The function hashes bytes directly.  It intentionally never invokes
    ``read_text``; malformed UTF-8 is classified as binary/manual-required.
    """

    target = Path(path)
    if data is None:
        data = target.read_bytes()
    media_type = media_type_for_path(target, data)
    kind = kind_for_media_type(media_type, path=target, data=data)
    if kind == "text" and not _looks_like_utf8_text(data):
        kind = "binary"
    required_tool = {
        "document": "inspect_document",
        "spreadsheet": "inspect_document",
        "pdf": "inspect_document",
        "image": "inspect_image",
        "binary": "manual_review",
        "manual_required": "manual_review",
    }.get(kind)
    return ArtifactDescriptor(
        canonical_ref=canonical_ref or target.as_posix(),
        path=target.as_posix(),
        media_type=media_type,
        kind=kind,
        logical_role=logical_role or infer_logical_role(target, kind=kind),
        allowed_operations=default_allowed_operations(kind),
        sha256=hashlib.sha256(data).hexdigest(),
        size_bytes=len(data),
        required_tool=required_tool,
    )


build_artifact_descriptor = descriptor_for_path
describe_path = descriptor_for_path
classify_artifact = descriptor_for_path


__all__ = [
    "ArtifactDescriptor",
    "ArtifactKind",
    "EXTENSION_MEDIA_TYPES",
    "MAGIC_MEDIA_TYPES",
    "MAGIC_MIME_TYPES",
    "MIME_BY_EXTENSION",
    "TEXT_MEDIA_TYPES",
    "artifact_kind_for_media_type",
    "build_artifact_descriptor",
    "default_allowed_operations",
    "descriptor_for_path",
    "describe_path",
    "detect_media_type",
    "infer_logical_role",
    "kind_for_media_type",
    "classify_artifact",
    "media_type_for_path",
    "mime_for_path",
]
