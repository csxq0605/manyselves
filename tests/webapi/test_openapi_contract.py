"""Contract tests for the Phase 1 frontend OpenAPI boundary."""

import json
import re
import subprocess
import sys
from inspect import signature
from pathlib import Path

from fastapi.routing import APIRoute

from manyselves.webapi.main import create_app, generate_operation_id
from manyselves.webapi.security import require_authenticated_session
from manyselves.webapi.settings import WebSettings
from scripts.export_openapi import render_openapi_document, write_or_check

CONTRACT_PATH = Path("frontend-contract/openapi.json")


def _test_settings() -> WebSettings:
    return WebSettings(
        data_root=Path("/manyselves/openapi-contract"),
        initial_project_id="openapi-contract-project",
        allowed_origins=["https://openapi.invalid"],
    )


def test_openapi_has_required_resources() -> None:
    schema = create_app(_test_settings()).openapi()
    required = {
        "/api/v1/bootstrap",
        "/api/v1/events",
        "/api/v1/projects",
        "/api/v1/global-knowledge/files/tree",
        "/api/v1/conversations",
        "/api/v1/agents/{agent_id}/messages",
        "/api/v1/operations/python",
        "/api/v1/maintenance/quiesce",
    }

    assert required <= set(schema["paths"])


def test_phase1_redesign_contract_exposes_authenticated_global_knowledge_routes() -> None:
    """The browser's authenticated knowledge workspace must remain generated-client safe."""
    schema = create_app(_test_settings()).openapi()
    paths = schema["paths"]

    required = {
        "/api/v1/auth/login",
        "/api/v1/auth/session",
        "/api/v1/global-knowledge/files/tree",
        "/api/v1/global-knowledge/files/upload",
        "/api/v1/global-knowledge/files/content",
        "/api/v1/global-knowledge/files/preview",
        "/api/v1/global-knowledge/files/download",
        "/api/v1/global-knowledge/files/entries",
    }
    assert required <= set(paths)
    assert "SessionCookie" in schema["components"]["securitySchemes"]
    assert "DeploymentBearer" not in schema["components"]["securitySchemes"]


def test_openapi_exporter_defaults_to_the_canonical_artifact() -> None:
    """Release automation can check the frontend contract without a duplicate path."""
    repository_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [sys.executable, "scripts/export_openapi.py", "--check"],
        capture_output=True,
        cwd=repository_root,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_business_routes_declare_the_session_dependency_once_at_router_level() -> None:
    """Duplicate route and endpoint dependencies would verify the session twice."""
    app = create_app(_test_settings())
    anonymous_prefixes = ("/api/v1/auth/", "/api/v1/health/")
    business_routes = []
    for included_router in app.routes:
        router = included_router.original_router
        for route in router.routes:
            if not isinstance(route, APIRoute):
                continue
            path = f"{included_router.include_context.prefix}{route.path}"
            if path.startswith(anonymous_prefixes):
                continue
            dependencies = (
                *included_router.include_context.dependencies,
                *route.dependant.dependencies,
            )
            business_routes.append((path, route, dependencies))

    assert business_routes
    for path, route, dependencies in business_routes:
        session_dependencies = [
            dependency
            for dependency in dependencies
            if getattr(dependency, "dependency", getattr(dependency, "call", None))
            is require_authenticated_session
        ]
        assert len(session_dependencies) == 1, path
        assert "_access" not in signature(route.endpoint).parameters


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


def test_all_operation_ids_depend_only_on_public_method_and_path() -> None:
    schema = create_app(_test_settings()).openapi()

    for path, path_item in schema["paths"].items():
        path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", path).strip("_").lower()
        for method, operation in path_item.items():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            assert operation["operationId"] == f"{method.lower()}_{path_slug}"


def test_operation_id_is_independent_of_endpoint_function_name() -> None:
    def first_handler() -> None:
        pass

    def renamed_handler() -> None:
        pass

    first = APIRoute(
        "/api/v1/widgets/{widget_id}",
        first_handler,
        methods=["GET"],
    )
    renamed = APIRoute(
        "/api/v1/widgets/{widget_id}",
        renamed_handler,
        methods=["GET"],
    )

    assert generate_operation_id(first) == "get_api_v1_widgets_widget_id"
    assert generate_operation_id(renamed) == "get_api_v1_widgets_widget_id"


def test_rendered_openapi_is_deterministic_and_contains_no_settings() -> None:
    first = render_openapi_document()
    second = render_openapi_document()

    assert first == second
    assert first.endswith("\n")
    assert json.loads(first)["openapi"]
    assert "not-a-session-secret" not in first
    assert "openapi-contract-data" not in first
    assert "openapi-contract-project" not in first


def test_check_mode_rejects_stale_artifact_without_overwriting(tmp_path: Path) -> None:
    artifact = tmp_path / "openapi.json"
    artifact.write_text('{"stale": true}\n', encoding="utf-8")

    assert write_or_check(artifact, check=True) is False
    assert artifact.read_text(encoding="utf-8") == '{"stale": true}\n'


def test_canonical_openapi_artifact_is_current() -> None:
    assert CONTRACT_PATH.read_text(encoding="utf-8") == render_openapi_document()


def test_openapi_locks_errors_security_sse_and_preview_semantics() -> None:
    """A generated React client must see the same wire contracts as runtime clients."""
    schema = create_app(_test_settings()).openapi()
    components = schema["components"]

    assert "ErrorEnvelope" in components["schemas"]
    assert "EventEnvelope" in components["schemas"]
    assert "PreviewResponse" in components["schemas"]
    assert components["securitySchemes"]["SessionCookie"] == {
        "type": "apiKey",
        "in": "cookie",
        "name": "manyselves_session",
    }
    assert "DeploymentBearer" not in components["securitySchemes"]
    assert components["securitySchemes"]["ControlLeaseToken"] == {
        "type": "apiKey",
        "in": "header",
        "name": "X-Control-Lease-Token",
    }

    mutation = schema["paths"]["/api/v1/settings/providers/{provider_id}"]["patch"]
    assert mutation["security"] == [
        {"SessionCookie": [], "ControlLeaseToken": []}
    ]
    assert schema["paths"]["/api/v1/projects"]["get"]["security"] == [
        {"SessionCookie": []}
    ]
    assert "Authorization" not in json.dumps(schema)
    assert mutation["responses"]["422"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }
    readiness = schema["paths"]["/api/v1/health/ready"]["get"]
    assert readiness["responses"]["503"]["content"]["application/json"]["schema"] == {
        "$ref": "#/components/schemas/ErrorEnvelope"
    }

    events = schema["paths"]["/api/v1/events"]["get"]
    stream = events["responses"]["200"]["content"]["text/event-stream"]
    assert stream["schema"]["type"] == "string"
    assert stream["x-event-envelope"] == {
        "$ref": "#/components/schemas/EventEnvelope"
    }
    preview = schema["paths"][
        "/api/v1/projects/{project_id}/files/preview"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert preview["$ref"] == "#/components/schemas/PreviewResponse"
    global_preview = schema["paths"][
        "/api/v1/global-knowledge/files/preview"
    ]["get"]["responses"]["200"]["content"]["application/json"]["schema"]
    assert global_preview["$ref"] == "#/components/schemas/GlobalKnowledgePreviewResponse"
    preview_schema = components["schemas"]["PreviewResponse"]
    assert preview_schema["discriminator"]["propertyName"] == "kind"
    assert len(preview_schema["oneOf"]) == 7


def test_openapi_exposes_correlated_and_sanitized_runtime_response_shapes() -> None:
    """Generated clients need stable names for recoverable tool and debug fields."""
    schemas = create_app(_test_settings()).openapi()["components"]["schemas"]

    tool = schemas["RuntimeToolResponse"]
    debug = schemas["RuntimeDebugResponse"]

    assert {"toolCallId", "agentId", "arguments", "result", "error"} <= set(
        tool["properties"]
    )
    assert {"agentId", "error"} <= set(debug["properties"])


def test_framework_validation_runtime_uses_the_documented_error_envelope() -> None:
    schema = create_app(_test_settings()).openapi()
    for path_item in schema["paths"].values():
        for operation in path_item.values():
            if not isinstance(operation, dict) or "responses" not in operation:
                continue
            validation = operation["responses"].get("422")
            if validation is None:
                continue
            assert validation["content"]["application/json"]["schema"] == {
                "$ref": "#/components/schemas/ErrorEnvelope"
            }
