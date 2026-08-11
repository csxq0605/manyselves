"""Qt-independent bootstrap for reporting API/coordinator/worker processes."""

from __future__ import annotations

import asyncio
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol

from ...config.schema import AgentDefaults
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..tools.task_board import TaskBoard
from .execution_runtime import ExecutionProfileCatalog, ProviderRouter
from .job_runtime import LocalReportingJobStore, ReportingJob, ReportingJobWorker
from .models import ReportRequest, RevisionRequest
from .service import ReportingRunResult, ReportingService


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Deployment-owned roots; no path is derived from the source checkout."""

    project_storage_root: Path
    service_state_root: Path
    config_root: Path

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "project_storage_root", Path(self.project_storage_root).resolve()
        )
        object.__setattr__(
            self, "service_state_root", Path(self.service_state_root).resolve()
        )
        object.__setattr__(self, "config_root", Path(self.config_root).resolve())

    @classmethod
    def from_environment(
        cls, env: Mapping[str, str] | None = None
    ) -> "RuntimePaths":
        values = env if env is not None else os.environ
        required = {
            "project_storage_root": values.get("MANYSELVES_PROJECT_STORAGE_ROOT"),
            "service_state_root": values.get("MANYSELVES_SERVICE_STATE_ROOT"),
            "config_root": values.get("MANYSELVES_CONFIG_ROOT"),
        }
        missing = [name for name, value in required.items() if not value]
        if missing:
            raise ValueError(
                "headless runtime paths are not configured: " + ", ".join(missing)
            )
        return cls(**required)  # type: ignore[arg-type]


class ConfigProvider(Protocol):
    def agent_defaults(self) -> AgentDefaults: ...


class StaticConfigProvider:
    def __init__(self, defaults: AgentDefaults | None = None) -> None:
        self._defaults = defaults or AgentDefaults()

    def agent_defaults(self) -> AgentDefaults:
        return self._defaults.model_copy(deep=True)


class HeadlessReportingRuntime:
    """Submit immediately, then execute jobs in an independent worker lifecycle."""

    def __init__(
        self,
        paths: RuntimePaths,
        *,
        llm_provider: LLMProvider,
        config_provider: ConfigProvider | None = None,
        bus: MessageBus | None = None,
        task_board: TaskBoard | None = None,
        provider_router: ProviderRouter | None = None,
    ) -> None:
        self.paths = paths
        self.paths.project_storage_root.mkdir(parents=True, exist_ok=True)
        self.paths.service_state_root.mkdir(parents=True, exist_ok=True)
        self.paths.config_root.mkdir(parents=True, exist_ok=True)
        self.bus = bus or MessageBus()
        self.task_board = task_board or TaskBoard()
        self.config_provider = config_provider or StaticConfigProvider()
        if provider_router is None:
            catalog = ExecutionProfileCatalog.load(
                self.paths.config_root / "execution-profiles.json"
            )
            provider_router = ProviderRouter(
                llm_provider,
                profiles=catalog.profiles,
            )
        self.service = ReportingService(
            self.paths.project_storage_root,
            bus=self.bus,
            task_board=self.task_board,
            llm_provider=llm_provider,
            agent_defaults=self.config_provider.agent_defaults(),
            provider_router=provider_router,
        )
        self.jobs = LocalReportingJobStore(
            self.paths.project_storage_root,
            state_root=self.paths.service_state_root,
        )

    def submit(self, request: ReportRequest) -> ReportingJob:
        run_id = self.service.prepare_run(request)
        return self.jobs.enqueue(run_id, request)

    def submit_revision(self, request: RevisionRequest) -> ReportingJob:
        run_id = self.service.prepare_revision_run(request)
        return self.jobs.enqueue_revision(run_id, request)

    def resume(self, run_id: str) -> ReportingJob:
        return self.jobs.requeue(run_id)

    async def work_once(self, worker_id: str = "headless-worker") -> ReportingRunResult | None:
        return await ReportingJobWorker(
            self.service, self.jobs, worker_id
        ).run_once()

    async def work_until_idle(
        self,
        worker_id: str = "headless-worker",
        *,
        max_jobs: int | None = None,
    ) -> list[ReportingRunResult]:
        completed: list[ReportingRunResult] = []
        while max_jobs is None or len(completed) < max_jobs:
            result = await self.work_once(worker_id)
            if result is None:
                break
            completed.append(result)
        return completed

    async def serve_worker(
        self,
        stop_event: asyncio.Event,
        worker_id: str = "headless-worker",
        *,
        idle_poll_seconds: float = 0.25,
    ) -> None:
        if idle_poll_seconds <= 0:
            raise ValueError("worker poll interval must be positive")
        while not stop_event.is_set():
            result = await self.work_once(worker_id)
            if result is None:
                try:
                    await asyncio.wait_for(
                        stop_event.wait(), timeout=idle_poll_seconds
                    )
                except asyncio.TimeoutError:
                    pass

    def projection(self, run_id: str):
        return self.jobs.projection(run_id)
