"""Neutral parameter-adjustment Capability and execution adapter."""

from pathlib import Path

from manyselves.kernel.definitions import (
    CapabilityDefinition,
    DefinitionRegistry,
    load_capability,
)

from .adapters import ParameterAdjustmentResult, execute_parameter_adjustment

CAPABILITY_ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = CAPABILITY_ROOT / "capability.yaml"


def load_parameter_adjustment_capability(
) -> tuple[CapabilityDefinition, DefinitionRegistry]:
    """Load the packaged parameter-adjustment definition graph."""

    return load_capability(CAPABILITY_FILE)


__all__ = [
    "CAPABILITY_FILE",
    "CAPABILITY_ROOT",
    "ParameterAdjustmentResult",
    "execute_parameter_adjustment",
    "load_parameter_adjustment_capability",
]
