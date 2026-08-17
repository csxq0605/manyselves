"""Capability-scoped, bounded access to project and internal artifacts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Mapping

from ..access_policy import reject_forbidden_agent_document
from .types import ArtifactDescriptor, descriptor_for_path, infer_logical_role

MAX_ARTIFACT_PAGE_CHARS = 160_000


class ToolContractError(RuntimeError, ValueError):
    """A fail-closed, provider-visible tool contract failure.

    The exception is intentionally also a ``ValueError`` for compatibility
    with the old ``read``/``open_artifact`` callers.  ``as_dict`` is the
    structured form used by tool adapters; it never asks a model to infer a
    path or retry an unsupported operation.
    """

    def __init__(
        self,
        message: str = "tool contract violation",
        *,
        category: str = "tool_contract",
        code: str = "contract_violation",
        retryable: bool = False,
        repair_code: str | None = None,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        self.category = str(category)
        self.code = str(code)
        self.message = str(message)
        self.retryable = bool(retryable)
        self.repair_code = repair_code
        self.details = dict(details or {})
        super().__init__(self.message)

    def as_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "category": self.category,
            "code": self.code,
            "message": self.message,
            "retryable": self.retryable,
        }
        if self.repair_code is not None:
            payload["repair_code"] = self.repair_code
        if self.details:
            payload["details"] = dict(self.details)
        return payload

    to_dict = as_dict


def contract_error_result(error: ToolContractError, **extra: Any) -> dict[str, Any]:
    """Return a stable result envelope without losing structured error fields."""

    payload: dict[str, Any] = {
        "status": "failed",
        "error": error.as_dict(),
        "code": error.code,
        "message": error.message,
        "retryable": error.retryable,
    }
    if error.repair_code is not None:
        payload["repair_code"] = error.repair_code
    payload.update(extra)
    return payload


@dataclass(frozen=True)
class ArtifactGrant:
    workflow_id: str
    task_id: str
    agent_id: str
    session_id: str


@dataclass(frozen=True)
class ArtifactPage:
    ref: str
    content: str
    offset: int
    returned: int
    total: int
    truncated: bool
    next_offset: int | None
    descriptor: ArtifactDescriptor | None = None

    def as_dict(self) -> dict:
        payload = {
            "ref": self.ref,
            "content": self.content,
            "offset": self.offset,
            "returned": self.returned,
            "total": self.total,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
        }
        if self.descriptor is not None:
            payload["descriptor"] = self.descriptor.as_dict()
        return payload


class ArtifactGateway:
    """Open project paths and opaque internal results through one authority."""

    def __init__(self, workspace: Path, grant: ArtifactGrant, *, secret: bytes | None = None):
        self.workspace = Path(workspace).resolve()
        self.grant = grant
        self._secret = secret or self._load_workspace_secret()

    def _load_workspace_secret(self) -> bytes:
        key_path = self.workspace / ".manyselves" / "artifact-gateway.key"
        if key_path.exists():
            key = key_path.read_bytes()
            if len(key) == 32:
                return key
        key = secrets.token_bytes(32)
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_bytes(key)
        return key

    def scoped(self, grant: ArtifactGrant) -> "ArtifactGateway":
        return ArtifactGateway(self.workspace, grant, secret=self._secret)

    def _resolve_public(self, ref: str) -> Path:
        reject_forbidden_agent_document(ref)
        target = (self.workspace / ref).resolve()
        if not target.is_relative_to(self.workspace):
            raise PermissionError("artifact is outside the project workspace")
        internal = self.workspace / ".manyselves"
        if target == internal or target.is_relative_to(internal):
            raise PermissionError("raw .manyselves paths are never artifact authority")
        if not target.is_file():
            raise FileNotFoundError(ref)
        reject_forbidden_agent_document(target)
        return target

    def _resolve_with_payload(self, ref: str) -> tuple[Path, dict[str, Any] | None]:
        """Resolve a public path or an authorized opaque reference."""

        if not isinstance(ref, str) or not ref.strip():
            raise ToolContractError(
                "artifact reference is required",
                code="invalid_reference",
                details={"ref": ref},
            )
        if ref.startswith("artifact:v1:"):
            payload = self._decode(ref)
            self._check_identity(payload)
            path_value = payload.get("path")
            if not isinstance(path_value, str) or not path_value:
                raise ToolContractError(
                    "opaque artifact reference has no path",
                    code="invalid_reference",
                )
            target = (self.workspace / path_value).resolve()
            internal_root = (self.workspace / ".manyselves" / "artifacts").resolve()
            if not target.is_relative_to(internal_root) or not target.is_file():
                raise ToolContractError(
                    "invalid internal artifact target",
                    code="invalid_reference",
                )
            return target, payload
        if ref.startswith("artifact:"):
            raise ToolContractError(
                "unsupported artifact reference version",
                code="invalid_reference",
                details={"ref": ref},
            )
        return self._resolve_public(ref), None

    def _encode(self, payload: dict) -> str:
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        body = base64.urlsafe_b64encode(raw).decode().rstrip("=")
        signature = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
        return f"artifact:v1:{body}:{signature}"

    def _decode(self, ref: str) -> dict:
        try:
            prefix, version, body, signature = ref.split(":", 3)
            if (prefix, version) != ("artifact", "v1"):
                raise ValueError
            expected = hmac.new(self._secret, body.encode(), hashlib.sha256).hexdigest()
            if not hmac.compare_digest(signature, expected):
                raise PermissionError("invalid artifact reference signature")
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            return json.loads(raw)
        except PermissionError:
            raise
        except Exception as exc:
            raise ValueError("invalid opaque artifact reference") from exc

    def _check_identity(
        self,
        payload: Mapping[str, Any],
        identity: ArtifactGrant | Mapping[str, Any] | None = None,
    ) -> None:
        grant = identity or self.grant
        if isinstance(grant, ArtifactGrant):
            expected = {
                "workflow_id": grant.workflow_id,
                "task_id": grant.task_id,
                "agent_id": grant.agent_id,
                "session_id": grant.session_id,
            }
        else:
            expected = {
                key: grant.get(key)
                for key in ("workflow_id", "task_id", "agent_id", "session_id")
                if key in grant
            }
        if any(payload.get(key) != value for key, value in expected.items()):
            # Preserve the long-standing permission boundary for callers that
            # distinguish a bad session from a malformed operation.  New
            # callers can still use ``describe`` with a matching identity and
            # receive the typed descriptor/contract errors below.
            raise PermissionError("artifact reference belongs to another workflow session")

    def _descriptor_from_path(
        self,
        ref: str,
        target: Path,
        payload: Mapping[str, Any] | None,
    ) -> ArtifactDescriptor:
        # Do not expose internal .manyselves paths through descriptors returned
        # to an agent.  The opaque ref remains the canonical identity.
        canonical = ref
        path = None if payload is not None else target.relative_to(self.workspace).as_posix()
        role = str(payload.get("logical_role") or "") if payload else ""
        descriptor = descriptor_for_path(
            target,
            canonical_ref=canonical,
            logical_role=(role or infer_logical_role(path)),
        )
        if payload is None:
            # ``descriptor_for_path`` uses the concrete path for standalone
            # callers; gateway responses use the project-relative public ref
            # so an absolute host path never enters the prompt.
            descriptor = replace(descriptor, path=path)
        declared_sha = payload.get("sha256") if payload else None
        declared_size = payload.get("size_bytes") if payload else None
        if declared_sha is not None and str(declared_sha).casefold() != descriptor.sha256:
            raise ToolContractError(
                "artifact content hash does not match its opaque reference",
                code="invalid_reference",
                details={"ref": ref, "expected_sha256": declared_sha, "actual_sha256": descriptor.sha256},
            )
        if declared_size is not None and int(declared_size) != descriptor.size_bytes:
            raise ToolContractError(
                "artifact size does not match its opaque reference",
                code="invalid_reference",
                details={"ref": ref, "expected_size_bytes": declared_size, "actual_size_bytes": descriptor.size_bytes},
            )
        return descriptor

    def describe(
        self,
        ref: str,
        identity: ArtifactGrant | Mapping[str, Any] | None = None,
    ) -> ArtifactDescriptor:
        """Describe a public or opaque ref without decoding its payload as text."""

        target, payload = self._resolve_with_payload(ref)
        if payload is not None:
            self._check_identity(payload, identity)
        return self._descriptor_from_path(ref, target, payload)

    def persist_internal(self, kind: str, key: str, content: str | bytes) -> str:
        safe_kind = "".join(c if c.isalnum() or c in "-_" else "_" for c in kind)
        raw = content.encode("utf-8") if isinstance(content, str) else bytes(content)
        digest = hashlib.sha256(raw).hexdigest()
        target = self.workspace / ".manyselves" / "artifacts" / safe_kind / f"{digest}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_bytes(raw)
        payload = {
            "workflow_id": self.grant.workflow_id,
            "task_id": self.grant.task_id,
            "agent_id": self.grant.agent_id,
            "session_id": self.grant.session_id,
            "kind": safe_kind,
            "logical_role": "tool_result",
            "key": key,
            "path": target.relative_to(self.workspace).as_posix(),
            "sha256": digest,
            "size_bytes": len(raw),
        }
        return self._encode(payload)

    def _resolve(self, ref: str) -> Path:
        target, _payload = self._resolve_with_payload(ref)
        return target

    def open(self, ref: str, *, offset: int = 0, limit: int = 4000) -> ArtifactPage:
        if offset < 0 or not 1 <= limit <= MAX_ARTIFACT_PAGE_CHARS:
            raise ValueError(
                "offset must be >= 0 and limit must be between 1 and "
                f"{MAX_ARTIFACT_PAGE_CHARS}"
            )
        descriptor = self.describe(ref)
        if descriptor.kind != "text":
            required = descriptor.required_tool or (
                "inspect_image" if descriptor.kind == "image" else "inspect_document"
            )
            raise ToolContractError(
                f"{descriptor.kind} artifacts cannot be opened as UTF-8 text",
                code="unsupported_operation",
                repair_code="use_format_reader",
                details={
                    "ref": ref,
                    "canonical_ref": descriptor.canonical_ref,
                    "kind": descriptor.kind,
                    "media_type": descriptor.media_type,
                    "required_tool": required,
                    "allowed_operations": list(descriptor.allowed_operations),
                },
            )
        try:
            content = self._resolve(ref).read_text(encoding="utf-8")
        except UnicodeDecodeError as exc:
            # A descriptor normally catches this before the read.  Keep a
            # second fail-closed guard for files changed between describe/open.
            raise ToolContractError(
                "artifact is not valid UTF-8 text",
                code="unsupported_operation",
                repair_code="use_format_reader",
                details={"ref": ref, "required_tool": "manual_review"},
            ) from exc
        page = content[offset : offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(content) else None
        return ArtifactPage(
            ref,
            page,
            offset,
            len(page),
            len(content),
            next_offset is not None,
            next_offset,
            descriptor,
        )

    def open_internal(self, ref: str, *, offset: int = 0, limit: int = 4000) -> ArtifactPage:
        if not ref.startswith("artifact:v1:"):
            raise ToolContractError(
                "open_tool_result requires an opaque artifact reference",
                code="non_opaque_reference",
                repair_code="use_open_artifact",
                details={"ref": ref, "required_tool": "open_artifact"},
            )
        return self.open(ref, offset=offset, limit=limit)

    def search(
        self,
        ref: str,
        query: str,
        *,
        max_matches: int = 20,
        context_lines: int = 2,
    ) -> dict:
        if not query:
            raise ValueError("query is required")
        if not 1 <= max_matches <= 50 or not 0 <= context_lines <= 10:
            raise ValueError("search bounds exceeded")
        descriptor = self.describe(ref)
        if descriptor.kind != "text":
            required = descriptor.required_tool or (
                "inspect_image" if descriptor.kind == "image" else "inspect_document"
            )
            raise ToolContractError(
                f"{descriptor.kind} artifacts cannot be searched as text",
                code="unsupported_operation",
                repair_code="use_format_reader",
                details={"ref": ref, "required_tool": required, "kind": descriptor.kind},
            )
        try:
            lines = self._resolve(ref).read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError as exc:
            raise ToolContractError(
                "artifact is not valid UTF-8 text",
                code="unsupported_operation",
                repair_code="use_format_reader",
                details={"ref": ref, "required_tool": "manual_review"},
            ) from exc
        needle = query.casefold()
        matches = []
        for index, line in enumerate(lines):
            if needle not in line.casefold():
                continue
            start = max(0, index - context_lines)
            end = min(len(lines), index + context_lines + 1)
            matches.append({"line": index + 1, "context": "\n".join(lines[start:end])})
            if len(matches) >= max_matches:
                break
        return {"ref": ref, "query": query, "matches": matches, "truncated": len(matches) == max_matches}
