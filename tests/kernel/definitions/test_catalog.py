from pathlib import Path

import pytest

from manyselves.kernel.definitions import (
    CapabilityCatalog,
    CapabilityCatalogError,
    DefinitionKind,
    DefinitionReferenceError,
)


def _write_capability(root: Path, capability_id: str, workflow_id: str) -> Path:
    capability = root / capability_id
    for location in (
        "agents",
        "contracts",
        "gates",
        "recovery",
        "tasks",
        "tools",
        "workflows",
    ):
        (capability / location).mkdir(parents=True, exist_ok=True)
    (capability / "capability.yaml").write_text(
        "\n".join(
            [
                f"id: {capability_id}",
                "version: 1.0.0",
                f"description: {capability_id} fixture",
                "agents: ./agents",
                "workflows: ./workflows",
                "tasks: ./tasks",
                "contracts: ./contracts",
                "tools: ./tools",
                "gates: ./gates",
                "recovery: ./recovery",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (capability / "workflows" / "entry.yaml").write_text(
        "\n".join(
            [
                f"id: {workflow_id}",
                "version: 1.0.0",
                "description: fixture workflow",
                "actions: []",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return capability / "capability.yaml"


def test_catalog_discovers_capabilities_and_resolves_workflow_ownership(
    tmp_path: Path,
) -> None:
    _write_capability(tmp_path, "alpha", "alpha-entry")
    _write_capability(tmp_path, "beta", "beta-entry")

    catalog = CapabilityCatalog.discover([tmp_path])

    assert [item.definition.id for item in catalog.all()] == ["alpha", "beta"]
    loaded, workflow = catalog.require_workflow("beta-entry")
    assert loaded.definition.id == "beta"
    assert workflow.id == "beta-entry"
    assert loaded.registry.require(DefinitionKind.WORKFLOW, workflow.id) is workflow


def test_catalog_rejects_duplicate_public_workflow_ids(tmp_path: Path) -> None:
    _write_capability(tmp_path, "alpha", "shared-entry")
    _write_capability(tmp_path, "beta", "shared-entry")

    with pytest.raises(CapabilityCatalogError, match="duplicate workflow id"):
        CapabilityCatalog.discover([tmp_path])


def test_capability_entrypoints_must_reference_loaded_workflows(tmp_path: Path) -> None:
    source = _write_capability(tmp_path, "alpha", "alpha-entry")
    source.write_text(
        source.read_text(encoding="utf-8")
        + "entrypoints:\n- missing-entry\n",
        encoding="utf-8",
    )

    with pytest.raises(DefinitionReferenceError, match="missing-entry"):
        CapabilityCatalog.discover([tmp_path])


def test_builtin_catalog_exposes_both_production_capabilities() -> None:
    from manyselves.capabilities import load_builtin_capability_catalog

    catalog = load_builtin_capability_catalog()

    assert [item.definition.id for item in catalog.all()] == [
        "distribution-reporting",
        "parameter-adjustment",
    ]
