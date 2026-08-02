"""Contract tests for the Phase 1 frontend OpenAPI boundary."""

import json
from pathlib import Path

from pydantic import SecretStr

from manyselves.webapi.main import create_app
from manyselves.webapi.settings import WebSettings
from scripts.export_openapi import render_openapi_document, write_or_check

CONTRACT_PATH = Path("frontend-contract/openapi.json")


def _test_settings() -> WebSettings:
    return WebSettings(
        data_root=Path("/manyselves/openapi-contract"),
        initial_project_id="openapi-contract-project",
        access_token=SecretStr("openapi-contract-secret"),
        allowed_origins=["https://openapi.invalid"],
    )


def test_openapi_has_required_resources() -> None:
    schema = create_app(_test_settings()).openapi()
    required = {
        "/api/v1/bootstrap",
        "/api/v1/events",
        "/api/v1/projects",
        "/api/v1/conversations",
        "/api/v1/agents/{agent_id}/messages",
        "/api/v1/operations/python",
        "/api/v1/maintenance/quiesce",
    }

    assert required <= set(schema["paths"])


def test_all_operations_have_unique_operation_ids() -> None:
    schema = create_app(_test_settings()).openapi()
    operations = [
        operation
        for path_item in schema["paths"].values()
        for operation in path_item.values()
        if isinstance(operation, dict) and "responses" in operation
    ]
    operation_ids = [operation.get("operationId") for operation in operations]

    assert None not in operation_ids
    assert len(operation_ids) == len(set(operation_ids))


def test_rendered_openapi_is_deterministic_and_contains_no_settings() -> None:
    first = render_openapi_document()
    second = render_openapi_document()

    assert first == second
    assert first.endswith("\n")
    assert json.loads(first)["openapi"]
    assert "not-a-deployment-secret" not in first
    assert "openapi-contract-data" not in first
    assert "openapi-contract-project" not in first


def test_check_mode_rejects_stale_artifact_without_overwriting(tmp_path: Path) -> None:
    artifact = tmp_path / "openapi.json"
    artifact.write_text('{"stale": true}\n', encoding="utf-8")

    assert write_or_check(artifact, check=True) is False
    assert artifact.read_text(encoding="utf-8") == '{"stale": true}\n'


def test_canonical_openapi_artifact_is_current() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_openapi_document()
