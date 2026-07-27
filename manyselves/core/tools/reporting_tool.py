"""Main-Agent tools for non-blocking project-local report runs."""

import asyncio
import json

from pathlib import Path
from typing import Any, Awaitable, Callable, Literal
from uuid import uuid4

from ...config.schema import AgentDefaults
from ...interfaces.types import AgentStatus, ReportMessage, StatusChange, TaskStatus
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..reporting.models import (
    EvidenceDecisionAction,
    ReportOperation,
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)
from ..reporting.service import ReportingService
from .outcomes import ToolOutcome, normalize_tool_outcome
from .registry import Tool
from .task_board import TaskBoard


def reporting_result_outcome(payload: dict[str, Any]) -> ToolOutcome:
    """Normalize a reporting entry-point result for loop and GUI consumers."""

    return normalize_tool_outcome(payload, "run_reporting_workflow")


class ReportingRunController:
    """Own background report tasks and return their terminal result to Main."""

    def __init__(
        self,
        service: ReportingService,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        status_check_interval_seconds: float = 60.0,
    ):
        self.service = service
        self.bus = bus
        self.task_board = task_board
        self.status_check_interval_seconds = status_check_interval_seconds
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._status_watchers: dict[str, asyncio.Task[None]] = {}
        self._task_ids: dict[str, str] = {}
        self._cancel_notified: set[str] = set()

    def start(self, request: ReportRequest) -> dict[str, Any]:
        run_id = self.service.prepare_run(request)
        return self.start_operation(
            run_id,
            request.instruction,
            lambda: self.service.run_prepared(request, run_id),
        )

    def start_operation(
        self,
        run_id: str,
        brief: str,
        operation: Callable[[], Awaitable[Any]],
    ) -> dict[str, Any]:
        if run_id in self._tasks and not self._tasks[run_id].done():
            raise ValueError(f"report run is already active: {run_id}")
        self._cancel_notified.discard(run_id)
        item = self.task_board.create_task(
            source="main",
            target="report-workflow",
            brief=f"执行报告任务 {run_id}: {brief[:120]}",
            blocking=False,
        )
        self.task_board.start_task(item.task_id, target_agent="report-workflow")
        self._task_ids[run_id] = item.task_id
        task = asyncio.create_task(
            self._start_and_execute_operation(
                operation,
                run_id,
                item.task_id,
                brief,
            ),
            name=f"manyselves-report-{run_id}",
        )
        self._tasks[run_id] = task
        watcher = asyncio.create_task(
            self._watch_status(run_id, item.task_id, task),
            name=f"manyselves-report-status-{run_id}",
        )
        self._status_watchers[run_id] = watcher
        task.add_done_callback(lambda _done, rid=run_id: self._finish_tracking(rid))
        return {"status": "running", "run_id": run_id, "task_id": item.task_id}

    def _finish_tracking(self, run_id: str) -> None:
        self._tasks.pop(run_id, None)
        watcher = self._status_watchers.pop(run_id, None)
        if watcher is not None and not watcher.done():
            watcher.cancel()

    async def _watch_status(
        self,
        run_id: str,
        task_id: str,
        operation_task: asyncio.Task[None],
    ) -> None:
        """Check a running workflow periodically without waking the Main LLM."""

        try:
            while not operation_task.done():
                await asyncio.sleep(self.status_check_interval_seconds)
                if operation_task.done():
                    return
                snapshot = self.status(run_id)
                await self.bus.publish(
                    StatusChange(
                        agent_type="report-workflow",
                        status=AgentStatus.THINKING,
                        extra={
                            "run_id": run_id,
                            "task_id": task_id,
                            "scheduled_status_check": True,
                            "task_status": snapshot.get("status", "unknown"),
                        },
                    )
                )
        except asyncio.CancelledError:
            return

    async def _start_and_execute_operation(
        self,
        operation: Callable[[], Awaitable[Any]],
        run_id: str,
        task_id: str,
        brief: str,
    ) -> None:
        """Publish the new-run boundary before any child Agent can emit UI events."""
        await self.bus.publish(
            StatusChange(
                agent_type="report-workflow",
                status=AgentStatus.THINKING,
                extra={"run_id": run_id, "task": brief[:120]},
            )
        )
        await self._execute_operation(operation, run_id, task_id)

    async def _execute_operation(
        self,
        operation: Callable[[], Awaitable[Any]],
        run_id: str,
        task_id: str,
    ) -> None:
        try:
            result = await operation()
            payload = result.model_dump(mode="json")
            payload["output_paths"] = [
                Path(path).relative_to(self.service.workspace).as_posix()
                for path in result.output_paths
            ]
            task = self.task_board.get_task(task_id, target_agent="report-workflow")
            if task is not None and task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
                if result.status in {
                    "needs_decision",
                    "needs_user_decision",
                    "needs_scope_expansion",
                    "blocked",
                }:
                    self.task_board.block_task(task_id, target_agent="report-workflow")
                elif result.status == "completed":
                    self.task_board.complete_task(task_id, target_agent="report-workflow")
                else:
                    self.task_board.fail_task(task_id, target_agent="report-workflow")
            await self._publish_result(task_id, payload)
        except asyncio.CancelledError:
            task = self.task_board.get_task(task_id, target_agent="report-workflow")
            if task is not None and task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
                self.task_board.cancel_task(task_id, target_agent="report-workflow")
            if run_id not in self._cancel_notified:
                await self._publish_result(
                    task_id,
                    {"status": "cancelled", "run_id": run_id, "error": "用户取消了报告任务"},
                )
        except Exception as exc:
            task = self.task_board.get_task(task_id, target_agent="report-workflow")
            if task is not None and task.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
                self.task_board.fail_task(task_id, target_agent="report-workflow")
            await self._publish_result(
                task_id,
                {"status": "failed", "run_id": run_id, "error": str(exc)},
            )
        finally:
            self._tasks.pop(run_id, None)

    def resume_decision(
        self,
        decision_id: str,
        action: EvidenceDecisionAction,
        supplements: list[UserSupplement] | None,
    ) -> dict[str, Any]:
        run_id = self.service.decisions.load(decision_id).run_id
        return self.start_operation(
            run_id,
            f"恢复证据决策 {decision_id}",
            lambda: self.service.resume(decision_id, action, supplements),
        )

    def resume_run(
        self,
        run_id: str,
        *,
        max_provider_attempts: int | None = None,
        max_total_tokens: int | None = None,
        supplements: list[UserSupplement] | None = None,
    ) -> dict[str, Any]:
        return self.start_operation(
            run_id,
            f"从检查点恢复 {run_id}",
            lambda: self.service.resume_run(
                run_id,
                max_provider_attempts=max_provider_attempts,
                max_total_tokens=max_total_tokens,
                supplements=supplements,
            ),
        )

    def revise(self, request: RevisionRequest) -> dict[str, Any]:
        run_id = f"report-revision-{uuid4().hex[:10]}"
        return self.start_operation(
            run_id,
            request.feedback,
            lambda: self.service.revise(request, run_id=run_id),
        )

    async def _publish_result(self, task_id: str, payload: dict[str, Any]) -> None:
        status = str(payload.get("status", "failed"))
        run_id = str(payload.get("run_id", ""))
        outputs = payload.get("output_paths") or []
        summary = f"报告任务 {run_id} 状态：{status}"
        if outputs:
            summary += f"；输出：{', '.join(map(str, outputs))}"
        await self.bus.publish(
            ReportMessage(
                agent_type="report-workflow",
                task_id=task_id,
                report_type="reply" if status == "completed" else "quality",
                summary=summary,
                content=json.dumps(payload, ensure_ascii=False),
            )
        )
        await self.bus.publish(
            StatusChange(
                agent_type="report-workflow",
                status=(AgentStatus.IDLE if status == "completed" else AgentStatus.ERROR),
                extra={"run_id": run_id, "terminal_status": status},
            )
        )

    def cancel(self, run_id: str) -> bool:
        task = self._tasks.get(run_id)
        if task is None or task.done():
            return False
        task_id = self._task_ids[run_id]
        item = self.task_board.get_task(task_id, target_agent="report-workflow")
        if item is not None and item.status in {TaskStatus.PENDING, TaskStatus.IN_PROGRESS}:
            self.task_board.cancel_task(task_id, target_agent="report-workflow")
        self._cancel_notified.add(run_id)
        asyncio.create_task(
            self._publish_result(
                task_id,
                {"status": "cancelled", "run_id": run_id, "error": "用户取消了报告任务"},
            )
        )
        task.cancel()
        return True

    def cancel_all(self) -> list[str]:
        cancelled = []
        for run_id in list(self._tasks):
            if self.cancel(run_id):
                cancelled.append(run_id)
        return cancelled

    def status(self, run_id: str) -> dict[str, Any]:
        task_id = self._task_ids.get(run_id)
        if task_id is None:
            return {"status": "not_found", "run_id": run_id}
        item = self.task_board.get_task(task_id, target_agent="report-workflow")
        return {
            "status": item.status.value if item is not None else "unknown",
            "run_id": run_id,
            "task_id": task_id,
        }


class RunReportingWorkflowTool(Tool):
    name = "run_reporting_workflow"
    description = (
        "Run one explicitly selected power-distribution report operation. Use "
        "distill_template_skill to refresh the fixed template-writing Skill without writing "
        "a report; use full_report "
        "for a new five-module report; module_report for only selected modules; "
        "aggregate_existing for five existing module Markdown files; or render_existing "
        "for one existing approved aggregate Markdown file. "
        "Do not use it to revise an already delivered baseline report."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
        controller: ReportingRunController | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        service = ReportingService(
            self.workspace, bus=bus, task_board=task_board,
            llm_provider=llm_provider, agent_defaults=agent_defaults,
        )
        self.controller = controller or ReportingRunController(service, bus, task_board)

    async def __call__(
        self,
        instruction: str,
        operation: ReportOperation,
        target_modules: list[str] | None = None,
        source_module_refs: dict[str, str] | None = None,
        source_markdown_ref: str | None = None,
        output_filename: str | None = None,
        execution_requirements: list[str] | None = None,
        missing_evidence_policy: Literal["ask", "block", "skip", "draft"] = "ask",
        max_provider_attempts: int = 80,
        max_total_tokens: int = 800000,
    ) -> dict[str, Any]:
        """Run the report workflow.

        Args:
            instruction: The user's report request without invented requirements.
            operation: Explicit route selected by Main; never inferred by this tool.
            target_modules: Fixed report modules to run, from 2.1 through 2.5. Must be
                empty for distill_template_skill.
            source_module_refs: Optional module-id to project-relative Markdown mapping for
                aggregate_existing. Defaults to Outputs/Modules/<module>.md.
            source_markdown_ref: Project-relative Markdown path for render_existing.
            output_filename: Optional DOCX filename beneath Outputs/Reports.
            execution_requirements: Turn-specific requirements such as deep reasoning.
            missing_evidence_policy: How to handle submodules without customer evidence.
            max_provider_attempts: Deprecated telemetry reference; never stops the run.
            max_total_tokens: Deprecated telemetry reference; never stops the run.
        """

        request = ReportRequest(
            operation=operation,
            instruction=instruction,
            target_modules=(
                list(target_modules or [])
                if operation == "distill_template_skill"
                else target_modules or ["2.1", "2.2", "2.3", "2.4", "2.5"]
            ),
            source_module_refs=(
                {module_id: Path(path) for module_id, path in source_module_refs.items()}
                if source_module_refs is not None
                else None
            ),
            source_markdown_ref=(
                Path(source_markdown_ref) if source_markdown_ref is not None else None
            ),
            output_filename=output_filename,
            execution_requirements=execution_requirements or [],
            missing_evidence_policy=missing_evidence_policy,
            max_provider_attempts=max_provider_attempts,
            max_total_tokens=max_total_tokens,
        )
        return self.controller.start(request)


class CancelReportingWorkflowTool(Tool):
    name = "cancel_reporting_workflow"
    description = "Cancel a running report by run_id without blocking the Main Agent."

    def __init__(self, controller: ReportingRunController):
        self.controller = controller

    async def __call__(self, run_id: str) -> dict[str, Any]:
        """Cancel a running report.

        Args:
            run_id: The report run identifier returned when the run started.
        """
        cancelled = self.controller.cancel(run_id)
        return {
            "status": "cancellation_requested" if cancelled else "not_running",
            "run_id": run_id,
        }


class GetReportingWorkflowStatusTool(Tool):
    name = "get_reporting_workflow_status"
    description = "Read the current lifecycle state of a background report run."

    def __init__(self, controller: ReportingRunController):
        self.controller = controller

    async def __call__(self, run_id: str) -> dict[str, Any]:
        """Read a report run status.

        Args:
            run_id: The report run identifier returned when the run started.
        """
        return self.controller.status(run_id)


class ResumeReportingWorkflowTool(Tool):
    name = "resume_reporting_workflow"
    description = (
        "Resume the same report run after a missing-evidence decision, user-decision block, "
        "or checkpointed failure. Use decision_id plus action only for a real evidence "
        "decision; otherwise use run_id and pass newly confirmed facts as typed supplements."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
        controller: ReportingRunController | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
            llm_provider=llm_provider,
            agent_defaults=agent_defaults,
        )
        self.controller = controller or ReportingRunController(service, bus, task_board)

    async def __call__(
        self,
        decision_id: str | None = None,
        action: EvidenceDecisionAction | None = None,
        supplements: list[UserSupplement] | None = None,
        run_id: str | None = None,
        max_provider_attempts: int | None = None,
        max_total_tokens: int | None = None,
    ) -> dict[str, Any]:
        """Resume a pending evidence decision or a checkpointed run.

        Args:
            decision_id: Pending evidence decision identifier.
            action: Evidence action used with decision_id.
            supplements: Scoped current-run facts or instructions with explicit stages and supersession.
            run_id: Blocked, decision-stopped, failed, or cancelled report run identifier.
            max_provider_attempts: Deprecated compatibility field; no hard limit is enforced.
            max_total_tokens: Deprecated compatibility field; no hard limit is enforced.
        """

        if run_id is not None:
            if decision_id is not None or action is not None:
                raise ValueError("run resume and evidence-decision resume cannot be mixed")
            return self.controller.resume_run(
                run_id,
                max_provider_attempts=max_provider_attempts,
                max_total_tokens=max_total_tokens,
                supplements=supplements,
            )
        else:
            if decision_id is None or action is None:
                raise ValueError("evidence resume requires decision_id and action")
            return self.controller.resume_decision(decision_id, action, supplements)


class ReviseReportingWorkflowTool(Tool):
    name = "revise_reporting_workflow"
    description = (
        "Revise a delivered report from an immutable baseline version. "
        "Use for report feedback; Skill promotion remains false unless explicitly requested."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        task_board: TaskBoard,
        *,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
        controller: ReportingRunController | None = None,
    ):
        self.workspace = Path(workspace).resolve()
        service = ReportingService(
            self.workspace,
            bus=bus,
            task_board=task_board,
            llm_provider=llm_provider,
            agent_defaults=agent_defaults,
        )
        self.controller = controller or ReportingRunController(service, bus, task_board)

    async def __call__(
        self,
        baseline_version_id: str,
        feedback: str,
        target_module_ids: list[str],
        target_submodule_ids: list[str] | None = None,
        target_claim_ids: list[str] | None = None,
        promote_to_skill: bool = False,
        promote_skill_id: str | None = None,
        max_provider_attempts: int = 40,
        max_total_tokens: int = 400000,
    ) -> dict[str, Any]:
        """Run a version-aware local report revision.

        Args:
            baseline_version_id: Delivered report version to restore.
            feedback: User-authorized revision instruction.
            target_module_ids: Modules allowed to change.
            target_submodule_ids: Optional narrower submodule scope.
            target_claim_ids: Optional narrower claim scope.
            promote_to_skill: Explicitly request separate Skill feedback recording.
            promote_skill_id: Skill receiving explicit feedback when promotion is requested.
            max_provider_attempts: Deprecated telemetry reference; never stops the revision.
            max_total_tokens: Deprecated telemetry reference; never stops the revision.
        """

        return self.controller.revise(
            RevisionRequest(
                baseline_version_id=baseline_version_id,
                feedback=feedback,
                target_module_ids=target_module_ids,
                target_submodule_ids=target_submodule_ids or [],
                target_claim_ids=target_claim_ids or [],
                promote_to_skill=promote_to_skill,
                promote_skill_id=promote_skill_id,
                max_provider_attempts=max_provider_attempts,
                max_total_tokens=max_total_tokens,
            )
        )
