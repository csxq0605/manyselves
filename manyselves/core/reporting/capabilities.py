"""Compile reporting inputs into small, fail-closed artifact capabilities.

``TaskEnvelope`` predates the capability compiler and deliberately remains a
small compatibility carrier.  This module is the boundary where that carrier
is turned into executable access: every ref is canonicalised, described from
bytes, checked against the current run/workspace and assigned the operations
which are meaningful for its MIME/kind.  A provider-visible ref which cannot
be described is never silently converted into a guessed path.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope

from ..access_policy import ISOLATED_DISTILLATION_SNAPSHOT_NAME
from ..artifacts.gateway import ArtifactGateway, ArtifactGrant
from ..artifacts.types import ArtifactDescriptor
from .config import AgentDefinition, ConfigurationError

DeliveryMode = Literal["inline", "reference", "hash_retained"]
_OPAQUE_PREFIX = "artifact:v1:"
_REF_KEY_RE = re.compile(r"(?:^|_)(?:ref|refs|path|paths)$", re.IGNORECASE)
_REF_CONTEXT_TOKENS = (
    "subject",
    "finding",
    "photo",
    "artifact",
    "shared",
    "inherited",
    "evidence",
    "knowledge",
    "context",
    "input",
    "completion",
    "validation",
    "review",
    "verdict",
    "coverage",
    "manifest",
    "discovery",
    "bundle",
    "source",
    "template",
)
_ID_ONLY_RE = re.compile(r"^(?:[A-Z]{1,8}-[A-Za-z0-9_.-]+|[A-Za-z_]+)$")
_KNOWN_PUBLIC_ROOTS = {
    "Inputs",
    "Input",
    "Work",
    "Knowledge",
    "Templates",
    "Capabilities",
    "ProductCapabilities",
}


def _isolated_template_tool_ref(
    definition: AgentDefinition,
    envelope: TaskEnvelope,
    typed_input: Any | None,
    gateway: ArtifactGateway,
) -> str | None:
    """Return the one ref reserved for the distiller's specialized reader.

    The snapshot must not become a generic artifact capability: only the
    one-shot ``InspectDocumentTool`` may read it.  This narrow exemption lets
    capability compilation accept the declared input without weakening the
    global expert-template deny rule used by every other Agent-facing reader.
    """

    expected = (
        f"Work/runs/{envelope.run_id}/templates/"
        f"{ISOLATED_DISTILLATION_SNAPSHOT_NAME}"
    )
    if getattr(typed_input, "template_ref", None) != expected:
        return None
    grant = gateway.grant
    if not (
        envelope.task_id == "template-skill-distillation"
        and envelope.agent_id == "template-distiller"
        and envelope.input_contract_kind == "template_distillation_input"
        and getattr(typed_input, "run_id", None) == envelope.run_id
        and definition.id == "template-distiller"
        and grant.task_id == envelope.task_id
        and grant.agent_id == definition.id
    ):
        return None
    return expected


def _as_relative_ref(value: Any) -> str | None:
    """Return a candidate local/opaque ref while rejecting URL and ID values."""

    if isinstance(value, Path):
        value = value.as_posix()
    if not isinstance(value, str):
        return None
    ref = value.strip()
    if not ref or "\x00" in ref or "://" in ref:
        return None
    if ref.startswith(_OPAQUE_PREFIX):
        return ref
    if ref.upper().startswith("P-") and "/" not in ref and "\\" not in ref:
        return ref
    # E-/C-/F- values and other bare identifiers are domain identifiers,
    # not artifact paths.  P-* is also an identifier, but it is returned above
    # so the recursive collector can route it through the current-run photo
    # map rather than guessing a path.
    if _ID_ONLY_RE.fullmatch(ref) and "/" not in ref and "\\" not in ref:
        return None
    return ref


def _field_is_ref_key(key: str, *, context: tuple[str, ...] = ()) -> bool:
    normalized = str(key).casefold()
    if normalized in {"ref", "refs", "path", "paths"}:
        return True
    if _REF_KEY_RE.search(normalized):
        return True
    return any(token in normalized for token in _REF_CONTEXT_TOKENS) and (
        normalized.endswith(("_path", "_paths", "_ref", "_refs"))
        or normalized in {"artifact", "artifacts", "shared", "inherited"}
        or any(token in context for token in ("photo", "artifact", "subject", "finding"))
    )


def _looks_like_path(ref: str) -> bool:
    if ref.startswith(_OPAQUE_PREFIX):
        return True
    path = Path(ref)
    if path.is_absolute() or ".." in path.parts:
        return True
    if "/" in ref or "\\" in ref:
        return True
    return path.suffix.casefold() in {
        ".json",
        ".jsonl",
        ".txt",
        ".md",
        ".markdown",
        ".csv",
        ".tsv",
        ".yaml",
        ".yml",
        ".docx",
        ".xlsx",
        ".xls",
        ".pdf",
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".bmp",
        ".webp",
    }


def _collect_nested_refs(
    value: Any,
    *,
    key: str = "",
    context: tuple[str, ...] = (),
    refs: list[str],
    photo_ids: list[str],
) -> None:
    """Walk Pydantic/dict/list payloads without treating IDs as paths.

    The reporting input models intentionally contain nested ``subject`` and
    ``finding`` objects.  A shallow ``input_refs`` lookup misses their
    ``*_ref(s)`` fields and the photo paths carried by ``PhotoAsset``.  Keys
    provide the semantic boundary; bare strings are accepted only when they
    visibly look like a workspace path or an opaque ref.
    """

    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            value = value.model_dump()
    if isinstance(value, Mapping):
        current_context = context
        if key:
            current_context = (*context, str(key).casefold())
        for raw_key, item in value.items():
            item_key = str(raw_key)
            lowered = item_key.casefold()
            # Hash indexes use artifact refs as mapping keys.  The values are
            # digests and must not become guessed artifact references.
            if lowered in {"artifact_sha256", "hashes_by_ref", "artifact_hashes"} and isinstance(item, Mapping):
                for candidate_key in item:
                    candidate = _as_relative_ref(candidate_key)
                    if candidate is not None:
                        refs.append(candidate)
                continue
            _collect_nested_refs(
                item,
                key=item_key,
                context=current_context,
                refs=refs,
                photo_ids=photo_ids,
            )
        return
    if isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _collect_nested_refs(
                item,
                key=key,
                context=context,
                refs=refs,
                photo_ids=photo_ids,
            )
        return
    if not isinstance(value, (str, Path)):
        return
    candidate = _as_relative_ref(value)
    if candidate is None:
        return
    text = str(candidate)
    if text.upper().startswith("P-") and "/" not in text and "\\" not in text:
        photo_ids.append(text)
        return
    if _field_is_ref_key(key, context=context) or _looks_like_path(text):
        refs.append(text)


def collect_reference_refs(
    envelope: TaskEnvelope,
    *,
    typed_input: Any | None = None,
    extra_refs: Iterable[str] = (),
) -> tuple[str, ...]:
    """Collect envelope and nested typed-input refs in deterministic order."""

    refs: list[str] = [
        *envelope.input_refs,
        *envelope.context_summary_refs,
        *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
        *([envelope.input_contract_ref] if envelope.input_contract_ref else []),
        *extra_refs,
    ]
    if typed_input is not None:
        _collect_nested_refs(typed_input, refs=refs, photo_ids=[])
    return tuple(dict.fromkeys(str(ref) for ref in refs if str(ref).strip()))


# Compatibility spellings useful to callers which used the audit vocabulary.
collect_task_refs = collect_reference_refs
collect_artifact_refs = collect_reference_refs


def collect_photo_ids(value: Any) -> tuple[str, ...]:
    """Collect P-* identifiers without interpreting them as workspace paths."""

    refs: list[str] = []
    photo_ids: list[str] = []
    _collect_nested_refs(value, refs=refs, photo_ids=photo_ids)
    return tuple(dict.fromkeys(photo_ids))


def _safe_public_ref(ref: str, *, workspace: Path, run_id: str) -> None:
    """Reject traversal, forbidden internals, and cross-run public refs."""

    if not isinstance(ref, str) or not ref.strip():
        raise ConfigurationError("artifact reference must be a non-empty string")
    if ref.startswith(_OPAQUE_PREFIX):
        return
    path = Path(ref)
    if path.is_absolute() or ".." in path.parts or "\x00" in ref:
        raise ConfigurationError(f"artifact reference traversal is forbidden: {ref}")
    if any(part.casefold() == ".manyselves" for part in path.parts):
        raise ConfigurationError("raw .manyselves paths are never prompt-visible artifacts")
    target = (workspace / path).resolve()
    if not target.is_relative_to(workspace):
        raise ConfigurationError(f"artifact reference escapes the workspace: {ref}")
    parts = path.parts
    if len(parts) >= 3 and parts[0] == "Work" and parts[1] == "runs" and parts[2] != run_id:
        raise ConfigurationError(
            f"artifact reference belongs to another run: {ref} (expected {run_id})"
        )


def _typed_input_payload(
    envelope: TaskEnvelope,
    gateway: ArtifactGateway,
) -> Any | None:
    if not envelope.input_contract_ref:
        return None
    # Read only after the gateway has validated workspace/opaque identity.  A
    # typed contract is JSON by contract; malformed or mismatched contracts are
    # configuration errors, never a reason to expose a broader path tool.
    descriptor = gateway.describe(envelope.input_contract_ref)
    if descriptor.kind != "text":
        raise ConfigurationError("input contract must be a readable UTF-8 JSON artifact")
    target = gateway._resolve(envelope.input_contract_ref)  # validated gateway boundary
    try:
        payload = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ConfigurationError("input contract is not readable JSON") from exc
    if envelope.input_contract_kind:
        try:
            from .input_contracts import INPUT_CONTRACT_TYPES

            model = INPUT_CONTRACT_TYPES.get(envelope.input_contract_kind)
            if model is None:
                raise ConfigurationError(
                    f"unknown input contract kind: {envelope.input_contract_kind}"
                )
            return model.model_validate(payload)
        except ConfigurationError:
            raise
        except Exception as exc:
            raise ConfigurationError(
                f"input contract does not match {envelope.input_contract_kind}"
            ) from exc
    return payload


@dataclass(frozen=True, slots=True)
class Capability:
    """One executable artifact grant, suitable for prompt/tool injection."""

    canonical_ref: str
    descriptor: ArtifactDescriptor
    media_type: str
    logical_role: str
    allowed_operations: tuple[str, ...]
    delivery_mode: DeliveryMode = "reference"

    def __post_init__(self) -> None:
        if self.canonical_ref != self.descriptor.canonical_ref:
            raise ConfigurationError("capability canonical ref does not match descriptor")
        if self.media_type != self.descriptor.media_type:
            raise ConfigurationError("capability media type does not match descriptor")
        if self.logical_role != self.descriptor.logical_role:
            raise ConfigurationError("capability logical role does not match descriptor")
        operations = tuple(dict.fromkeys(str(item) for item in self.allowed_operations))
        if not set(operations).issubset(set(self.descriptor.allowed_operations)):
            raise ConfigurationError("capability operations exceed descriptor operations")
        object.__setattr__(self, "allowed_operations", operations)

    @property
    def ref(self) -> str:
        return self.canonical_ref

    @property
    def path(self) -> str | None:
        return self.descriptor.path

    @property
    def kind(self) -> str:
        return self.descriptor.kind

    @property
    def readable(self) -> bool:
        return bool(self.descriptor.path or self.descriptor.opaque)

    def allows(self, value: str) -> bool:
        # ``normalize_tool_call`` calls ``allows(ref)`` while wrappers call it
        # with an operation.  Supporting both keeps the compatibility API small.
        return value == self.canonical_ref or value in self.allowed_operations

    def allows_operation(self, operation: str) -> bool:
        return operation in self.allowed_operations

    def as_dict(self) -> dict[str, Any]:
        return {
            "canonical_ref": self.canonical_ref,
            "descriptor": self.descriptor.as_dict(),
            "media_type": self.media_type,
            "logical_role": self.logical_role,
            "allowed_operations": list(self.allowed_operations),
            "delivery_mode": self.delivery_mode,
        }

    to_dict = as_dict


@dataclass(frozen=True)
class CompiledAgentAccess:
    gateway: ArtifactGateway
    readable_refs: tuple[str, ...]
    unreadable_refs: tuple[str, ...]
    tool_names: tuple[str, ...]
    capabilities: tuple[Capability, ...] = ()
    photo_refs: tuple[tuple[str, str], ...] = ()

    @property
    def capability_map(self) -> dict[str, Capability]:
        return {item.canonical_ref: item for item in self.capabilities}

    @property
    def refs(self) -> tuple[str, ...]:
        return tuple(item.canonical_ref for item in self.capabilities)

    def get(self, ref: str) -> Capability | None:
        return self.capability_map.get(ref)

    def allows(self, ref: str, operation: str | None = None) -> bool:
        item = self.get(ref)
        return bool(item and (operation is None or item.allows_operation(operation)))

    def photo_map(self) -> dict[str, str]:
        return dict(self.photo_refs)


def _photo_map_from_value(value: Any, *, workspace: Path, run_id: str) -> dict[str, str]:
    result: dict[str, str] = {}
    if hasattr(value, "model_dump"):
        try:
            value = value.model_dump(mode="json")
        except Exception:
            value = value.model_dump()
    if isinstance(value, Mapping):
        photo_id = value.get("id")
        path = value.get("path")
        if isinstance(photo_id, str) and photo_id.upper().startswith("P-") and path:
            ref = str(path)
            _safe_public_ref(ref, workspace=workspace, run_id=run_id)
            result.setdefault(photo_id, ref)
        for item in value.values():
            result.update(_photo_map_from_value(item, workspace=workspace, run_id=run_id))
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            result.update(_photo_map_from_value(item, workspace=workspace, run_id=run_id))
    return result


def _load_run_photo_map(
    envelope: TaskEnvelope,
    *,
    workspace: Path,
    typed_input: Any | None,
    photo_assets: Any | None,
) -> dict[str, str]:
    result = _photo_map_from_value(
        typed_input,
        workspace=workspace,
        run_id=envelope.run_id,
    )
    result.update(
        _photo_map_from_value(
            photo_assets,
            workspace=workspace,
            run_id=envelope.run_id,
        )
        if photo_assets is not None
        else {}
    )
    roots = (
        workspace / f"Work/runs/{envelope.run_id}/preparation/photo-manifest.json",
        workspace / f"Work/runs/{envelope.run_id}/context/photo-manifest.json",
        workspace / f"Work/runs/{envelope.run_id}/photo-manifest.json",
    )
    for path in roots:
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        result.update(
            _photo_map_from_value(payload, workspace=workspace, run_id=envelope.run_id)
        )
    return result


def compile_agent_access(
    definition: AgentDefinition,
    envelope: TaskEnvelope,
    refs: list[str] | None = None,
    *,
    gateway: ArtifactGateway,
    typed_input: Any | None = None,
    shared_refs: Iterable[str] = (),
    photo_assets: Any | None = None,
) -> CompiledAgentAccess:
    """Compile a role/envelope into descriptor-backed capabilities.

    ``refs`` is retained as the old positional compatibility argument.  The
    envelope and its typed input are authoritative; callers cannot broaden the
    set by passing an arbitrary path to a tool after compilation.
    """

    workspace = gateway.workspace
    if typed_input is None and envelope.input_contract_ref:
        typed_input = _typed_input_payload(envelope, gateway)
    isolated_template_ref = _isolated_template_tool_ref(
        definition,
        envelope,
        typed_input,
        gateway,
    )
    envelope_refs = collect_reference_refs(
        envelope,
        typed_input=typed_input,
        extra_refs=[*(refs or ()), *shared_refs],
    )
    photo_map = _load_run_photo_map(
        envelope,
        workspace=workspace,
        typed_input=typed_input,
        photo_assets=photo_assets,
    )
    # Resolve P-* identifiers carried by typed subjects/photos to the one
    # current-run PhotoAsset path.  An unknown P-ID remains an explicit gap and
    # is never converted to ``Inputs/P-...`` or another guessed path.
    typed_photo_ids = collect_photo_ids(typed_input) if typed_input is not None else ()
    resolved_photo_refs: set[str] = set()
    for photo_id in typed_photo_ids:
        mapped = photo_map.get(photo_id)
        if mapped is not None:
            envelope_refs = tuple(dict.fromkeys([*envelope_refs, mapped]))
            resolved_photo_refs.add(mapped)

    modes = envelope.artifact_delivery_modes
    top_refs = set(
        [
            *envelope.input_refs,
            *envelope.context_summary_refs,
            *([envelope.prior_result_ref] if envelope.prior_result_ref else []),
            *([envelope.input_contract_ref] if envelope.input_contract_ref else []),
            *(str(ref) for ref in shared_refs),
            *(str(ref) for ref in (refs or ())),
        ]
    )
    capabilities: list[Capability] = []
    unreadable: list[str] = []
    readable: list[str] = []
    for ref in envelope_refs:
        _safe_public_ref(ref, workspace=workspace, run_id=envelope.run_id)
        if ref == isolated_template_ref:
            # The exact current-run snapshot is intentionally absent from
            # generic capabilities/readable_refs.  AgentRunner validates its
            # CAS binding and grants only one InspectDocumentTool call.
            continue
        mode: DeliveryMode = modes.get(
            ref,
            "reference"
            if ref in top_refs or ref in resolved_photo_refs
            else "inline",
        )
        try:
            descriptor = gateway.describe(ref)
        except Exception as exc:
            # Legacy typed contracts carry provenance refs such as
            # ``coverage.json`` which are intentionally embedded/inline and do
            # not grant a filesystem reader.  Current-run paths, top-level
            # prompt refs, and opaque refs are strict and fail closed.
            is_strict = (
                ref in top_refs
                or ref.startswith(_OPAQUE_PREFIX)
            )
            if not is_strict:
                unreadable.append(ref)
                continue
            raise ConfigurationError(
                f"{definition.id} cannot read declared artifact ref {ref}: {exc}"
            ) from exc
        operations = tuple(descriptor.allowed_operations)
        capabilities.append(
            Capability(
                canonical_ref=descriptor.canonical_ref,
                descriptor=descriptor,
                media_type=descriptor.media_type,
                logical_role=descriptor.logical_role,
                allowed_operations=operations,
                delivery_mode=mode,
            )
        )
        # Only reference delivery grants a model-side reopen.  Inline and
        # hash-retained values remain provenance/hash evidence, not readers.
        if mode == "reference":
            readable.append(descriptor.canonical_ref)

    declared_tools = list(dict.fromkeys(definition.tools))
    # Artifact tools are role-declared, capability-gated tools.  Do not expose
    # generic readers merely because a ref was present in an envelope.
    operation_refs = {
        operation
        for capability in capabilities
        if capability.delivery_mode == "reference"
        for operation in capability.allowed_operations
    }
    # Legacy reporting definitions often omitted the generic reader names and
    # relied on the envelope's attached artifact refs to activate them.  Keep
    # that behaviour, but only when a concrete reference capability supports
    # the operation; an empty task no longer receives a workspace reader.
    # Keep the historical generic-reader order (open, result, search) stable:
    # provider context fingerprints include the tool declaration sequence.
    # Capability gating only controls whether a concrete reader is present;
    # inspect_image remains role-declared even when no image capability was
    # delivered so legacy reporting definitions keep their contract while the
    # implementation rejects unscoped paths.
    for operation in ("open_artifact",):
        if operation in operation_refs and operation not in declared_tools:
            declared_tools.append(operation)
    if "open_artifact" in declared_tools and "open_artifact" not in operation_refs:
        declared_tools.remove("open_artifact")
    if "search_text" in declared_tools and "search_text" not in operation_refs:
        declared_tools.remove("search_text")
    # ``open_tool_result`` is a Runtime-owned lossless continuation reader, not
    # a Capability ToolDefinition: it can only read opaque references minted by
    # this run's tool-result truncation boundary.  Keep it always available for
    # backwards compatibility without making those internal refs part of a
    # Capability's static Agent/Task scopes.
    if "open_tool_result" not in declared_tools:
        declared_tools.append("open_tool_result")
    for operation in ("search_text",):
        if operation in operation_refs and operation not in declared_tools:
            declared_tools.append(operation)
    declared_tools_tuple = tuple(declared_tools)
    if envelope.allowed_tools:
        unknown = sorted(set(envelope.allowed_tools) - set(declared_tools_tuple))
        if unknown:
            raise ConfigurationError(
                f"{definition.id} task requested undeclared tools {unknown}"
            )
        allowed = {*envelope.allowed_tools, "open_tool_result"}
        tools = tuple(name for name in declared_tools_tuple if name in allowed)
    else:
        tools = declared_tools_tuple
    return CompiledAgentAccess(
        gateway,
        tuple(dict.fromkeys(readable)),
        tuple(dict.fromkeys(unreadable)),
        tools,
        tuple(capabilities),
        tuple(sorted(photo_map.items())),
    )


def scoped_gateway(
    root: ArtifactGateway,
    *,
    workflow_id: str,
    envelope: TaskEnvelope,
    agent_id: str,
    session_id: str,
) -> ArtifactGateway:
    return root.scoped(
        ArtifactGrant(workflow_id, envelope.task_id, agent_id, session_id)
    )


__all__ = [
    "Capability",
    "CompiledAgentAccess",
    "collect_artifact_refs",
    "collect_photo_ids",
    "collect_reference_refs",
    "collect_task_refs",
    "compile_agent_access",
    "scoped_gateway",
]
