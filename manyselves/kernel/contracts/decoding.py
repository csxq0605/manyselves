"""Lists and objects sent as their JSON text, read back where the contract says so.

A model given a tool that declares only "an object" sometimes fills a list
field with the list's JSON text (``"items": "[{...}]"``) instead of the list
itself, and sends it the same way again when corrected.  The contract says
what each position holds, so a string is read as JSON when, and only when,
that is certain from the schema alone:

- the schema at that position does not allow a string, and does allow an
  array or an object (``$ref`` and ``Optional`` are followed; a position whose
  type the schema leaves open, such as ``Any``, is not touched);
- the text parses as JSON, and gives a list where an array is allowed or an
  object where an object is allowed.

Anything else stays as submitted and is reported by validation as before.
Positions are followed through declared properties, map values and list
items, so items that are each sent as text, inside a list sent as text, are
read back too.  The parsed value still goes through the whole contract.
"""

from __future__ import annotations

import json
from typing import Any

from .relocation import _resolve

_MAX_DEPTH = 32
_KIND = {list: "array", dict: "object"}


def _types(schema: Any, root: dict[str, Any], depth: int = 0) -> set[str] | None:
    """The JSON types a position allows, or None when the schema leaves it open."""

    schema = _resolve(schema, root) if isinstance(schema, dict) else None
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None
    declared = schema.get("type")
    if isinstance(declared, str):
        return {declared}
    if isinstance(declared, list) and declared and all(isinstance(t, str) for t in declared):
        return set(declared)
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            found: set[str] = set()
            for branch in branches:
                allowed = _types(branch, root, depth + 1)
                if allowed is None:
                    return None
                found |= allowed
            return found
    return None


def _follow(schema: Any, root: dict[str, Any], kind: str, depth: int) -> dict[str, Any] | None:
    """The one schema a value of JSON type ``kind`` is checked against here."""

    schema = _resolve(schema, root) if isinstance(schema, dict) else None
    if not isinstance(schema, dict) or depth > _MAX_DEPTH:
        return None
    for key in ("anyOf", "oneOf"):
        branches = schema.get(key)
        if isinstance(branches, list) and branches:
            fitting = [
                b for b in branches if kind in (_types(b, root, depth + 1) or set())
            ]
            if len(fitting) != 1:
                return None
            return _follow(fitting[0], root, kind, depth + 1)
    allowed = _types(schema, root, depth)
    if allowed is not None and kind not in allowed:
        return None
    return schema


def _parsed(text: str, allowed: set[str]) -> Any:
    """The JSON value of ``text`` when it is a list or object the position allows."""

    stripped = text.strip()
    if stripped[:1] not in ("[", "{"):
        return None
    try:
        value = json.loads(stripped, strict=False)
    except (ValueError, RecursionError):
        return None
    return value if _KIND.get(type(value)) in allowed else None


def _label(path: tuple[Any, ...]) -> str:
    parts: list[str] = []
    for part in path:
        parts.append(f"[{part}]" if isinstance(part, int) else ("." if parts else "") + part)
    return "".join(parts) or "(根)"


def _walk(
    value: Any,
    schema: Any,
    root: dict[str, Any],
    path: tuple[Any, ...],
    read: list[str],
    depth: int,
) -> Any:
    if depth > _MAX_DEPTH:
        return value
    if isinstance(value, str):
        allowed = _types(schema, root)
        if not allowed or "string" in allowed or not allowed & {"array", "object"}:
            return value
        parsed = _parsed(value, allowed)
        if parsed is None:
            return value
        read.append(_label(path))
        value = parsed
    if isinstance(value, dict):
        followed = _follow(schema, root, "object", depth)
        if followed is None:
            return value
        declared = followed.get("properties")
        declared = declared if isinstance(declared, dict) else {}
        extra = followed.get("additionalProperties")
        result: dict[str, Any] = {}
        for key, item in value.items():
            sub = declared.get(key) if key in declared else extra
            result[key] = (
                _walk(item, sub, root, (*path, key), read, depth + 1)
                if isinstance(sub, dict) else item
            )
        return result
    if isinstance(value, list):
        followed = _follow(schema, root, "array", depth)
        if followed is None:
            return value
        prefix = followed.get("prefixItems")
        items = followed.get("items")
        out: list[Any] = []
        for index, item in enumerate(value):
            if isinstance(prefix, list) and index < len(prefix):
                sub = prefix[index]
            else:
                sub = items
            out.append(
                _walk(item, sub, root, (*path, index), read, depth + 1)
                if isinstance(sub, dict) else item
            )
        return out
    return value


def read_json_text(value: Any, schema: dict[str, Any]) -> tuple[Any, list[str]]:
    """The value with lists and objects sent as JSON text read back, and where.

    Each entry of the second element is the location of one string that was
    read back.  The value itself is returned, not a copy, when there is none.
    """

    read: list[str] = []
    result = _walk(value, schema, schema, (), read, 0)
    return (result, read) if read else (value, [])


__all__ = ["read_json_text"]
