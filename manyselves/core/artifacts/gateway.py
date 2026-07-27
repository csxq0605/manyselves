"""Capability-scoped, bounded access to project and internal artifacts."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from pathlib import Path

from ..access_policy import reject_forbidden_agent_document

MAX_ARTIFACT_PAGE_CHARS = 160_000


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

    def as_dict(self) -> dict:
        return {
            "ref": self.ref,
            "content": self.content,
            "offset": self.offset,
            "returned": self.returned,
            "total": self.total,
            "truncated": self.truncated,
            "next_offset": self.next_offset,
        }


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

    def persist_internal(self, kind: str, key: str, content: str) -> str:
        safe_kind = "".join(c if c.isalnum() or c in "-_" else "_" for c in kind)
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        target = self.workspace / ".manyselves" / "artifacts" / safe_kind / f"{digest}.txt"
        target.parent.mkdir(parents=True, exist_ok=True)
        if not target.exists():
            target.write_text(content, encoding="utf-8")
        payload = {
            "workflow_id": self.grant.workflow_id,
            "task_id": self.grant.task_id,
            "agent_id": self.grant.agent_id,
            "session_id": self.grant.session_id,
            "kind": safe_kind,
            "key": key,
            "path": target.relative_to(self.workspace).as_posix(),
        }
        return self._encode(payload)

    def _resolve(self, ref: str) -> Path:
        if not ref.startswith("artifact:v1:"):
            return self._resolve_public(ref)
        payload = self._decode(ref)
        identity = {
            "workflow_id": self.grant.workflow_id,
            "task_id": self.grant.task_id,
            "agent_id": self.grant.agent_id,
            "session_id": self.grant.session_id,
        }
        if any(payload.get(key) != value for key, value in identity.items()):
            raise PermissionError("artifact reference belongs to another workflow session")
        target = (self.workspace / payload["path"]).resolve()
        internal_root = self.workspace / ".manyselves" / "artifacts"
        if not target.is_relative_to(internal_root) or not target.is_file():
            raise PermissionError("invalid internal artifact target")
        return target

    def open(self, ref: str, *, offset: int = 0, limit: int = 4000) -> ArtifactPage:
        if offset < 0 or not 1 <= limit <= MAX_ARTIFACT_PAGE_CHARS:
            raise ValueError(
                "offset must be >= 0 and limit must be between 1 and "
                f"{MAX_ARTIFACT_PAGE_CHARS}"
            )
        content = self._resolve(ref).read_text(encoding="utf-8")
        page = content[offset : offset + limit]
        next_offset = offset + len(page) if offset + len(page) < len(content) else None
        return ArtifactPage(ref, page, offset, len(page), len(content), next_offset is not None, next_offset)

    def open_internal(self, ref: str, *, offset: int = 0, limit: int = 4000) -> ArtifactPage:
        if not ref.startswith("artifact:v1:"):
            raise PermissionError("open_internal requires an opaque reference")
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
        lines = self._resolve(ref).read_text(encoding="utf-8").splitlines()
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
