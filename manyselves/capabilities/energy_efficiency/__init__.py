"""Energy efficiency: find candidate energy-loss items in meter data.

The model finds and explains candidate loss items; the program parses the
meter data, computes every number, replays each item's finding method over
the whole data period and exports the candidate register.  Which items are
kept is decided by a person or by rules the person wrote, never by the model.
"""

from pathlib import Path

from manyselves.kernel.definitions import (
    CapabilityDefinition,
    DefinitionRegistry,
    load_capability,
)

CAPABILITY_ROOT = Path(__file__).resolve().parent
CAPABILITY_FILE = CAPABILITY_ROOT / "capability.yaml"


def load_energy_efficiency_capability() -> tuple[CapabilityDefinition, DefinitionRegistry]:
    return load_capability(CAPABILITY_FILE)


__all__ = ["CAPABILITY_FILE", "CAPABILITY_ROOT", "load_energy_efficiency_capability"]
