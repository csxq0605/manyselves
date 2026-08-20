"""Distribution-reporting Capability definitions and compatibility adapters."""

from pathlib import Path

from manyselves.kernel.definitions import (
    CapabilityDefinition,
    DefinitionRegistry,
    load_capability,
)

from .adapters import load_reporting_agents

CAPABILITY_ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = CAPABILITY_ROOT / "capability.yaml"


def load_distribution_reporting_capability(
) -> tuple[CapabilityDefinition, DefinitionRegistry]:
    """Load the packaged distribution-reporting definition graph."""

    return load_capability(CAPABILITY_FILE)


__all__ = [
    "CAPABILITY_FILE",
    "CAPABILITY_ROOT",
    "load_distribution_reporting_capability",
    "load_reporting_agents",
]
