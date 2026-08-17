"""Hash-only ContextManifest v3 primitives.

The reporting runner has two kinds of context records: a task context record
and a per-Provider-call record.  Both records deliberately carry *references,
hashes and character counts only*.  This module owns the small amount of
accounting shared by the two records so a retry, a second revision, or two
concurrent workers cannot accidentally count another run's text as a repeat.

Older runners wrote v1/v2 manifests with ``prompt_components`` and ``messages``
arrays.  The readers in this module are intentionally permissive; writers in
the current runner emit v3 fields while retaining those legacy fields for
existing forensic tooling.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
from pathlib import Path
from typing import Any, Iterable, Literal, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SEGMENT_KINDS: tuple[str, ...] = (
    "system_prompt",
    "task_contract",
    "task_state_capsule",
    "knowledge_slice",
    "evidence_slice",
    "input_contract",
    "module_skill",
    "artifact_ref",
    "message",
    "tool_result",
    "completed_result_ref",
    "response_tail",
    "tool_schema",
)

STABLE_SEGMENTS = frozenset({"system_prompt", "module_skill"})


def canonical_json(value: Any) -> str:
    """Return the deterministic representation used for character counts."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_text(value: Any) -> str | bytes:
    """Turn a segment payload into hashable text without retaining it."""

    if isinstance(value, bytes):
        return value
    if isinstance(value, str):
        return value
    return canonical_json(value)


def sha256_value(value: Any) -> tuple[str, int]:
    """Return ``(sha256, characters)`` for text or bytes."""

    payload = canonical_text(value)
    if isinstance(payload, bytes):
        return hashlib.sha256(payload).hexdigest(), len(payload)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), len(payload)


def _safe_component(value: str) -> str:
    return "".join(char if char.isalnum() or char in "-_." else "_" for char in value)


def namespace_key(
    run_id: str,
    task_id: str,
    identity_key: str,
    revision: int,
) -> str:
    """Build the exact repeat namespace required by the v3 contract."""

    raw = "\x1f".join(
        (str(run_id), str(task_id), str(identity_key), str(int(revision)))
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class HashOccurrenceTracker:
    """Process-safe first-occurrence tracker for one manifest directory.

    A claim is written once under ``root/<namespace>/<digest>.json``.  The
    lock file makes the read/claim operation safe across processes while the
    claim itself is append-only (an existing first occurrence is never
    replaced).  ``namespace`` includes run, task, identity and revision, so a
    hash from another logical task can never turn a new segment into a repeat.
    """

    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(
        self,
        root: Path | str,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        identity_key: str | None = None,
        revision: int | None = None,
        namespace: str | None = None,
    ) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        if namespace is None:
            if None in {run_id, task_id, identity_key, revision}:
                raise ValueError(
                    "HashOccurrenceTracker requires namespace or run/task/identity/revision"
                )
            namespace = namespace_key(
                str(run_id), str(task_id), str(identity_key), int(revision)
            )
        self.namespace = str(namespace)
        self.namespace_root = self.root / _safe_component(self.namespace)
        self.namespace_root.mkdir(parents=True, exist_ok=True)

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(path, threading.Lock())

    def claim(self, digest: str, occurrence: str) -> tuple[bool, str]:
        """Claim one digest and return ``(repeated, first_occurrence)``."""

        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError("digest must be a lowercase SHA-256 value")
        path = self.namespace_root / f"{digest}.json"
        lock_path = path.with_suffix(path.suffix + ".lock")
        with self._lock_for(path):
            with lock_path.open("a+", encoding="utf-8") as lock_handle:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
                try:
                    if path.is_file():
                        try:
                            payload = json.loads(path.read_text(encoding="utf-8"))
                            first = str(payload.get("first_occurrence") or occurrence)
                        except (OSError, ValueError, TypeError):
                            # A malformed claim is not allowed to manufacture a
                            # second first occurrence.  Keep the original path
                            # as the conservative reference.
                            first = occurrence
                        return first != occurrence, first
                    path.write_text(
                        json.dumps(
                            {
                                "namespace": self.namespace,
                                "sha256": digest,
                                "first_occurrence": occurrence,
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                    return False, occurrence
                finally:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)

    # Friendly aliases used by small offline harnesses.
    observe_hash = claim
    record = claim

    def observe(
        self,
        kind: str,
        value: Any,
        *,
        ref: str,
        stable: bool | None = None,
        occurrence: str | None = None,
        duplicate: bool = False,
        index: int | None = None,
    ) -> dict[str, Any]:
        """Hash one payload and return its hash-only segment record."""

        if kind not in SEGMENT_KINDS:
            raise ValueError(f"unsupported context segment kind: {kind}")
        digest, chars = sha256_value(value)
        occurrence = occurrence or ref
        repeated, first = self.claim(digest, occurrence)
        item: dict[str, Any] = {
            "kind": kind,
            "ref": ref,
            "sha256": digest,
            "chars": chars,
            "status": "repeated" if repeated else "new",
            "stability": (
                "stable" if (stable if stable is not None else kind in STABLE_SEGMENTS) else "dynamic"
            ),
            "first_occurrence": first,
            "repeated_content": repeated,
            "duplicate": bool(duplicate),
        }
        if index is not None:
            item["index"] = int(index)
        return item


def segment_metrics(segments: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    """Aggregate v3 character counters using UsageLedger's canonical names."""

    totals = {
        "new_chars": 0,
        "repeated_chars": 0,
        "stable_chars": 0,
        "dynamic_chars": 0,
        "duplicate_chars": 0,
        "repeated_stable_chars": 0,
        "repeated_dynamic_chars": 0,
        "duplicate_tool_result_chars": 0,
        "duplicate_completed_result_chars": 0,
        "duplicate_evidence_chars": 0,
    }
    for raw in segments:
        chars = max(0, int(raw.get("chars", 0) or 0))
        kind = str(raw.get("kind") or "")
        repeated = str(raw.get("status") or "").casefold() == "repeated" or bool(
            raw.get("repeated_content")
        )
        stable = str(raw.get("stability") or "").casefold() == "stable"
        duplicate = bool(raw.get("duplicate"))
        if repeated:
            totals["repeated_chars"] += chars
            totals["repeated_stable_chars" if stable else "repeated_dynamic_chars"] += chars
        else:
            totals["new_chars"] += chars
        totals["stable_chars" if stable else "dynamic_chars"] += chars
        if duplicate:
            totals["duplicate_chars"] += chars
            if kind == "tool_result":
                totals["duplicate_tool_result_chars"] += chars
            elif kind == "completed_result_ref":
                totals["duplicate_completed_result_chars"] += chars
            elif kind in {"evidence_slice", "knowledge_slice"}:
                totals["duplicate_evidence_chars"] += chars
    return totals


def _manifest_digest(payload: Mapping[str, Any]) -> str:
    body = dict(payload)
    body.pop("manifest_sha256", None)
    return hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()


class ManifestSegment(BaseModel):
    """Typed hash-only segment; no body/text field is accepted."""

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    kind: str
    ref: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    chars: int = Field(default=0, ge=0)
    status: str = "new"
    stability: str = "dynamic"
    first_occurrence: str | None = None
    repeated_content: bool = False
    duplicate: bool = False

    @field_validator("sha256")
    @classmethod
    def _sha_is_lower_hex(cls, value: str) -> str:
        if any(char not in "0123456789abcdef" for char in value):
            raise ValueError("sha256 must be lowercase hexadecimal")
        return value

    @model_validator(mode="after")
    def _validate_kind(self) -> "ManifestSegment":
        if self.kind not in SEGMENT_KINDS:
            raise ValueError(f"unsupported context segment kind: {self.kind}")
        if self.status not in {"new", "repeated"}:
            raise ValueError("context segment status must be new or repeated")
        if self.stability not in {"stable", "dynamic"}:
            raise ValueError("context segment stability must be stable or dynamic")
        return self


class ContextManifest(BaseModel):
    """Hash-only v3 context manifest.

    ``extra='allow'`` is deliberate: v1/v2 readers frequently carry fields
    written by older adapters.  New writers only populate the fields below and
    legacy fields are retained by the caller, not copied from message bodies.
    """

    model_config = ConfigDict(extra="allow", populate_by_name=True)

    schema_version: int = 3
    manifest_version: int = 3
    context_manifest_version: int = 3
    manifest_kind: str = "context_manifest_v3"
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    identity_key: str = ""
    revision: int = Field(default=0, ge=0)
    task_attempt_id: str | None = None
    session_id: str | None = None
    provider_call_id: str | None = None
    provider_call_ref: str | None = None
    provider_call_hash: str | None = None
    segments: list[ManifestSegment] = Field(default_factory=list)
    metrics: dict[str, int] = Field(default_factory=dict)
    pre_send_guard_status: str | None = None
    manifest_sha256: str | None = None

    @classmethod
    def from_payload(cls, payload: Mapping[str, Any]) -> "ContextManifest":
        """Read v1/v2 or v3 payloads without treating legacy prose as context."""

        data = dict(payload)
        # v1 used ``context_manifest_version=1`` and v2 used a compressed trace
        # manifest.  Preserve their fields in ``extra`` but expose a v3 view
        # with an empty segment list unless hash-only segments were present.
        data.setdefault("run_id", str(data.get("run") or "legacy-run"))
        data.setdefault("task_id", str(data.get("task") or "legacy-task"))
        data.setdefault("revision", int(data.get("revision", 0) or 0))
        data.setdefault("identity_key", str(data.get("identity") or "legacy"))
        data["schema_version"] = 3
        data["manifest_version"] = 3
        data["context_manifest_version"] = 3
        segments = data.get("segments")
        if not isinstance(segments, list):
            data["segments"] = []
        if not isinstance(data.get("metrics"), Mapping):
            data["metrics"] = segment_metrics(data["segments"])
        return cls.model_validate(data)

    @property
    def computed_sha256(self) -> str:
        return _manifest_digest(self.model_dump(mode="json", exclude_none=True))

    def with_hash(self) -> "ContextManifest":
        return self.model_copy(update={"manifest_sha256": self.computed_sha256})


class ProviderContextManifest(ContextManifest):
    """Hash-only observation of one exact Provider request.

    This is intentionally a separate type from the durable task
    ``context_state.ContextManifest``.  A Provider observation may carry
    adapter payload hashes and attempt disposition; it is not a Rebuilder
    source and must never be parsed as typed task state.
    """

    manifest_kind: Literal["provider_context_observation"] = "provider_context_observation"


# Descriptive alias used by callers that prefer the journal terminology.  The
# shorter ``ContextManifest`` name remains available for v1/v2 context readers.
ProviderCallManifest = ProviderContextManifest
ContextManifestV3 = ContextManifest


def load_manifest(
    payload: Mapping[str, Any],
    *,
    expected_kind: str | None = None,
) -> ContextManifest | ProviderContextManifest:
    """Load a v3 manifest with an explicit semantic kind.

    v1/v2 records had no ``manifest_kind`` and remain readable as ordinary
    context manifests.  A present but unknown kind is rejected fail-closed;
    callers asking for a Provider observation also reject a task-context
    record even when its hashes happen to be valid.
    """

    data = dict(payload)
    kind = data.get("manifest_kind")
    if kind is None and data.get("provider_context_manifest_version") is not None:
        # Pre-kind provider journals already carried this discriminator.
        kind = "provider_context_observation"
        data["manifest_kind"] = kind
    if kind is None and int(data.get("manifest_version", data.get("context_manifest_version", 1)) or 1) <= 2:
        if expected_kind not in {None, "context_manifest_v3"}:
            raise ValueError(
                f"context manifest kind mismatch: expected {expected_kind}, got legacy_context"
            )
        return ContextManifest.from_payload(data)
    if kind is None:
        kind = "context_manifest_v3"
        data["manifest_kind"] = kind
    if kind not in {"context_manifest_v3", "provider_context_observation"}:
        raise ValueError(f"unsupported context manifest kind: {kind}")
    if expected_kind is not None and kind != expected_kind:
        raise ValueError(
            f"context manifest kind mismatch: expected {expected_kind}, got {kind}"
        )
    if kind == "provider_context_observation":
        return ProviderContextManifest.model_validate(data)
    return ContextManifest.model_validate(data)


def build_manifest_payload(
    *,
    run_id: str,
    task_id: str,
    identity_key: str,
    revision: int,
    segments: Sequence[Mapping[str, Any]],
    task_attempt_id: str | None = None,
    session_id: str | None = None,
    provider_call_id: str | None = None,
    provider_call_ref: str | None = None,
    provider_call_hash: str | None = None,
    manifest_kind: str = "context_manifest_v3",
    pre_send_guard_status: str | None = None,
    **legacy_fields: Any,
) -> dict[str, Any]:
    """Build a v3 hash-only payload and retain caller-supplied old fields."""

    clean_segments = [ManifestSegment.model_validate(item).model_dump(mode="json") for item in segments]
    metrics = segment_metrics(clean_segments)
    payload: dict[str, Any] = {
        "schema_version": 3,
        "manifest_version": 3,
        "context_manifest_version": 3,
        "manifest_kind": manifest_kind,
        "run_id": run_id,
        "task_id": task_id,
        "identity_key": identity_key,
        "revision": int(revision),
        "task_attempt_id": task_attempt_id,
        "session_id": session_id,
        "provider_call_id": provider_call_id,
        "provider_call_ref": provider_call_ref,
        "provider_call_hash": provider_call_hash,
        "segments": clean_segments,
        "metrics": metrics,
        # Top-level aliases make the JSONL/JSON manifest directly auditable and
        # match UsageLedger's canonical character field names.
        **metrics,
        "pre_send_guard_status": pre_send_guard_status,
        **legacy_fields,
    }
    payload = {key: value for key, value in payload.items() if value is not None}
    payload["manifest_sha256"] = _manifest_digest(payload)
    return payload


__all__ = [
    "ContextManifest",
    "ContextManifestV3",
    "HashOccurrenceTracker",
    "ManifestSegment",
    "ProviderContextManifest",
    "ProviderCallManifest",
    "SEGMENT_KINDS",
    "STABLE_SEGMENTS",
    "build_manifest_payload",
    "canonical_json",
    "canonical_text",
    "namespace_key",
    "segment_metrics",
    "sha256_value",
    "load_manifest",
]
