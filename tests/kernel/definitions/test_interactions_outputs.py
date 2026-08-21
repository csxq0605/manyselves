from pathlib import Path

from manyselves.kernel.definitions import DefinitionKind, load_capability

FIXTURE = (
    Path(__file__).parents[2]
    / "fixtures"
    / "capabilities"
    / "interaction_output"
    / "capability.yaml"
)


def test_capability_loads_interaction_and_output_definitions() -> None:
    capability, registry = load_capability(FIXTURE)

    assert capability.entrypoints == ["collect-name"]
    assert [item.id for item in registry.all(DefinitionKind.INTERACTION)] == [
        "request-name"
    ]
    assert [item.id for item in registry.all(DefinitionKind.OUTPUT)] == [
        "name-result"
    ]
