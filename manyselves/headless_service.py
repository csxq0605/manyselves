"""Command-line entrypoint for separate headless API and worker processes."""

from __future__ import annotations

import argparse
import asyncio
import os
import signal

from .core.providers.base import LLMProvider
from .core.providers.factory import ProviderFactory
from .core.reporting.headless_runtime import HeadlessReportingRuntime, RuntimePaths
from .core.reporting.production_runtime import EnvironmentSecretProvider
from .core.reporting.web_runtime import (
    LocalAuthorizationService,
    ReportingApi,
    ReportingASGIApp,
)


class _ApiOnlyProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("unavailable", model="api-process-does-not-call-provider")

    async def chat(self, *args, **kwargs):
        raise RuntimeError("Provider calls belong to the worker process")


def _worker_provider() -> LLMProvider:
    provider_type = os.environ.get("MANYSELVES_PROVIDER_TYPE", "openai")
    secret_name = os.environ.get(
        "MANYSELVES_PROVIDER_API_KEY_SECRET", "MANYSELVES_PROVIDER_API_KEY"
    )
    secret = EnvironmentSecretProvider({secret_name}).get(secret_name)
    return ProviderFactory.create_provider(
        provider_type,
        secret,
        api_base=os.environ.get("MANYSELVES_PROVIDER_API_BASE") or None,
        model=os.environ.get("MANYSELVES_PROVIDER_MODEL") or None,
    )


def create_api_app() -> ReportingASGIApp:
    paths = RuntimePaths.from_environment()
    runtime = HeadlessReportingRuntime(paths, llm_provider=_ApiOnlyProvider())
    authorization = LocalAuthorizationService(paths.service_state_root)
    return ReportingASGIApp(ReportingApi(runtime, authorization))


async def _serve_worker() -> None:
    paths = RuntimePaths.from_environment()
    runtime = HeadlessReportingRuntime(paths, llm_provider=_worker_provider())
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
        except NotImplementedError:
            pass
    await runtime.serve_worker(
        stop_event,
        worker_id=os.environ.get("MANYSELVES_WORKER_ID", "worker-1"),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Manyselves headless service")
    parser.add_argument("mode", choices=("api", "worker"))
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", default=8080, type=int)
    args = parser.parse_args()
    if args.mode == "worker":
        asyncio.run(_serve_worker())
        return
    try:
        import uvicorn
    except ImportError as exc:
        raise SystemExit(
            "API mode requires the server extra: pip install 'manyselves[server]'"
        ) from exc
    uvicorn.run(create_api_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
