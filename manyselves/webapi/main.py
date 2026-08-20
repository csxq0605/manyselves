"""FastAPI application factory and Gunicorn entry point."""

import asyncio
import re
from collections.abc import Callable
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.openapi.utils import get_openapi
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.base import RequestResponseEndpoint
from starlette.responses import FileResponse, Response

from .errors import (
    ApiError,
    api_error_handler,
    http_error_handler,
    internal_error_handler,
    request_validation_error_handler,
)
from .lifespan import application_lifespan
from .routes.agents import router as agents_router
from .routes.auth import router as auth_router
from .routes.bootstrap import router as bootstrap_router
from .routes.control import router as control_router
from .routes.conversations import router as conversations_router
from .routes.events import router as events_router
from .routes.files import router as files_router
from .routes.global_knowledge import router as global_knowledge_router
from .routes.health import router as health_router
from .routes.maintenance import router as maintenance_router
from .routes.operations import router as operations_router
from .routes.projects import router as projects_router
from .routes.reporting import router as reporting_router
from .routes.settings import router as settings_router
from .routes.workflows import router as workflows_router
from .schemas.common import ErrorEnvelope
from .security import require_authenticated_session
from .settings import WebSettings

API_PREFIX = "/api/v1"


def generate_operation_id(route: APIRoute) -> str:
    """Generate a client-stable ID from the public HTTP method and path only."""
    methods = sorted(method.lower() for method in route.methods or ())
    if len(methods) != 1:
        raise ValueError(
            "OpenAPI routes must declare exactly one HTTP method for a stable "
            "operation ID"
        )
    path_slug = re.sub(r"[^a-zA-Z0-9]+", "_", route.path_format).strip("_").lower()
    return f"{methods[0]}_{path_slug or 'root'}"


class DeferredCORSMiddleware:
    """Apply CORS after lifespan has validated deferred environment settings."""

    def __init__(
        self,
        app: Callable[..., object],
        *,
        settings_getter: Callable[[], WebSettings | None],
    ) -> None:
        self.app = app
        self._settings_getter = settings_getter
        self._cors: CORSMiddleware | None = None
        self._origins: tuple[str, ...] | None = None

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        settings = self._settings_getter()
        origins = () if settings is None else tuple(settings.allowed_origins)
        if self._cors is None or origins != self._origins:
            self._cors = CORSMiddleware(
                self.app,
                allow_origins=list(origins),
                allow_credentials=True,
                allow_methods=["*"],
                allow_headers=["*"],
            )
            self._origins = origins
        await self._cors(scope, receive, send)


def create_app(settings: WebSettings | None = None) -> FastAPI:
    """Construct an import-safe HTTP app without creating a runtime."""
    app = FastAPI(
        lifespan=application_lifespan,
        generate_unique_id_function=generate_operation_id,
        openapi_url=None,
        docs_url=None,
        redoc_url=None,
        responses={
            422: {"model": ErrorEnvelope, "description": "Request validation failed"},
            "default": {"model": ErrorEnvelope, "description": "API error"},
        },
    )
    app.state.web_settings = settings
    app.state.runtime_host = None
    app.state.runtime_facade = None
    app.state.event_broker = None
    app.state.project_registry = None
    app.state.conversation_service = None
    app.state.reporting_facade = None
    app.state.python_run_service = None
    app.state.maintenance_service = None
    app.state.global_knowledge_service = None
    app.state.session_signer = None
    app.state.account_catalog = None
    app.state.tenant_runtime_manager = None
    app.state.tenant_runtime_factory = None
    app.state.lifecycle_lock = asyncio.Lock()
    app.state.lifecycle_active = False
    app.state._lifecycle_cleanup_pending = None
    app.add_exception_handler(ApiError, api_error_handler)
    app.add_exception_handler(RequestValidationError, request_validation_error_handler)
    app.add_exception_handler(StarletteHTTPException, http_error_handler)
    app.add_exception_handler(Exception, internal_error_handler)

    app.add_middleware(
        DeferredCORSMiddleware,
        settings_getter=lambda: app.state.web_settings,
    )

    @app.middleware("http")
    async def attach_request_id(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        """Attach a stable request identifier for the current HTTP response."""
        request_id = request.headers.get("X-Request-ID", uuid4().hex)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(health_router, prefix=API_PREFIX)
    app.include_router(auth_router, prefix=API_PREFIX)
    app.include_router(
        bootstrap_router,
        prefix=API_PREFIX,
        dependencies=[Depends(require_authenticated_session)],
    )
    app.include_router(control_router, prefix=API_PREFIX)
    app.include_router(projects_router, prefix=API_PREFIX)
    app.include_router(files_router, prefix=API_PREFIX)
    app.include_router(global_knowledge_router, prefix=API_PREFIX)
    app.include_router(conversations_router, prefix=API_PREFIX)
    app.include_router(events_router, prefix=API_PREFIX)
    app.include_router(agents_router, prefix=API_PREFIX)
    app.include_router(reporting_router, prefix=API_PREFIX)
    app.include_router(settings_router, prefix=API_PREFIX)
    app.include_router(operations_router, prefix=API_PREFIX)
    app.include_router(maintenance_router, prefix=API_PREFIX)
    app.include_router(workflows_router, prefix=API_PREFIX)

    # 挂载 React 前端静态文件
    frontend_dist = Path(__file__).parent.parent.parent / "frontend" / "dist"
    if frontend_dist.exists():
        # 挂载静态资源（CSS、JS、图片等）
        app.mount("/assets", StaticFiles(directory=frontend_dist / "assets"), name="assets")

        # SPA fallback: 所有未匹配的路由返回 index.html
        @app.get("/", include_in_schema=False)
        @app.get("/{full_path:path}", include_in_schema=False)
        async def serve_spa(full_path: str = ""):
            """Serve React SPA for all non-API routes."""
            return FileResponse(frontend_dist / "index.html")

    def semantic_openapi() -> dict:
        """Expose the actual error, auth, preview, and streaming wire semantics."""
        if app.openapi_schema is not None:
            return app.openapi_schema
        schema = get_openapi(
            title=app.title,
            version=app.version,
            routes=app.routes,
        )
        schema.setdefault("components", {}).setdefault("securitySchemes", {})[
            "SessionCookie"
        ] = {
            "type": "apiKey",
            "in": "cookie",
            "name": "manyselves_session",
        }
        anonymous_prefixes = (f"{API_PREFIX}/auth/", f"{API_PREFIX}/health/")
        for path, path_item in schema.get("paths", {}).items():
            for operation in path_item.values():
                if not isinstance(operation, dict) or "responses" not in operation:
                    continue
                if "422" in operation["responses"]:
                    operation["responses"]["422"] = {
                        "description": "Request validation failed",
                        "content": {
                            "application/json": {
                                "schema": {
                                    "$ref": "#/components/schemas/ErrorEnvelope"
                                }
                            }
                        },
                    }
                for parameter in operation.get("parameters", []):
                    if parameter.get("name") == "X-Control-Lease-Token":
                        parameter["required"] = True
                security = operation.get("security", [])
                schemes = {
                    name
                    for requirement in security
                    for name in requirement
                }
                if path.startswith(anonymous_prefixes):
                    continue
                if "ControlLeaseToken" in schemes:
                    operation["security"] = [
                        {
                            "SessionCookie": [],
                            "ControlLeaseToken": [],
                        }
                    ]
                else:
                    operation["security"] = [{"SessionCookie": []}]
        event_response = schema["paths"][f"{API_PREFIX}/events"]["get"]["responses"]["200"]
        event_response["content"] = {
            "text/event-stream": {
                "schema": {"type": "string"},
                "x-event-envelope": {
                    "$ref": "#/components/schemas/EventEnvelope"
                },
            }
        }
        app.openapi_schema = schema
        return schema

    app.openapi = semantic_openapi
    return app


# For tests and direct imports - create a default app instance
app = create_app()
