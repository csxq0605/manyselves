"""YAML, JSON, and Markdown frontmatter definition loading."""

import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from .models import (
    DEFINITION_MODELS,
    CapabilityDefinition,
    Definition,
    DefinitionKind,
)
from .registry import DefinitionRegistry


class DefinitionLoadError(ValueError):
    """Raised when an external definition cannot be parsed or validated."""


def _markdown_frontmatter(content: str, path: Path) -> tuple[dict[str, Any], str]:
    lines = content.splitlines()
    if not lines or lines[0].strip() != "---":
        raise DefinitionLoadError(f"{path}: missing YAML frontmatter")
    try:
        closing = next(
            index
            for index, line in enumerate(lines[1:], start=1)
            if line.strip() == "---"
        )
    except StopIteration as exc:
        raise DefinitionLoadError(f"{path}: unterminated YAML frontmatter") from exc
    try:
        payload = yaml.safe_load("\n".join(lines[1:closing])) or {}
    except yaml.YAMLError as exc:
        raise DefinitionLoadError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(payload, dict):
        raise DefinitionLoadError(f"{path}: top level must be an object")
    return payload, "\n".join(lines[closing + 1 :]).strip()


def _structured_payload(path: Path) -> tuple[dict[str, Any], str | None]:
    content = path.read_text(encoding="utf-8")
    suffix = path.suffix.lower()
    if suffix == ".md":
        return _markdown_frontmatter(content, path)
    if suffix in {".yaml", ".yml"}:
        try:
            payload = yaml.safe_load(content) or {}
        except yaml.YAMLError as exc:
            raise DefinitionLoadError(f"{path}: invalid YAML: {exc}") from exc
    elif suffix == ".json":
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise DefinitionLoadError(f"{path}: invalid JSON: {exc}") from exc
    else:
        raise DefinitionLoadError(f"{path}: unsupported definition format: {suffix}")
    if not isinstance(payload, dict):
        raise DefinitionLoadError(f"{path}: top level must be an object")
    return payload, None


def load_definition(
    path: Path,
    *,
    expected_kind: DefinitionKind | str | None = None,
) -> Definition:
    """Load and validate one external definition file."""

    source = Path(path)
    payload, markdown_body = _structured_payload(source)
    if expected_kind is not None:
        kind = DefinitionKind(expected_kind)
        payload.setdefault("kind", kind.value)
    else:
        raw_kind = payload.get("kind")
        if raw_kind is None:
            raise DefinitionLoadError(f"{source}: missing definition kind")
        try:
            kind = DefinitionKind(raw_kind)
        except ValueError as exc:
            raise DefinitionLoadError(f"{source}: unknown definition kind: {raw_kind}") from exc
    if markdown_body is not None and kind is DefinitionKind.AGENT:
        payload.setdefault("instructions", markdown_body)
    model = DEFINITION_MODELS[kind]
    try:
        return model.model_validate(payload)  # type: ignore[return-value]
    except ValidationError as exc:
        raise DefinitionLoadError(f"{source}: {exc}") from exc


_CAPABILITY_LOCATIONS: tuple[tuple[str, DefinitionKind], ...] = (
    ("agents", DefinitionKind.AGENT),
    ("workflows", DefinitionKind.WORKFLOW),
    ("tasks", DefinitionKind.TASK),
    ("contracts", DefinitionKind.CONTRACT),
    ("tools", DefinitionKind.TOOL),
    ("gates", DefinitionKind.GATE),
    ("recovery", DefinitionKind.RECOVERY),
)
_SUPPORTED_SUFFIXES = {".yaml", ".yml", ".json", ".md"}


def load_capability(path: Path) -> tuple[CapabilityDefinition, DefinitionRegistry]:
    """Load a capability entry file and all definition locations it indexes."""

    source = Path(path)
    loaded = load_definition(source, expected_kind=DefinitionKind.CAPABILITY)
    if not isinstance(loaded, CapabilityDefinition):
        raise DefinitionLoadError(f"{source}: expected capability definition")
    registry = DefinitionRegistry()
    registry.register(loaded)
    root = source.parent
    for field_name, kind in _CAPABILITY_LOCATIONS:
        location = root / getattr(loaded, field_name)
        if location.is_dir():
            definition_paths = sorted(
                child
                for child in location.iterdir()
                if child.is_file() and child.suffix.lower() in _SUPPORTED_SUFFIXES
            )
        elif location.is_file():
            definition_paths = [location]
        else:
            raise DefinitionLoadError(
                f"{source}: definition location does not exist: {location}"
            )
        for definition_path in definition_paths:
            registry.register(load_definition(definition_path, expected_kind=kind))
    registry.validate_references()
    return loaded, registry
