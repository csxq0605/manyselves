"""Non-blocking Main/report lifecycle tests."""

import asyncio
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.service import ReportingRunResult
from manyselves.core.tools.reporting_tool import ReportingRunController
from manyselves.core.tools.task_board import TaskBoard
from manyselves.interfaces.types import ReportMessage, StatusChange, TaskStatus, UserMessage


class _ControlledService:
    def __init__(self, workspace: Path):
        self.workspace = workspace
        self.release = asyncio.Event()

    def prepare_run(self, request: ReportRequest) -> str:
        return "report-background"

    async def run_prepared(
        self, request: ReportRequest, run_id: str
    ) -> ReportingRunResult:
        await self.release.wait()
        return ReportingRunResult(run_id=run_id, status="completed")


class _EmittingService(_ControlledService):
    def __init__(self, workspace: Path, bus: MessageBus):
        super().__init__(workspace)
        self.bus = bus

    async def run_prepared(
        self, request: ReportRequest, run_id: str
    ) -> ReportingRunResult:
        await self.bus.publish(
            UserMessage(
                agent_type="module-2.1-specialist--session-new",
                source="report-workflow",
                content="开始模块 2.1",
            )
        )
        return ReportingRunResult(run_id=run_id, status="completed")


@pytest.mark.asyncio
async def test_background_report_returns_immediately_and_reports_completion(tmp_path: Path) -> None:
    bus = MessageBus()
    board = TaskBoard()
    service = _ControlledService(tmp_path)
    controller = ReportingRunController(service, bus, board)  # type: ignore[arg-type]
    reports: list[ReportMessage] = []

    async def record(message):
        reports.append(message)

    bus.subscribe(ReportMessage, record)
    processor = asyncio.create_task(bus.process_queue())
    started = controller.start(ReportRequest(instruction="生成报告"))

    assert started["status"] == "running"
    assert controller.status(started["run_id"])["status"] == TaskStatus.IN_PROGRESS.value
    service.release.set()
    await asyncio.wait_for(controller._tasks[started["run_id"]], timeout=1)
    await asyncio.sleep(0)

    assert controller.status(started["run_id"])["status"] == TaskStatus.COMPLETED.value
    assert reports[-1].agent_type == "report-workflow"
    assert '"status": "completed"' in reports[-1].content
    bus.shutdown()
    processor.cancel()
    await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_new_run_status_is_published_before_child_agent_events(tmp_path: Path) -> None:
    bus = MessageBus()
    board = TaskBoard()
    service = _EmittingService(tmp_path, bus)
    controller = ReportingRunController(service, bus, board)  # type: ignore[arg-type]
    observed: list[str] = []

    async def record_status(message: StatusChange) -> None:
        if message.status == "thinking":
            observed.append("new-run")

    async def record_child(_message: UserMessage) -> None:
        observed.append("child")

    bus.subscribe(StatusChange, record_status)
    bus.subscribe(UserMessage, record_child)
    processor = asyncio.create_task(bus.process_queue())
    started = controller.start(ReportRequest(instruction="生成报告"))
    await asyncio.wait_for(controller._tasks[started["run_id"]], timeout=1)
    await asyncio.sleep(0)

    assert observed[:2] == ["new-run", "child"]
    bus.shutdown()
    processor.cancel()
    await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_report_can_be_cancelled(tmp_path: Path) -> None:
    bus = MessageBus()
    board = TaskBoard()
    service = _ControlledService(tmp_path)
    controller = ReportingRunController(service, bus, board)  # type: ignore[arg-type]
    reports: list[ReportMessage] = []

    async def record(message):
        reports.append(message)

    bus.subscribe(ReportMessage, record)
    processor = asyncio.create_task(bus.process_queue())
    started = controller.start(ReportRequest(instruction="生成报告"))
    task = controller._tasks[started["run_id"]]
    assert controller.cancel(started["run_id"])
    with pytest.raises(asyncio.CancelledError):
        await task
    await asyncio.sleep(0)

    assert controller.status(started["run_id"])["status"] == TaskStatus.CANCELLED.value
    assert '"status": "cancelled"' in reports[-1].content
    bus.shutdown()
    processor.cancel()
    await asyncio.gather(processor, return_exceptions=True)


@pytest.mark.asyncio
async def test_background_report_periodically_checks_status_without_cancelling(
    tmp_path: Path,
) -> None:
    bus = MessageBus()
    board = TaskBoard()
    service = _ControlledService(tmp_path)
    controller = ReportingRunController(
        service,  # type: ignore[arg-type]
        bus,
        board,
        status_check_interval_seconds=0.01,
    )
    statuses: list[StatusChange] = []

    async def record(message: StatusChange) -> None:
        statuses.append(message)

    bus.subscribe(StatusChange, record)
    processor = asyncio.create_task(bus.process_queue())
    started = controller.start(ReportRequest(instruction="生成报告"))
    await asyncio.sleep(0.035)

    scheduled = [
        message
        for message in statuses
        if message.extra.get("scheduled_status_check") is True
    ]
    assert scheduled
    assert scheduled[-1].extra["task_status"] == TaskStatus.IN_PROGRESS.value
    assert not controller._tasks[started["run_id"]].done()

    service.release.set()
    await asyncio.wait_for(controller._tasks[started["run_id"]], timeout=1)
    bus.shutdown()
    processor.cancel()
    await asyncio.gather(processor, return_exceptions=True)
