"""Production-neutral parameter-adjustment Capability definitions."""

from pathlib import Path

from manyselves.kernel.definitions import (
    CapabilityDefinition,
    DefinitionRegistry,
    load_capability,
)

CAPABILITY_ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = CAPABILITY_ROOT / "capability.yaml"


def load_parameter_adjustment_capability(
) -> tuple[CapabilityDefinition, DefinitionRegistry]:
    """Load the packaged parameter-adjustment definition graph."""

    return load_capability(CAPABILITY_FILE)


__all__ = [
    "CAPABILITY_FILE",
    "CAPABILITY_ROOT",
    "load_parameter_adjustment_capability",
]
