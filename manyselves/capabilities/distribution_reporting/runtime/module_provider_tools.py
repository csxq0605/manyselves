"""Artifact and calculation Tools used by the module Provider composition.

These implementations are mechanically shared by the Capability-owned module
Provider and the legacy Reporting runner.  The bounded artifact readers remain
the generic ``runtime.artifacts`` boundary; the only Capability-specific part is
the existing run-scoped completed-result wrapper.
"""

from __future__ import annotations

import ast
import operator
from pathlib import Path
from typing import Any

from manyselves.runtime.artifacts import ArtifactGateway, ToolContractError, parse_artifact
from manyselves.runtime.tools.artifact_tools import (
    OpenArtifactTool,
    OpenToolResultTool,
    SearchTextTool,
)
from manyselves.runtime.tools.registry import Tool
from manyselves.runtime.tools.result_memory import RunToolResultIndex


class InspectImageTool(Tool):
    name = "inspect_image"
    description = "Inspect dimensions and format of one project-local image."

    side_effect = "pure_read"
    parallel_safe = True

    def __init__(
        self,
        workspace: Path,
        *,
        gateway: ArtifactGateway | None = None,
        capabilities: tuple[Any, ...] | list[Any] = (),
        allowed_refs: tuple[str, ...] | list[str] = (),
        photo_refs: dict[str, str] | tuple[tuple[str, str], ...] | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        self.gateway = gateway
        self.capabilities = {
            item.canonical_ref: item for item in capabilities
        }
        self.allowed_refs = frozenset(str(ref) for ref in allowed_refs)
        self.photo_refs = dict(photo_refs or ())

    def _resolve_ref(self, value: str) -> tuple[str, Path, Any | None]:
        if not isinstance(value, str) or not value.strip():
            raise ToolContractError(
                "image reference is required",
                code="invalid_reference",
            )
        ref = value.strip()
        if ref.upper().startswith("P-") and "/" not in ref and "\\" not in ref:
            mapped = self.photo_refs.get(ref)
            if mapped is None:
                raise ToolContractError(
                    "image identifier is not in the current run PhotoAsset map",
                    code="image_scope_unresolved",
                    repair_code="resolve_image_scope",
                    details={"identifier": ref},
                )
            ref = mapped
        if ref not in self.allowed_refs:
            raise PermissionError(
                "image was not delivered by reference for this task"
            )
        capability = self.capabilities.get(ref)
        if capability is None and self.gateway is not None:
            descriptor = self.gateway.describe(ref)
            if descriptor.kind != "image" or "inspect_image" not in descriptor.allowed_operations:
                raise ToolContractError(
                    "artifact is not an inspectable image",
                    code="unsupported_operation",
                    repair_code="use_format_reader",
                    details={"ref": ref, "kind": descriptor.kind},
                )
        elif capability is not None:
            if capability.kind != "image" or not capability.allows_operation("inspect_image"):
                raise ToolContractError(
                    "artifact capability does not allow image inspection",
                    code="capability_denied",
                    details={"ref": ref},
                )
        if self.gateway is not None and ref.startswith("artifact:v1:"):
            target = self.gateway._resolve(ref)
        else:
            target = (self.workspace / ref).resolve()
        if not target.is_relative_to(self.workspace) or not target.is_file():
            raise PermissionError("image must resolve to a current workspace file")
        return ref, target, capability

    async def __call__(self, path: str | None = None, ref: str | None = None) -> dict:
        """Inspect an image.

        Args:
            path: Authorized project-relative image path or a current-run P-ID.
            ref: Alias for an authorized opaque/current image reference.
        """
        selected = ref if ref is not None else path
        canonical_ref, target, _capability = self._resolve_ref(selected or "")
        parsed = parse_artifact(target)
        return {
            "path": canonical_ref,
            "kind": parsed.kind,
            "metadata": parsed.blocks[0].text if parsed.blocks else "",
            "visual_verified": False,
            "error": parsed.error,
        }


class _IndexedOpenArtifactTool(OpenArtifactTool):
    """Run-scoped idempotency wrapper for the bounded artifact reader."""

    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(
        self, ref: str, offset: int = 0, limit: int | None = None
    ) -> dict:
        arguments = {"ref": ref, "offset": offset, "limit": limit}
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(ref=ref, offset=offset, limit=limit)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedSearchTextTool(SearchTextTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(
        self,
        ref: str,
        query: str,
        max_matches: int = 20,
        context_lines: int = 2,
    ) -> dict:
        arguments = {
            "ref": ref,
            "query": query,
            "max_matches": max_matches,
            "context_lines": context_lines,
        }
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(
            ref=ref,
            query=query,
            max_matches=max_matches,
            context_lines=context_lines,
        )
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedOpenToolResultTool(OpenToolResultTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(self, ref: str, offset: int = 0, limit: int = 8000) -> dict:
        arguments = {"ref": ref, "offset": offset, "limit": limit}
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=ref,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(ref=ref, offset=offset, limit=limit)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=ref,
            )
        return result


class _IndexedInspectImageTool(InspectImageTool):
    def __init__(self, *args, result_index: RunToolResultIndex, task_id: str, **kwargs):
        super().__init__(*args, **kwargs)
        self.result_index = result_index
        self.task_id = task_id

    async def __call__(self, path: str | None = None, ref: str | None = None) -> dict:
        arguments = {"path": path, "ref": ref}
        selected = ref if ref is not None else path
        existing = (
            self.result_index.lookup(
                self.task_id,
                self.name,
                arguments,
                ref=selected,
            )
            if getattr(self, "reuse_result", True)
            else None
        )
        if existing is not None and existing.get("status") == "completed":
            return existing.get("result")
        result = await super().__call__(path=path, ref=ref)
        if getattr(self, "reuse_result", True):
            self.result_index.record(
                self.task_id,
                self.name,
                arguments,
                result,
                status="completed",
                ref=selected,
            )
        return result


class CalculateTool(Tool):
    name = "calculate"
    description = "Evaluate a basic arithmetic expression with no names or code execution."
    side_effect = "pure_read"
    parallel_safe = True
    _ops = {
        ast.Add: operator.add,
        ast.Sub: operator.sub,
        ast.Mult: operator.mul,
        ast.Div: operator.truediv,
        ast.FloorDiv: operator.floordiv,
        ast.Mod: operator.mod,
        ast.Pow: operator.pow,
        ast.USub: operator.neg,
        ast.UAdd: operator.pos,
    }

    async def __call__(self, expression: str) -> dict:
        """Calculate a value.

        Args:
            expression: Arithmetic expression containing numbers and operators only.
        """

        def evaluate(node):
            if isinstance(node, ast.Expression):
                return evaluate(node.body)
            if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                return node.value
            if isinstance(node, ast.BinOp) and type(node.op) in self._ops:
                return self._ops[type(node.op)](evaluate(node.left), evaluate(node.right))
            if isinstance(node, ast.UnaryOp) and type(node.op) in self._ops:
                return self._ops[type(node.op)](evaluate(node.operand))
            raise ValueError("unsupported expression")

        return {"expression": expression, "result": evaluate(ast.parse(expression, mode="eval"))}


__all__ = [
    "CalculateTool",
    "InspectImageTool",
    "_IndexedInspectImageTool",
    "_IndexedOpenArtifactTool",
    "_IndexedOpenToolResultTool",
    "_IndexedSearchTextTool",
]
