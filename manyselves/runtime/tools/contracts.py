"""Local tool-call contract validation and mechanical, fail-closed repairs.

Provider transcripts occasionally contain a visible project path where a
continuation tool requires an opaque result reference.  This module contains
the deliberately tiny whitelist that can repair that deterministic shape.  It
never broadens a capability set and never guesses a path for a bare P-ID.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Callable

from ..artifacts.gateway import ToolContractError


_OPAQUE_PREFIX = "artifact:v1:"


def _capability_refs(capability: Any) -> frozenset[str] | None:
    """Extract an explicitly delivered ref set without accepting global policy."""

    if capability is None:
        return None
    if isinstance(capability, Mapping):
        for key in ("readable_refs", "allowed_refs", "refs", "artifact_refs"):
            if key in capability:
                value = capability[key]
                if value is None:
                    return frozenset()
                return frozenset(str(item) for item in value)
        return frozenset()
    for name in ("readable_refs", "allowed_refs", "artifact_refs"):
        if hasattr(capability, name):
            value = getattr(capability, name)
            if value is None:
                return frozenset()
            return frozenset(str(item) for item in value)
    if callable(getattr(capability, "allows", None)):
        return None
    try:
        return frozenset(str(item) for item in capability)
    except TypeError:
        return frozenset()


def _is_authorized(ref: str, refs: frozenset[str] | None, capability: Any) -> bool:
    if refs is not None:
        return ref in refs
    if capability is not None and callable(getattr(capability, "allows", None)):
        try:
            return bool(capability.allows(ref))
        except Exception:
            return False
    # Absence of an explicit set is intentionally not authorization for a
    # public path.  Opaque refs are checked by the gateway's signed identity.
    return False


class NormalizedToolCall(dict):
    """Mapping result with attribute access for callers using either style."""

    @property
    def name(self) -> str:
        return str(self.get("name") or self.get("tool_name") or "")

    @property
    def tool_name(self) -> str:
        return self.name

    @property
    def arguments(self) -> dict[str, Any]:
        value = self.get("arguments")
        return value if isinstance(value, dict) else {}

    @property
    def changed(self) -> bool:
        return bool(self.get("changed", False))

    @property
    def accepted(self) -> bool:
        return bool(self.get("accepted", False))


def _call_parts(
    tool_name: str | Mapping[str, Any] | Any,
    arguments: Mapping[str, Any] | None,
) -> tuple[str, dict[str, Any], dict[str, Any] | None]:
    if isinstance(tool_name, Mapping):
        raw = dict(tool_name)
        name = str(raw.get("name") or raw.get("tool_name") or "")
        args = raw.get("arguments")
        if args is None:
            args = raw.get("args")
        if args is None:
            args = {}
        return name, dict(args) if isinstance(args, Mapping) else {}, raw
    return str(tool_name), dict(arguments or {}), None


def normalize_tool_call(
    tool_name: str | Mapping[str, Any] | Any,
    arguments: Mapping[str, Any] | None = None,
    authorized_refs: Iterable[str] | None = None,
    *,
    allowed_refs: Iterable[str] | None = None,
    capability: Any = None,
    image_resolver: Callable[[str], Any] | None = None,
    resolve_image_ref: Callable[[str], Any] | None = None,
) -> NormalizedToolCall:
    """Normalize one provider tool call using only explicit local facts.

    The only automatic rewrite is ``open_tool_result(public_ref)`` to
    ``open_artifact(public_ref)`` when that exact ref was delivered in the
    current capability.  A non-opaque, unauthorized continuation remains
    rejected.  ``inspect_image`` accepts a P-ID only when a caller supplies a
    resolver callback that returns an explicit, authorized path/ref.
    """

    name, args, original = _call_parts(tool_name, arguments)
    refs = (
        frozenset(str(ref) for ref in authorized_refs)
        if authorized_refs is not None
        else frozenset(str(ref) for ref in allowed_refs)
        if allowed_refs is not None
        else _capability_refs(capability)
    )
    result = NormalizedToolCall(
        {
            "name": name,
            "tool_name": name,
            "arguments": dict(args),
            "changed": False,
            "accepted": True,
            "error": None,
            "original_name": name,
            "original_arguments": dict(args),
        }
    )
    if original is not None:
        # Preserve provider call metadata (id/type) without allowing it to
        # override normalized name/arguments.
        for key in ("id", "type"):
            if key in original:
                result[key] = original[key]

    ref = args.get("ref")
    if name == "open_tool_result" and isinstance(ref, str):
        if ref.startswith(_OPAQUE_PREFIX):
            return result
        if _is_authorized(ref, refs, capability):
            result.update(
                {
                    "name": "open_artifact",
                    "tool_name": "open_artifact",
                    "changed": True,
                    "repair_code": "public_ref_to_open_artifact",
                }
            )
            return result
        error = ToolContractError(
            "open_tool_result requires a delivered opaque artifact reference",
            code="non_opaque_reference",
            repair_code="use_open_artifact",
            details={"ref": ref, "required_tool": "open_artifact"},
        )
        result.update(
            {
                "accepted": False,
                "error": error.as_dict(),
                "code": error.code,
                "repair_code": error.repair_code,
            }
        )
        return result
    if name == "open_tool_result" and ref is not None:
        error = ToolContractError(
            "open_tool_result ref must be a string",
            code="invalid_reference",
            repair_code="use_open_artifact",
        )
        result.update({"accepted": False, "error": error.as_dict(), "code": error.code})
        return result

    if name == "inspect_image":
        image_key = "path" if args.get("path") is not None else "ref"
        image_ref = args.get(image_key)
        resolver = image_resolver or resolve_image_ref
        # P-IDs are identifiers, not project paths.  Never turn one into a
        # guessed ``Inputs/P-0013`` path; only an explicit resolver may do so.
        if isinstance(image_ref, str) and image_ref.upper().startswith("P-"):
            if resolver is None:
                error = ToolContractError(
                    "image identifier requires an explicit scope resolver",
                    code="image_scope_unresolved",
                    repair_code="resolve_image_scope",
                    details={"identifier": image_ref},
                )
                result.update({"accepted": False, "error": error.as_dict(), "code": error.code})
                return result
            try:
                resolved = resolver(image_ref)
            except Exception as exc:
                error = ToolContractError(
                    "image scope resolver failed",
                    code="image_scope_unresolved",
                    repair_code="resolve_image_scope",
                    details={"identifier": image_ref, "reason": str(exc)},
                )
                result.update({"accepted": False, "error": error.as_dict(), "code": error.code})
                return result
            if isinstance(resolved, Mapping):
                resolved_ref = resolved.get("path") or resolved.get("ref")
            else:
                resolved_ref = resolved
            if not isinstance(resolved_ref, str) or not _is_authorized(resolved_ref, refs, capability):
                error = ToolContractError(
                    "resolved image is not delivered by this task",
                    code="capability_denied",
                    repair_code="resolve_image_scope",
                    details={"identifier": image_ref, "resolved_ref": resolved_ref},
                )
                result.update({"accepted": False, "error": error.as_dict(), "code": error.code})
                return result
            repaired_args = dict(args)
            repaired_args[image_key] = resolved_ref
            result.update(
                {
                    "arguments": repaired_args,
                    "changed": True,
                    "repair_code": "resolved_image_scope",
                }
            )
    return result


# Spelling used by a few local callers and tests.
normalize_tool_call_contract = normalize_tool_call
mechanical_tool_correction = normalize_tool_call


__all__ = [
    "NormalizedToolCall",
    "ToolContractError",
    "mechanical_tool_correction",
    "normalize_tool_call",
    "normalize_tool_call_contract",
]
