"""Checklist review: a neutral Capability that reviews one document item by item."""

from pathlib import Path

from manyselves.kernel.definitions import (
    CapabilityDefinition,
    DefinitionRegistry,
    load_capability,
)

CAPABILITY_ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = CAPABILITY_ROOT / "capability.yaml"


def load_checklist_review_capability() -> tuple[CapabilityDefinition, DefinitionRegistry]:
    return load_capability(CAPABILITY_FILE)


__all__ = ["CAPABILITY_FILE", "CAPABILITY_ROOT", "load_checklist_review_capability"]
