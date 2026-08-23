import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

import pytest
from pydantic import TypeAdapter

from manyselves.capabilities.distribution_reporting import (
    load_distribution_reporting_capability,
)
from manyselves.kernel.contracts import ContractValidationError, build_contract_catalog
from manyselves.kernel.definitions import ContractDefinition, DefinitionKind

PREPARATION_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.preparation"
)
REPORTING_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.models.reporting"
)

CONTRACT_MODELS = {
    "project_manifest": (PREPARATION_MODULE, "ProjectManifest"),
    "parsed_artifacts": (PREPARATION_MODULE, "ParsedArtifacts"),
    "evidence_items": (REPORTING_MODULE, "EvidenceItems"),
    "coverage_matrix": (REPORTING_MODULE, "CoverageMatrix"),
    "output_artifacts": (REPORTING_MODULE, "OutputArtifacts"),
}


def test_preparation_models_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {PREPARATION_MODULE} as preparation",
                    f"import {REPORTING_MODULE} as reporting",
                    "print(json.dumps({",
                    "    'preparation': preparation.__name__,",
                    "    'reporting': reporting.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "preparation": PREPARATION_MODULE,
        "reporting": REPORTING_MODULE,
        "core_reporting": [],
    }


def test_preparation_models_are_physically_capability_owned() -> None:
    preparation = import_module(PREPARATION_MODULE)
    reporting = import_module(REPORTING_MODULE)

    for model_name in (
        "ManifestFile",
        "ProjectManifest",
        "ParsedArtifact",
        "FilePreparationResult",
        "MappingGap",
        "MappingResult",
    ):
        assert getattr(preparation, model_name).__module__ == PREPARATION_MODULE

    source = Path(preparation.__file__).read_text(encoding="utf-8")
    imports = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, (ast.Import, ast.ImportFrom))
    ]
    assert all(
        not any(
            name.name.startswith("manyselves.core.reporting")
            for name in node.names
        )
        if isinstance(node, ast.Import)
        else not (node.module or "").startswith("manyselves.core.reporting")
        for node in imports
    )

    for model_name in ("ManifestFile", "ProjectManifest", "ParsedArtifact"):
        assert model_name not in vars(reporting)

    def has_spec(name: str) -> bool:
        try:
            return find_spec(name) is not None
        except ModuleNotFoundError:
            return False

    assert not has_spec("manyselves.core.reporting.preparation")
    assert not has_spec("manyselves.core.reporting.mappers.common")


def test_preparation_models_preserve_nested_round_trip_and_extra_forbid() -> None:
    preparation = import_module(PREPARATION_MODULE)
    reporting = import_module(REPORTING_MODULE)

    manifest_file = preparation.ManifestFile(
        id="file-1",
        path=Path("Inputs/source.txt"),
        sha256="source-id",
        media_type="text/plain",
        purpose="supporting",
    )
    manifest = preparation.ProjectManifest(files=[manifest_file])
    parsed = preparation.ParsedArtifact(
        id="artifact-1",
        kind="text",
        source=reporting.SourceLocation(
            file_id="file-1",
            path=Path("Inputs/source.txt"),
        ),
        payload={"text": "evidence"},
    )
    file_result = preparation.FilePreparationResult(
        manifest_order=0,
        file_id="file-1",
        source_path=Path("Inputs/source.txt"),
        source_sha256="0" * 64,
        status="parsed",
        parsed_artifacts=[parsed],
        mapping_gaps=[{"code": "raw-gap", "custom": True}],
    )
    gap = preparation.MappingGap(code="missing", message="missing source")
    mapping = preparation.MappingResult(evidence_items=[], gaps=[gap])

    for value in (manifest, parsed, file_result, gap, mapping):
        assert type(value).model_validate(value.model_dump(mode="json")) == value
        with pytest.raises(ValueError):
            type(value).model_validate({**value.model_dump(mode="json"), "extra": True})


def test_preparation_contract_catalog_uses_typed_models_and_collection_shapes() -> None:
    _, registry = load_distribution_reporting_capability()
    contracts = build_contract_catalog(registry)
    preparation = import_module(PREPARATION_MODULE)
    reporting = import_module(REPORTING_MODULE)

    assert preparation.ParsedArtifacts == list[preparation.ParsedArtifact]
    assert reporting.EvidenceItems == list[reporting.EvidenceItem]
    assert reporting.OutputArtifacts == list[reporting.OutputArtifact]

    valid_values = {
        "project_manifest": {"files": []},
        "parsed_artifacts": [],
        "evidence_items": [],
        "coverage_matrix": {"entries": {}},
        "output_artifacts": [],
    }
    for contract_id, (module_name, model_name) in CONTRACT_MODELS.items():
        definition = registry.require(DefinitionKind.CONTRACT, contract_id)
        model_type = getattr(import_module(module_name), model_name)
        assert isinstance(definition, ContractDefinition)
        assert definition.adapter == "pydantic"
        assert definition.model == f"{module_name}:{model_name}"
        assert contracts[contract_id].json_schema() == TypeAdapter(
            model_type
        ).json_schema()
        contracts[contract_id].validate(valid_values[contract_id])

    for contract_id in ("parsed_artifacts", "evidence_items", "output_artifacts"):
        assert contracts[contract_id].json_schema()["type"] == "array"
        with pytest.raises(ContractValidationError):
            contracts[contract_id].validate({})
    for contract_id in ("project_manifest", "coverage_matrix"):
        assert contracts[contract_id].json_schema()["type"] == "object"
        with pytest.raises(ContractValidationError):
            contracts[contract_id].validate([])

    report_request = registry.require(DefinitionKind.CONTRACT, "report_request")
    assert isinstance(report_request, ContractDefinition)
    assert report_request.adapter == "json_schema"
    assert report_request.schema_ == {}
