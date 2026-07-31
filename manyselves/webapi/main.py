"""FastAPI application factory and Gunicorn entry point."""

from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.responses import Response

from .lifespan import application_lifespan
from .routes.bootstrap import router as bootstrap_router
from .settings import WebSettings

API_PREFIX = "/api/v1"


def create_app(settings: WebSettings | None = None) -> FastAPI:
    """Construct an import-safe HTTP app without creating a runtime."""
    app = FastAPI(lifespan=application_lifespan)
    app.state.web_settings = settings
    app.state.runtime_host = None
    app.state.runtime_facade = None
    app.state.event_broker = None

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[] if settings is None else settings.allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.middleware("http")
    async def attach_request_id(request: Request, call_next: object) -> Response:
        """Attach a stable request identifier for the current HTTP response."""
        request_id = request.headers.get("X-Request-ID", uuid4().hex)
        request.state.request_id = request_id
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        return response

    app.include_router(bootstrap_router, prefix=API_PREFIX)
    return app


app = create_app()
