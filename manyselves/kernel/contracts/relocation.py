"""Fields submitted at the wrong level, put back where the contract declares them.

A model sometimes nests a field one level too deep (``considered`` inside the
``relations`` map, as if it were one of its keys) or one level too high.  The
contract itself says where each field belongs, so a field is moved when, and
only when, that is certain from the schema alone:

- the field is not declared where it was submitted (an extra key, or a key of
  a map whose value does not fit the map);
- exactly one object in the contract's fixed structure declares a property of
  that name (objects reached from the root through declared properties, not
  through list items or map values), and the submission does not fill it yet;
- the value is valid there.

Every field whose place is certain but whose value does not fit there yet is
named in a note, so the correction says where the field goes instead of only
what its entries lack under the wrong parent.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any

from jsonschema.validators import validator_for

Path = tuple[str, ...]


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    """Follow ``$ref`` and single-branch ``allOf``/``anyOf`` wrappers (``Optional``)."""

    seen = 0
    while isinstance(schema, dict) and seen < 20:
        seen += 1
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/"):
            node: Any = root
            for part in ref[2:].split("/"):
                node = node.get(part, {}) if isinstance(node, dict) else {}
            schema = node
            continue
        for key in ("allOf", "anyOf", "oneOf"):
            branches = [
                b for b in schema.get(key) or []
                if isinstance(b, dict) and b.get("type") != "null"
            ]
            if key in schema and len(branches) == 1:
                schema = branches[0]
                break
        else:
            return schema
    return schema if isinstance(schema, dict) else {}


def _objects(root: dict[str, Any]) -> dict[Path, dict[str, Any]]:
    """The fixed object positions of a schema: root and object properties, recursively."""

    found: dict[Path, dict[str, Any]] = {}

    def walk(schema: dict[str, Any], path: Path, depth: int) -> None:
        schema = _resolve(schema, root)
        if depth > 8 or not isinstance(schema.get("properties"), dict):
            return
        found[path] = schema
        for name, sub in schema["properties"].items():
            walk(sub, (*path, name), depth + 1)

    walk(root, (), 0)
    return found


def _valid(value: Any, schema: dict[str, Any], root: dict[str, Any]) -> bool:
    wrapped = {**schema, **{k: v for k, v in root.items() if k in ("$defs", "definitions")}}
    validator = validator_for(wrapped)(wrapped)
    return validator.is_valid(value)


def _at(value: Any, path: Path) -> Any:
    for part in path:
        value = value.get(part) if isinstance(value, dict) else None
    return value


def _holder(value: dict[str, Any], path: Path) -> dict[str, Any] | None:
    """The object at ``path``, an empty one made for each level not submitted."""

    node: Any = value
    for part in path:
        if not isinstance(node, dict):
            return None
        node = node.setdefault(part, {}) if part not in node else node[part]
    return node if isinstance(node, dict) else None


def _label(path: Path) -> str:
    return ".".join(path) or "最外层"


def relocate(value: Any, root: dict[str, Any]) -> tuple[Any, list[tuple[str, str]]]:
    """The value with misplaced fields moved, and notes on the ones that could not be.

    Each note is the field's submitted location and a line saying where it
    belongs.  The value is returned unchanged (a copy) when nothing can be moved.
    """

    if not isinstance(value, dict):
        return value, []
    positions = _objects(root)
    owners: dict[str, list[Path]] = {}
    for path, schema in positions.items():
        for name in schema["properties"]:
            owners.setdefault(name, []).append(path)
    moved = deepcopy(value)
    notes: list[tuple[str, str]] = []
    for path, schema in sorted(positions.items(), key=lambda kv: len(kv[0])):
        node = _at(moved, path)
        if not isinstance(node, dict):
            continue
        declared = schema["properties"]
        extra = schema.get("additionalProperties")
        candidates: list[tuple[dict[str, Any], str, Path]] = []
        for key in list(node):
            if key not in declared:
                candidates.append((node, key, path))
        # A key inside a map of this node whose value does not fit the map.
        for name, sub in declared.items():
            sub = _resolve(sub, root)
            inner = node.get(name)
            values = sub.get("additionalProperties")
            if isinstance(inner, dict) and isinstance(values, dict) and "properties" not in sub:
                for key in list(inner):
                    if not _valid(inner[key], values, root):
                        candidates.append((inner, key, (*path, name)))
        for holder, key, where in candidates:
            if holder is node and isinstance(extra, dict) and _valid(node[key], extra, root):
                continue
            targets = [t for t in owners.get(key, []) if t != where]
            if len(targets) != 1:
                continue
            (target,) = targets
            existing = _at(moved, target) if target else moved
            if existing is not None and (not isinstance(existing, dict) or key in existing):
                continue
            siblings = "、".join(n for n in positions[target]["properties"] if n != key)
            label = (
                f"{key} 应放在 {_label(target)} 里（与 {siblings[:60]} 平级），"
                f"不是 {_label(where)} 里面"
            )
            if not _valid(holder[key], positions[target]["properties"][key], root):
                notes.append((".".join((*where, key)), label))
                continue
            parent = _holder(moved, target)
            if parent is not None:
                parent[key] = holder.pop(key)
    return moved, notes


__all__ = ["relocate"]
