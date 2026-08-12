"""Typed, hash-addressed state used to rebuild a bounded Provider context.

The models in this module deliberately contain references and hashes instead
of replaying large prose or tool payloads.  A separate forensic conversation
trace may retain those bytes, but it is not a Provider context source.  Stores
write a complete manifest, sequence record and current pointer through
``os.replace`` so a crash cannot leave a half-written state visible.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator


SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def canonical_json(value: Any) -> str:
    """Serialize a value deterministically for identity and content hashes."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json", exclude_none=True)
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def canonical_sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class ContextStateModel(BaseModel):
    """Strict base for persisted context state."""

    model_config = ConfigDict(
        extra="forbid",
        populate_by_name=True,
        validate_assignment=True,
    )

    @property
    def canonical_sha256(self) -> str:
        return canonical_sha256(self.model_dump(mode="json", exclude_none=True))


class _HashFields(ContextStateModel):
    """Shared reference/hash fields with optional bounded inline content."""

    ref: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    content: str | None = None
    summary: str | None = None
    characters: int = Field(default=0, ge=0, alias="chars")
    consumed: bool = False

    @field_validator("sha256")
    @classmethod
    def hash_is_lower_hex(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def consumed_payload_is_reference_only(self):
        if self.consumed and self.content is not None:
            raise ValueError("consumed context content must be retained by ref/hash only")
        if self.content is not None and self.characters == 0:
            self.characters = len(self.content)
        return self

    @property
    def chars(self) -> int:
        return self.characters


class KnowledgeSlice(_HashFields):
    """One bounded reusable knowledge/reference slice."""

    kind: Literal["knowledge", "reference", "research"] = "knowledge"
    title: str | None = None
    locator: str | None = None
    source_id: str | None = None
    question: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("text", "content"),
            ("content_sha256", "sha256"),
            ("source_ref", "ref"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        return payload


class EvidenceSlice(_HashFields):
    """One current-run evidence slice shown to a Provider when needed."""

    kind: Literal["evidence", "project_evidence"] = "evidence"
    evidence_id: str | None = Field(default=None, alias="id")
    title: str | None = None
    locator: str | None = None
    source_id: str | None = None
    binding: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("text", "content"),
            ("content_sha256", "sha256"),
            ("source_ref", "ref"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        return payload

    @property
    def id(self) -> str | None:
        return self.evidence_id


class ResultPartRef(ContextStateModel):
    """Reference to a durable result part; prose is not duplicated here."""

    part_id: str = Field(min_length=1)
    ref: str = Field(min_length=1)
    sha256: str = Field(min_length=64, max_length=64)
    characters: int = Field(default=0, ge=0, alias="chars")
    ready: bool = True
    consumed: bool = True
    revision: int = Field(default=0, ge=0)
    content: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("id", "part_id"),
            ("result_ref", "ref"),
            ("content_sha256", "sha256"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        return payload

    @field_validator("sha256")
    @classmethod
    def hash_is_lower_hex(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def prose_must_not_be_carried_after_consumption(self):
        if self.consumed and self.content is not None:
            raise ValueError("consumed result parts retain only ref/hash")
        if self.content is not None and self.characters == 0:
            self.characters = len(self.content)
        return self

    @property
    def chars(self) -> int:
        return self.characters

    @property
    def content_sha256(self) -> str:
        return self.sha256


class ToolResultMemo(ContextStateModel):
    """Hash-only memo for a consumed tool result."""

    call_id: str = Field(min_length=1)
    tool_name: str = Field(min_length=1)
    result_ref: str = Field(min_length=1, alias="ref")
    result_sha256: str = Field(min_length=64, max_length=64, alias="sha256")
    result_chars: int = Field(default=0, ge=0, alias="chars")
    consumed: bool = True
    status: str = "completed"
    summary: str | None = None
    run_id: str | None = None
    task_id: str | None = None
    revision: int | None = Field(default=None, ge=0)
    content: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("content_sha256", "result_sha256"),
            ("result_hash", "result_sha256"),
            ("result_ref", "ref"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        return payload

    @field_validator("result_sha256")
    @classmethod
    def hash_is_lower_hex(cls, value: str) -> str:
        if not SHA256_RE.fullmatch(value):
            raise ValueError("sha256 must be a lowercase SHA-256 digest")
        return value

    @model_validator(mode="after")
    def memo_content_policy(self):
        if self.consumed and self.content is not None:
            raise ValueError("consumed tool results retain only ref/hash")
        if self.content is not None and self.result_chars == 0:
            self.result_chars = len(self.content)
        return self

    @property
    def ref(self) -> str:
        return self.result_ref

    @property
    def sha256(self) -> str:
        return self.result_sha256

    @property
    def chars(self) -> int:
        return self.result_chars


class TaskStateCapsule(ContextStateModel):
    """Small typed task state that survives a Provider context rebase."""

    schema_version: Literal[3] = 3
    run_id: str = ""
    task_id: str = Field(min_length=1)
    revision: int = Field(default=0, ge=0)
    task_attempt_id: str | None = None
    objective: str = ""
    status: str = "in_progress"
    next_action: str | None = None
    continuation_required: bool = False
    input_refs: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    knowledge_refs: list[str] = Field(default_factory=list)
    result_part_refs: list[str] = Field(default_factory=list)
    ready_result_part_ids: list[str] = Field(default_factory=list)
    required_result_part_ids: list[str] = Field(default_factory=list)
    unresolved_questions: list[str] = Field(default_factory=list)
    research_refs: list[str] = Field(default_factory=list)
    research_questions: list[str] = Field(default_factory=list)
    required_tool_names: list[str] = Field(default_factory=list)
    allowed_tool_names: list[str] = Field(default_factory=list)
    state: dict[str, Any] = Field(default_factory=dict)
    # Typed lifecycle fields used by the reporting state protocol.  The
    # broader fields above remain for compatibility with early experiments;
    # these are the canonical compact progress carriers.
    phase: str = "task"
    evidence_acquired: list[str] = Field(default_factory=list)
    open_questions: list[str] = Field(default_factory=list)
    interface_requests: list[str] = Field(default_factory=list)
    completed_result_parts: list[str] = Field(default_factory=list)
    tool_result_refs: list[str] = Field(default_factory=list)
    remaining_work: list[str] = Field(default_factory=list)
    open_output_ref: str | None = None
    sequence: int = Field(default=0, ge=0)
    capsule_sha256: str | None = None

    @property
    def computed_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("capsule_sha256", None)
        return canonical_sha256(payload)

    def with_hash(self) -> "TaskStateCapsule":
        return self.model_copy(update={"capsule_sha256": self.computed_sha256})

    @model_validator(mode="after")
    def capsule_hash_is_consistent(self):
        if self.capsule_sha256:
            if not SHA256_RE.fullmatch(self.capsule_sha256):
                raise ValueError("capsule_sha256 must be a lowercase SHA-256 digest")
            if self.capsule_sha256 != self.computed_sha256:
                raise ValueError("task state capsule canonical hash mismatch")
        else:
            object.__setattr__(self, "capsule_sha256", self.computed_sha256)
        return self


class RunEvidenceIndex(ContextStateModel):
    """Current-run evidence/knowledge/result references for one typed task."""

    schema_version: Literal[3] = 3
    run_id: str = ""
    task_id: str = ""
    revision: int = Field(default=0, ge=0)
    items: list[EvidenceSlice] = Field(default_factory=list)
    knowledge: list[KnowledgeSlice] = Field(default_factory=list)
    result_parts: list[ResultPartRef] = Field(default_factory=list)
    tool_results: list[ToolResultMemo] = Field(default_factory=list)
    module_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    submodule_evidence_ids: dict[str, list[str]] = Field(default_factory=dict)
    conflict_ids: list[str] = Field(default_factory=list)
    gap_ids: list[str] = Field(default_factory=list)
    index_sha256: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("evidence", "items"),
            ("entries", "items"),
            ("evidence_slices", "items"),
            ("knowledge_slices", "knowledge"),
            ("result_part_refs", "result_parts"),
            ("tool_result_memos", "tool_results"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        return payload

    @property
    def entries(self) -> list[EvidenceSlice]:
        return self.items

    @property
    def evidence_slices(self) -> list[EvidenceSlice]:
        return self.items

    @property
    def evidence(self) -> list[EvidenceSlice]:
        return self.items

    @property
    def knowledge_slices(self) -> list[KnowledgeSlice]:
        return self.knowledge

    @property
    def computed_sha256(self) -> str:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("index_sha256", None)
        return canonical_sha256(payload)

    def with_hash(self) -> "RunEvidenceIndex":
        return self.model_copy(update={"index_sha256": self.computed_sha256})

    @model_validator(mode="after")
    def index_hash_is_consistent(self):
        if self.index_sha256:
            if not SHA256_RE.fullmatch(self.index_sha256):
                raise ValueError("index_sha256 must be a lowercase SHA-256 digest")
            if self.index_sha256 != self.computed_sha256:
                raise ValueError("run evidence index canonical hash mismatch")
        else:
            object.__setattr__(self, "index_sha256", self.computed_sha256)
        return self

    @model_validator(mode="after")
    def nested_identity_matches(self):
        for item in self.result_parts:
            if item.revision > self.revision:
                raise ValueError("result part revision cannot exceed evidence index revision")
        for item in self.tool_results:
            if item.run_id is not None and item.run_id != self.run_id:
                raise ValueError("tool memo run_id does not match evidence index")
            if item.task_id is not None and item.task_id != self.task_id:
                raise ValueError("tool memo task_id does not match evidence index")
            if item.revision is not None and item.revision != self.revision:
                raise ValueError("tool memo revision does not match evidence index")
        return self


class ContextManifest(ContextStateModel):
    """Manifest v3 describing exactly what can be sent to a Provider."""

    schema_version: Literal[3] = 3
    manifest_version: Literal[3] = 3
    run_id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    revision: int = Field(default=0, ge=0)
    task_attempt_id: str | None = None
    # Stable system prompt and immutable task header.  Messages are plain
    # JSON because provider adapters use their own LLMMessage dataclass.
    stable_prefix: list[dict[str, Any]] = Field(default_factory=list)
    task_state_capsule: TaskStateCapsule | None = None
    task: TaskStateCapsule | None = None
    capsule: TaskStateCapsule | None = None
    evidence_index: RunEvidenceIndex | None = None
    evidence: list[EvidenceSlice] = Field(default_factory=list)
    knowledge: list[KnowledgeSlice] = Field(default_factory=list)
    result_parts: list[ResultPartRef] = Field(default_factory=list)
    tool_results: list[ToolResultMemo] = Field(default_factory=list)
    # Complete, not partial, provider protocol units.  A unit is encoded by
    # ReportingContextRebuilder and validated before it is placed here.
    tool_units: list[dict[str, Any]] = Field(default_factory=list)
    partial_tail: list[dict[str, Any]] = Field(default_factory=list)
    minimal_tools: list[dict[str, Any]] = Field(default_factory=list)
    forensic_trace_ref: str | None = None
    forensic_trace_sha256: str | None = None
    manifest_sha256: str | None = None

    @model_validator(mode="before")
    @classmethod
    def compatibility_aliases(cls, value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        payload = dict(value)
        for alias, name in (
            ("state_capsule", "task_state_capsule"),
            ("task_capsule", "task_state_capsule"),
            ("run_evidence_index", "evidence_index"),
            ("evidence_slices", "evidence"),
            ("knowledge_slices", "knowledge"),
            ("result_part_refs", "result_parts"),
            ("tool_result_memos", "tool_results"),
            ("unconsumed_tool_units", "tool_units"),
            ("tools", "minimal_tools"),
            ("partial_output", "partial_tail"),
            ("canonical_hash", "manifest_sha256"),
            ("context_manifest_sha256", "manifest_sha256"),
        ):
            if alias in payload and name not in payload:
                payload[name] = payload.pop(alias)
        # The former ``task``/``capsule`` names are accepted as input but
        # normalized to the v3 task_state_capsule segment.
        if "task_state_capsule" not in payload:
            if "task" in payload:
                payload["task_state_capsule"] = payload.pop("task")
            elif "capsule" in payload:
                payload["task_state_capsule"] = payload.pop("capsule")
        return payload

    @model_validator(mode="after")
    def nested_identity_matches(self):
        nested = [self.task_state_capsule, self.task, self.capsule, self.evidence_index]
        for item in nested:
            if item is None:
                continue
            if item.run_id and self.run_id and item.run_id != self.run_id:
                raise ValueError("context manifest nested state identity mismatch")
            if item.task_id and self.task_id and item.task_id != self.task_id:
                raise ValueError("context manifest nested state identity mismatch")
            if item.revision != self.revision:
                raise ValueError("context manifest nested state revision mismatch")
        capsules = [item for item in (self.task_state_capsule, self.task, self.capsule) if item is not None]
        if capsules and any(item.model_dump(mode="json") != capsules[0].model_dump(mode="json") for item in capsules[1:]):
            raise ValueError("task state capsule aliases must describe the same typed state")
        if self.forensic_trace_sha256 and not SHA256_RE.fullmatch(self.forensic_trace_sha256):
            raise ValueError("forensic trace hash must be a lowercase SHA-256 digest")
        if not self.manifest_sha256:
            object.__setattr__(self, "manifest_sha256", self.computed_sha256)
        elif self.manifest_sha256 != self.computed_sha256:
            raise ValueError("context manifest canonical hash mismatch")
        return self

    @property
    def task_state(self) -> TaskStateCapsule | None:
        return self.task_state_capsule or self.task or self.capsule

    @property
    def canonical_payload(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json", exclude_none=True)
        payload.pop("manifest_sha256", None)
        return payload

    @property
    def computed_sha256(self) -> str:
        return canonical_sha256(self.canonical_payload)

    def with_hash(self) -> "ContextManifest":
        return self.model_copy(update={"manifest_sha256": self.computed_sha256})


def _safe_identity(value: str, label: str) -> str:
    safe = Path(value).name
    if not value or safe != value or value in {".", ".."}:
        raise ValueError(f"{label} must be a single path-safe identifier")
    return safe


class _AtomicStore:
    """Small atomic JSON store shared by context and memo state."""

    def __init__(self, root: Path | str, *, run_id: str | None = None, folder: str):
        base = Path(root).expanduser().resolve()
        if run_id:
            run_id = _safe_identity(run_id, "run_id")
            # Accept either workspace or an already selected run directory.
            if base.name == run_id:
                base = base / "context-state"
            elif (base / "Work" / "runs").is_dir() or not base.name.endswith("-state"):
                base = base / "Work" / "runs" / run_id / "context-state"
        elif base.name != "context-state":
            base = base / "context-state"
        self.root = base / folder if folder else base
        self.root.mkdir(parents=True, exist_ok=True)
        self.sequence_path = self.root / "sequence.json"
        self.current_path = self.root / "current.json"

    @staticmethod
    def _atomic_write(path: Path, content: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            os.replace(temporary, path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return path

    def _read_sequence(self) -> list[dict[str, Any]]:
        if not self.sequence_path.is_file():
            return []
        try:
            payload = json.loads(self.sequence_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("context state sequence is unreadable") from exc
        if not isinstance(payload, list):
            raise ValueError("context state sequence must be a list")
        return [item for item in payload if isinstance(item, dict)]

    def _next_sequence(self) -> int:
        return max((int(item.get("sequence", 0)) for item in self._read_sequence()), default=0) + 1

    def _write_sequence(self, entries: list[dict[str, Any]]) -> None:
        self._atomic_write(
            self.sequence_path,
            json.dumps(entries, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )


class TaskStateStore(_AtomicStore):
    """Atomic current-pointer/sequence store for :class:`ContextManifest`."""

    def __init__(
        self,
        root: Path | str,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
    ):
        super().__init__(root, run_id=run_id, folder="manifests")
        self.expected_run_id = run_id
        self.expected_task_id = task_id
        self.expected_revision = revision
        self.manifests_dir = self.root / "items"
        self.manifests_dir.mkdir(parents=True, exist_ok=True)

    def _validate_identity(
        self,
        manifest: ContextManifest,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
    ) -> None:
        expected = (
            run_id if run_id is not None else self.expected_run_id,
            task_id if task_id is not None else self.expected_task_id,
            revision if revision is not None else self.expected_revision,
        )
        actual = (manifest.run_id, manifest.task_id, manifest.revision)
        if expected[0] is not None and actual[0] != expected[0]:
            raise ValueError("context manifest run_id mismatch")
        if expected[1] is not None and actual[1] != expected[1]:
            raise ValueError("context manifest task_id mismatch")
        if expected[2] is not None and actual[2] != expected[2]:
            raise ValueError("context manifest revision mismatch")

    def save(
        self,
        manifest: ContextManifest,
        *,
        expected_sha256: str | None = None,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
    ) -> Path:
        if not isinstance(manifest, ContextManifest):
            manifest = ContextManifest.model_validate(manifest)
        self._validate_identity(manifest, run_id=run_id, task_id=task_id, revision=revision)
        computed = manifest.computed_sha256
        if manifest.manifest_sha256 and manifest.manifest_sha256 != computed:
            raise ValueError("context manifest canonical hash mismatch")
        if expected_sha256 is not None:
            current = self.current_pointer()
            if current is not None and current.get("manifest_sha256") != expected_sha256:
                raise ValueError("context manifest compare-and-swap hash mismatch")
        manifest = manifest.model_copy(update={"manifest_sha256": computed})
        sequence = self._next_sequence()
        ref = f"items/{sequence:08d}-{computed[:16]}.json"
        path = self.root / ref
        self._atomic_write(
            path,
            json.dumps(manifest.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2)
            + "\n",
        )
        entries = self._read_sequence()
        entries.append(
            {
                "sequence": sequence,
                "ref": ref,
                "manifest_sha256": computed,
                "run_id": manifest.run_id,
                "task_id": manifest.task_id,
                "revision": manifest.revision,
            }
        )
        self._write_sequence(entries)
        self._atomic_write(
            self.current_path,
            json.dumps(entries[-1], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return path

    write = save

    def current_pointer(self) -> dict[str, Any] | None:
        if not self.current_path.is_file():
            return None
        try:
            payload = json.loads(self.current_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("context current pointer is unreadable") from exc
        if not isinstance(payload, dict):
            raise ValueError("context current pointer must be an object")
        return payload

    def load(
        self,
        reference: str | Path | int | None = None,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
        expected_sha256: str | None = None,
    ) -> ContextManifest:
        pointer = self.current_pointer()
        if reference is None:
            if pointer is None:
                raise FileNotFoundError("context manifest current pointer is missing")
            reference = str(pointer.get("ref") or "")
            expected_sha256 = expected_sha256 or pointer.get("manifest_sha256")
        if isinstance(reference, int):
            entries = self._read_sequence()
            found = next((item for item in entries if int(item.get("sequence", -1)) == reference), None)
            if found is None:
                raise FileNotFoundError(f"context manifest sequence not found: {reference}")
            reference = str(found.get("ref") or "")
            expected_sha256 = expected_sha256 or found.get("manifest_sha256")
        path = Path(reference)
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError(f"context manifest is outside the state store: {reference}")
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            manifest = ContextManifest.model_validate(raw)
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("context manifest is invalid") from exc
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"context manifest is invalid: {exc}") from exc
        self._validate_identity(manifest, run_id=run_id, task_id=task_id, revision=revision)
        computed = manifest.computed_sha256
        if manifest.manifest_sha256 != computed:
            raise ValueError("context manifest canonical hash mismatch")
        if expected_sha256 is not None and computed != expected_sha256:
            raise ValueError("context manifest pointer hash mismatch")
        return manifest

    read = load
    load_current = load
    load_verify = load
    save_manifest = save
    load_manifest = load

    def current(self) -> ContextManifest:
        return self.load()

    get_current = current


class ToolResultMemoStore(_AtomicStore):
    """Atomic sequence store for consumed tool-result references."""

    def __init__(
        self,
        root: Path | str,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
    ):
        super().__init__(root, run_id=run_id, folder="tool-memos")
        self.expected_run_id = run_id
        self.expected_task_id = task_id
        self.expected_revision = revision
        self.memos_dir = self.root / "items"
        self.memos_dir.mkdir(parents=True, exist_ok=True)

    def _validate_identity(self, memo: ToolResultMemo) -> None:
        if self.expected_run_id is not None and memo.run_id not in {None, self.expected_run_id}:
            raise ValueError("tool memo run_id mismatch")
        if self.expected_task_id is not None and memo.task_id not in {None, self.expected_task_id}:
            raise ValueError("tool memo task_id mismatch")
        if self.expected_revision is not None and memo.revision not in {None, self.expected_revision}:
            raise ValueError("tool memo revision mismatch")

    def save(self, memo: ToolResultMemo | dict[str, Any]) -> Path:
        if not isinstance(memo, ToolResultMemo):
            memo = ToolResultMemo.model_validate(memo)
        self._validate_identity(memo)
        sequence = self._next_sequence()
        digest = memo.canonical_sha256
        ref = f"items/{sequence:08d}-{digest[:16]}.json"
        path = self.root / ref
        self._atomic_write(
            path,
            json.dumps(memo.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        entries = self._read_sequence()
        entries.append({"sequence": sequence, "ref": ref, "sha256": digest})
        self._write_sequence(entries)
        self._atomic_write(
            self.current_path,
            json.dumps(entries[-1], ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        )
        return path

    append = save
    record = save

    def load(self, reference: str | Path | int | None = None) -> ToolResultMemo:
        pointer = self.current_pointer()
        expected_sha = None
        if reference is None:
            if pointer is None:
                raise FileNotFoundError("tool memo current pointer is missing")
            reference = pointer.get("ref")
            expected_sha = pointer.get("sha256")
        if isinstance(reference, int):
            entry = next((item for item in self._read_sequence() if int(item.get("sequence", -1)) == reference), None)
            if entry is None:
                raise FileNotFoundError(f"tool memo sequence not found: {reference}")
            reference = entry.get("ref")
            expected_sha = entry.get("sha256")
        path = Path(str(reference))
        if not path.is_absolute():
            path = self.root / path
        path = path.resolve()
        if not path.is_relative_to(self.root) or not path.is_file():
            raise FileNotFoundError("tool memo is outside the state store")
        try:
            memo = ToolResultMemo.model_validate(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("tool memo is invalid") from exc
        except (ValidationError, ValueError) as exc:
            raise ValueError(f"tool memo is invalid: {exc}") from exc
        self._validate_identity(memo)
        if expected_sha and memo.canonical_sha256 != expected_sha:
            raise ValueError("tool memo hash mismatch")
        return memo

    read = load
    load_current = load
    save_memo = save
    load_memo = load

    def current(self) -> ToolResultMemo:
        return self.load()

    get_current = current


__all__ = [
    "ContextManifest",
    "ContextStateModel",
    "EvidenceSlice",
    "KnowledgeSlice",
    "ResultPartRef",
    "RunEvidenceIndex",
    "TaskStateCapsule",
    "TaskStateStore",
    "ToolResultMemo",
    "ToolResultMemoStore",
    "canonical_json",
    "canonical_sha256",
]
