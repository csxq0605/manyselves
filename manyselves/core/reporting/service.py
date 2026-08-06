"""Complete V2 reporting service executed inside the Manyselves runtime."""

import asyncio
import hashlib
import io
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from docx import Document
from pydantic import BaseModel, ConfigDict, Field

from ...config.schema import AgentDefaults
from ...interfaces.types import AgentType, SystemNotice
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..tools.task_board import TaskBoard
from ..usage_ledger import UsageLedger
from .agent_runner import ReportingAgentRunner
from .config import load_packaged_agents
from .coverage import evaluate_coverage
from .decisions import EvidenceDecisionStore
from .evidence_readiness import ReportingBlockedError
from .file_lock import file_lock, release_lock
from .intake.adapters import IntakeAdapterRegistry
from .intake.manifest import build_manifest
from .intake.wps_images import canonicalize_photo_bindings, extract_wps_images
from .mappers import map_s2_1, map_s4_4, map_s4_6
from .models import (
    EvidenceDecisionAction,
    EvidenceDecisionRequest,
    EvidenceItem,
    OutputArtifact,
    ParsedArtifact,
    PhotoAsset,
    ProjectManifest,
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)
from .output_verifier import OutputVerificationError, verify_current_run_outputs
from .rendering import PackagedV2DocxCore, PdsDocxRenderer, RenderRequest, RenderResult
from .rendering.packaged_docx import verify_rendered_markdown
from .store import ReportingStore
from .workflow import AgentWorkflowBlocked, ReportingNeedsDecisionError, ReportWorkflowRunner


class ReportingRunResult(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    run_id: str
    status: str
    output_paths: list[Path] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    decision_id: str | None = None
    scope_expansion_request_id: str | None = None
    feedback_record_id: str | None = None
    error: str | None = None
    usage: dict[str, int] = Field(default_factory=dict)


class ReportingService:
    """Run the provider-backed multi-agent workflow against the current project."""

    PROJECT_TEMPLATE_PATH = Path("Templates/report_template.docx")
    EXPERT_SKILL_SOURCE_PATH = Path(
        "Templates/配电安全专家咨询报告(专家优化版).docx"
    )

    def __init__(
        self,
        workspace: Path,
        *,
        bus: MessageBus,
        task_board: TaskBoard,
        llm_provider: LLMProvider,
        agent_defaults: AgentDefaults | None = None,
        global_root: Path | None = None,
    ):
        if llm_provider is None:
            raise ValueError(
                "ReportingService requires an LLM provider; offline report generation is not supported"
            )
        self.workspace = Path(workspace).resolve()
        self.bus = bus
        self.task_board = task_board
        self.llm_provider = llm_provider
        self.agent_defaults = agent_defaults or AgentDefaults()
        self.global_root = (
            Path(global_root).resolve() if global_root is not None else None
        )
        self.store = ReportingStore(self.workspace)
        self.decisions = EvidenceDecisionStore(self.workspace)
        self.agents = load_packaged_agents()
        self._active_agent_runners: dict[str, ReportingAgentRunner] = {}
        template_root = Path(__file__).resolve().parents[2] / "templates" / "reporting"
        self.packaged_report_template_path = template_root / "report_template.docx"

    def _agent_runner_for(self, workflow_id: str) -> ReportingAgentRunner:
        """Return the one identity registry retained by a live workflow."""

        runner = self._active_agent_runners.get(workflow_id)
        if runner is None:
            runner = ReportingAgentRunner(
                self.workspace,
                self.bus,
                self.llm_provider,
                self.agent_defaults,
                timeout=None,
                global_root=self.global_root,
            )
            self._active_agent_runners[workflow_id] = runner
        return runner

    def _forget_agent_runner(self, workflow_id: str | None) -> None:
        if workflow_id is not None:
            self._active_agent_runners.pop(workflow_id, None)

    def resolve_report_template(self) -> tuple[Path, str]:
        """Resolve the current template with project scope taking precedence."""
        project_template = self.workspace / self.PROJECT_TEMPLATE_PATH
        if project_template.exists():
            if not project_template.is_file():
                raise ValueError(
                    f"project report template must be a DOCX file: {self.PROJECT_TEMPLATE_PATH}"
                )
            if not project_template.resolve().is_relative_to(self.workspace):
                raise ValueError("project report template must stay inside the project workspace")
            return project_template, "project"
        if not self.packaged_report_template_path.is_file():
            raise FileNotFoundError(
                f"packaged report template is missing: {self.packaged_report_template_path}"
            )
        return self.packaged_report_template_path, "packaged"

    def resolve_skill_distillation_template(self) -> tuple[Path, str]:
        """Select the expert source only for the isolated Skill distillation task."""

        expert_source = self.workspace / self.EXPERT_SKILL_SOURCE_PATH
        if expert_source.exists():
            if not expert_source.is_file():
                raise ValueError(
                    "expert Skill source must be a DOCX file: "
                    f"{self.EXPERT_SKILL_SOURCE_PATH}"
                )
            if not expert_source.resolve().is_relative_to(self.workspace):
                raise ValueError("expert Skill source must stay inside the project workspace")
            return expert_source, "expert-skill-source"
        return self.resolve_report_template()

    @property
    def report_template_path(self) -> Path:
        """Return the template that would be selected at this moment."""
        return self.resolve_report_template()[0]

    @property
    def report_template_source(self) -> str:
        """Return ``project`` or ``packaged`` for the currently selected template."""
        return self.resolve_report_template()[1]

    async def run(self, request: ReportRequest) -> ReportingRunResult:
        run_id = self.prepare_run(request)
        return await self.run_prepared(request, run_id)

    def prepare_run(self, request: ReportRequest) -> str:
        """Persist a new run request and return its stable id before execution starts."""
        self.store.ensure_layout()
        run_id = f"report-{uuid.uuid4().hex[:10]}"
        self.store.write_json(f"Work/runs/{run_id}/request.json", request.model_dump(mode="json"))
        return run_id

    async def run_prepared(self, request: ReportRequest, run_id: str) -> ReportingRunResult:
        """Execute a request previously persisted by :meth:`prepare_run`."""
        return await self._execute(request, run_id)

    async def _execute(
        self, request: ReportRequest, run_id: str, *, resume: bool = False
    ) -> ReportingRunResult:
        lock_handle = self._acquire_run_lock(run_id)
        try:
            return await self._execute_locked(request, run_id, resume=resume)
        finally:
            release_lock(lock_handle)
            lock_handle.close()

    def _acquire_run_lock(self, run_id: str):
        lock_path = self.workspace / f"Work/runs/{run_id}/.active.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+", encoding="utf-8")
        try:
            import sys
            if sys.platform != "win32":
                # Unix: Use file locking
                import fcntl
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    handle.close()
                    raise RuntimeError(f"report run is already active: {run_id}") from exc
            # Windows: No actual file locking (simplified)
        except Exception:
            handle.close()
            raise
        return handle

    async def _execute_locked(
        self, request: ReportRequest, run_id: str, *, resume: bool = False
    ) -> ReportingRunResult:
        state: dict = {"request": request, "run_id": run_id, "resume": resume}
        execution_started_ns = time.time_ns()
        workflow_id: str | None = None
        await self._notice(f"配电报告流程 {run_id} 已启动。")

        try:
            if request.operation == "render_existing":
                await self._notice("已识别为现有 Markdown 渲染任务，直接启动 Render。")
                self._render_existing(state)
            elif request.operation == "distill_template_skill":
                workflow_id = f"template-skill-distillation:{run_id}"
                runner = ReportWorkflowRunner(
                    self,
                    self._agent_runner_for(workflow_id),
                )
                await runner.distill_template_skill(state)
            elif request.operation == "aggregate_existing":
                workflow_id = f"aggregate-existing-report:{run_id}"
                runner = ReportWorkflowRunner(
                    self,
                    self._agent_runner_for(workflow_id),
                )
                await runner.aggregate_existing(state)
                await self._notice("汇总 Markdown 已生成，正在直接启动 Render。")
                self._render_markdown(
                    state,
                    state["aggregate_markdown_ref"],
                    request.output_filename,
                )
            else:
                workflow_id = f"full-power-distribution-report:{run_id}"
                runner = ReportWorkflowRunner(
                    self,
                    self._agent_runner_for(workflow_id),
                )
                await runner.run(state)
            self._forget_agent_runner(workflow_id)
        except asyncio.CancelledError:
            self._forget_agent_runner(workflow_id)
            result = ReportingRunResult(
                run_id=run_id,
                status="cancelled",
                error="interrupted by user",
            )
            self._save_run(result)
            await self._notice("用户已中断报告流程；当前进度已保存。")
            return result
        except ReportingBlockedError as exc:
            self._forget_agent_runner(workflow_id)
            if request.missing_evidence_policy == "ask":
                decision = self.decisions.create(
                    EvidenceDecisionRequest(
                        decision_id=f"evidence-{uuid.uuid4().hex[:12]}",
                        run_id=run_id,
                        missing_items=exc.missing_evidence,
                        affected_modules=exc.affected_modules,
                    )
                )
                result = ReportingRunResult(
                    run_id=run_id,
                    status="needs_user_decision",
                    missing_evidence=exc.missing_evidence,
                    decision_id=decision.decision_id,
                )
                self._save_run(result)
                await self._notice(
                    "配电报告流程等待选择：补充资料、保留不确定性起草、"
                    "保留目录并标记未评估，或停止。"
                )
                return result
            result = ReportingRunResult(
                run_id=run_id,
                status="blocked",
                missing_evidence=exc.missing_evidence,
            )
            self._save_run(result)
            await self._notice("配电报告流程等待补资或用户确认：" + ", ".join(exc.missing_evidence))
            return result
        except ReportingNeedsDecisionError as exc:
            if not exc.keep_agents_alive:
                self._forget_agent_runner(workflow_id)
            result = ReportingRunResult(
                run_id=run_id,
                status="needs_decision",
                error=str(exc),
            )
            self._save_run(result)
            if exc.keep_agents_alive:
                await self._notice(
                    f"流程已正常挂起，现有身份 Agent 保持等待；请在 Main 对话中补充：{exc}"
                )
            else:
                await self._notice(f"主决策 Agent 已结束未完成流程：{exc}")
            return result
        except AgentWorkflowBlocked as exc:
            self._forget_agent_runner(workflow_id)
            result = ReportingRunResult(
                run_id=run_id,
                status="blocked",
                error=exc.reason,
            )
            self._save_run(result)
            await self._notice(f"{exc.agent_id} 已明确报告阻塞：{exc.reason}")
            return result
        except Exception as exc:
            self._forget_agent_runner(workflow_id)
            result = ReportingRunResult(
                run_id=run_id,
                status="failed",
                error=str(exc),
            )
            self._save_run(result)
            await self._notice(f"配电报告流程失败：{exc}")
            return result

        artifacts: list[OutputArtifact] = state.get("output_artifacts", [])
        try:
            output_paths = verify_current_run_outputs(
                self.workspace,
                run_id,
                artifacts,
                execution_started_ns,
                allow_existing_run_artifacts=resume,
                allow_existing_artifacts=bool(state.get("delivery_restored")),
            )
        except OutputVerificationError as exc:
            result = ReportingRunResult(
                run_id=run_id,
                status="failed",
                error=str(exc),
            )
            self._save_run(result)
            await self._notice(
                f"配电报告流程失败：{run_id} 未生成可验证的本次交付产物。"
            )
            return result
        result = ReportingRunResult(
            run_id=run_id,
            status="completed",
            output_paths=output_paths,
        )
        self._save_run(result)
        await self._notice(
            "配电报告流程已完成："
            + ", ".join(str(path.relative_to(self.workspace)) for path in result.output_paths)
        )
        return result

    def _render_existing(self, state: dict) -> None:
        """Render one approved project-local Markdown artifact without analysis Agents."""

        request: ReportRequest = state["request"]
        source_ref = request.source_markdown_ref
        if source_ref is None:
            raise ValueError("render_existing requires source_markdown_ref")
        self._render_markdown(state, source_ref, request.output_filename)

    def _render_markdown(
        self,
        state: dict,
        source_ref: Path,
        output_filename: str | None,
    ) -> None:
        """Render one project-local Markdown artifact through the deterministic component."""

        run_id = state["run_id"]
        source = (self.workspace / source_ref).resolve()
        if not source.is_relative_to(self.workspace) or not source.is_file():
            raise FileNotFoundError(f"render source does not exist: {source_ref}")
        markdown = source.read_text(encoding="utf-8")
        if not markdown.strip():
            raise ValueError("render source Markdown is empty")

        selected_template, template_source = self.resolve_report_template()
        template_snapshot = (
            self.workspace / f"Work/runs/{run_id}/templates/report_template.docx"
        )
        template_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected_template, template_snapshot)
        template_sha256 = hashlib.sha256(template_snapshot.read_bytes()).hexdigest()

        filename = output_filename or f"{source.stem}.docx"
        output_ref = Path("Outputs/Reports") / filename
        output = self.workspace / output_ref
        render_request = RenderRequest(
            run_id=run_id,
            source_markdown_ref=source.relative_to(self.workspace),
            template_ref=template_snapshot.relative_to(self.workspace),
            output_ref=output_ref,
        )
        self.store.write_json(
            f"Work/runs/{run_id}/render-request.json",
            render_request.model_dump(mode="json"),
        )

        _rendered_name, raw_docx = PackagedV2DocxCore(template_snapshot).render_approved_prose(
            markdown,
            filename=filename,
            report_model=None,
        )
        title = next(
            (
                line.removeprefix("#").strip()
                for line in markdown.splitlines()
                if line.startswith("# ")
            ),
            "配电安全专家咨询报告",
        )
        rendered_document = Document(io.BytesIO(raw_docx))
        PdsDocxRenderer._ensure_title(rendered_document, title)
        rendered_buffer = io.BytesIO()
        rendered_document.save(rendered_buffer)
        raw_docx = rendered_buffer.getvalue()
        output.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                prefix=f".{output.stem}-",
                suffix=".docx",
                dir=output.parent,
                delete=False,
            ) as temporary:
                temporary.write(raw_docx)
                temporary_path = Path(temporary.name)
            verify_rendered_markdown(temporary_path, markdown)
            temporary_path.replace(output)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        output_sha256 = hashlib.sha256(output.read_bytes()).hexdigest()
        render_log_ref = Path(f"Work/runs/{run_id}/render-result.json")
        render_result = RenderResult(
            status="completed",
            run_id=run_id,
            source_markdown_ref=source.relative_to(self.workspace),
            output_ref=output_ref,
            render_log_ref=render_log_ref,
            template_sha256=template_sha256,
            output_sha256=output_sha256,
            protected_prose_verified=True,
        )
        self.store.write_json(render_log_ref.as_posix(), render_result.model_dump(mode="json"))
        self.store.write_json(
            f"Work/runs/{run_id}/template-provenance.json",
            {
                "source": template_source,
                "selected_path": (
                    selected_template.relative_to(self.workspace).as_posix()
                    if selected_template.is_relative_to(self.workspace)
                    else "manyselves/templates/reporting/report_template.docx"
                ),
                "snapshot_path": template_snapshot.relative_to(self.workspace).as_posix(),
                "sha256": template_sha256,
            },
        )
        state["render_result"] = render_result
        state["output_artifacts"] = [
            *state.get("output_artifacts", []),
            OutputArtifact(kind="report", path=output_ref),
        ]

    async def resume(
        self,
        decision_id: str | None,
        action: EvidenceDecisionAction,
        supplements: list[UserSupplement] | None = None,
    ) -> ReportingRunResult:
        if not decision_id:
            raise ValueError("decision_id is required")
        current = self.decisions.load(decision_id)
        if action not in current.allowed_actions:
            raise ValueError(f"action is not allowed for evidence decision: {action}")
        if current.status == "resolved" and current.selected_action != action:
            raise ValueError(
                f"evidence decision already resolved as {current.selected_action}: "
                f"{decision_id}"
            )
        if current.status not in {"pending", "resolved"}:
            raise ValueError(f"evidence decision cannot be resumed: {decision_id}")
        if supplements and action != "supplement":
            raise ValueError("supplements are only valid with action=supplement")
        typed_supplements = [
            UserSupplement.model_validate(item) for item in (supplements or [])
        ]
        request_path = self.workspace / f"Work/runs/{current.run_id}/request.json"
        if not request_path.is_file():
            raise FileNotFoundError(f"report request is missing for run: {current.run_id}")
        request = ReportRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        resumed_request: ReportRequest | None = None
        if action != "stop":
            resumed_request = ReportRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    "missing_evidence_policy": (
                        "ask" if action == "supplement" else action
                    ),
                    "user_supplements": [
                        *request.user_supplements,
                        *typed_supplements,
                    ],
                }
            )

        if current.status == "resolved":
            result_path = self.workspace / f"Work/runs/{current.run_id}.json"
            previous = (
                ReportingRunResult.model_validate_json(
                    result_path.read_text(encoding="utf-8")
                )
                if result_path.is_file()
                else None
            )
            if previous is not None and previous.status != "needs_user_decision":
                raise ValueError(
                    f"evidence decision is already applied; resume the run by run_id: "
                    f"{current.run_id}"
                )
            decision = current
        else:
            # All request and supplement validation must finish before the durable
            # decision is consumed. If a process crashes after this point, a retry
            # with the same action reconciles the partially applied decision.
            decision = self.decisions.resolve(decision_id, action)
        self.store.write_json(
            f"Work/runs/{decision.run_id}/evidence-choice.json",
            decision.model_dump(mode="json"),
        )
        if action == "stop":
            result = ReportingRunResult(
                run_id=decision.run_id,
                status="stopped_incomplete",
                missing_evidence=decision.missing_items,
                decision_id=decision.decision_id,
            )
            self._save_run(result)
            await self._notice("用户选择停止；本次报告保持未完成，未生成成功交付成果。")
            return result
        assert resumed_request is not None
        self.store.write_json(
            f"Work/runs/{decision.run_id}/request.json",
            resumed_request.model_dump(mode="json"),
        )
        if resumed_request.user_supplements:
            self.store.write_json(
                f"Work/runs/{decision.run_id}/user-supplements.json",
                {
                    "run_id": decision.run_id,
                    "supplements": [
                        item.model_dump(mode="json")
                        for item in resumed_request.user_supplements
                    ],
                },
            )
        return await self._execute(resumed_request, decision.run_id)

    async def resume_run(
        self,
        run_id: str,
        *,
        max_provider_attempts: int | None = None,
        max_total_tokens: int | None = None,
        supplements: list[UserSupplement] | None = None,
    ) -> ReportingRunResult:
        """Resume a checkpoint and synchronize any newly supplied user facts."""

        _result_path, request_path, revision_path, _checkpoint_path, _previous = (
            self.validate_resume_run(run_id)
        )
        await self._notice(f"正在从已保存检查点恢复报告流程 {run_id}。")
        if request_path.is_file():
            request = ReportRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
            updates: dict = {
                "user_supplements": [
                    *request.user_supplements,
                    *(
                        UserSupplement.model_validate(item)
                        for item in (supplements or [])
                    ),
                ]
            }
            if max_provider_attempts is not None:
                updates["max_provider_attempts"] = max_provider_attempts
            if max_total_tokens is not None:
                updates["max_total_tokens"] = max_total_tokens
            resumed_request = ReportRequest.model_validate(
                {
                    **request.model_dump(mode="json"),
                    **updates,
                }
            )
            self.store.write_json(
                f"Work/runs/{run_id}/request.json", resumed_request.model_dump(mode="json")
            )
            if resumed_request.user_supplements:
                self.store.write_json(
                    f"Work/runs/{run_id}/user-supplements.json",
                    {
                        "run_id": run_id,
                        "supplements": [
                            item.model_dump(mode="json")
                            for item in resumed_request.user_supplements
                        ],
                    },
                )
            return await self._execute(resumed_request, run_id, resume=True)

        from .revisions import RevisionCoordinator

        revision = RevisionRequest.model_validate_json(revision_path.read_text(encoding="utf-8"))
        updates = {}
        if max_provider_attempts is not None:
            updates["max_provider_attempts"] = max_provider_attempts
        if max_total_tokens is not None:
            updates["max_total_tokens"] = max_total_tokens
        if supplements:
            typed = [UserSupplement.model_validate(item) for item in supplements]
            updates["user_supplements"] = [
                *revision.user_supplements,
                *typed,
            ]
        resumed_revision = RevisionRequest.model_validate(
            {
                **revision.model_dump(mode="json"),
                **updates,
            }
        )
        self.store.write_json(
            f"Work/runs/{run_id}/revision-request.json",
            resumed_revision.model_dump(mode="json"),
        )
        if resumed_revision.user_supplements:
            self.store.write_json(
                f"Work/runs/{run_id}/user-supplements.json",
                {
                    "run_id": run_id,
                    "supplements": [
                        item.model_dump(mode="json")
                        for item in resumed_revision.user_supplements
                    ],
                },
            )
        runner = ReportingAgentRunner(
            self.workspace,
            self.bus,
            self.llm_provider,
            self.agent_defaults,
        )
        return await RevisionCoordinator(self, runner).run(
            resumed_revision, run_id=run_id, resume=True
        )

    def validate_resume_run(
        self, run_id: str
    ) -> tuple[Path, Path, Path, Path, ReportingRunResult | None]:
        """Validate a same-run resume before a background task is announced."""

        if Path(run_id).name != run_id or not run_id:
            raise ValueError("run_id must be a single safe path component")
        result_path = self.workspace / f"Work/runs/{run_id}.json"
        request_path = self.workspace / f"Work/runs/{run_id}/request.json"
        revision_path = self.workspace / f"Work/runs/{run_id}/revision-request.json"
        checkpoint_path = self.workspace / f"Work/runs/{run_id}/workflow-state.json"
        if not (request_path.is_file() or revision_path.is_file()):
            raise FileNotFoundError(f"report run is not resumable: {run_id}")
        previous = (
            ReportingRunResult.model_validate_json(result_path.read_text(encoding="utf-8"))
            if result_path.is_file()
            else None
        )
        budget_stopped = (
            previous is not None
            and previous.status == "needs_decision"
            and "预算" in str(previous.error or "")
        )
        checkpoint_resumable = (
            checkpoint_path.is_file()
            and (
                previous is None
                or previous.status
                in {"failed", "cancelled", "in_progress", "needs_decision", "blocked"}
            )
        )
        if not (budget_stopped or checkpoint_resumable):
            raise ValueError(
                "only a blocked, decision-stopped, crashed, failed, or cancelled run with a persisted checkpoint "
                "can use run resume"
            )
        return (
            result_path,
            request_path,
            revision_path,
            checkpoint_path,
            previous,
        )

    async def revise(
        self, request: RevisionRequest, *, run_id: str | None = None
    ) -> ReportingRunResult:
        from .revisions import RevisionCoordinator

        runner = ReportingAgentRunner(
            self.workspace,
            self.bus,
            self.llm_provider,
            self.agent_defaults,
        )
        return await RevisionCoordinator(self, runner).run(request, run_id=run_id)

    async def _notice(self, content: str) -> None:
        await self.bus.publish(SystemNotice(agent_type=AgentType.MAIN, content=content))

    def _save_run(self, result: ReportingRunResult) -> Path:
        rows = UsageLedger(self.workspace, result.run_id).rows()
        result.usage = {
            "provider_attempts": len(rows),
            "input_tokens": sum(int(row.get("input_tokens", 0) or 0) for row in rows),
            "output_tokens": sum(int(row.get("output_tokens", 0) or 0) for row in rows),
            "total_tokens": sum(int(row.get("total_tokens", 0) or 0) for row in rows),
        }
        return self.store.write_json(
            f"Work/runs/{result.run_id}.json",
            result.model_dump(mode="json"),
        )

    async def _build_manifest(self, state: dict) -> None:
        manifest = build_manifest(self.workspace)
        state["project_manifest"] = manifest
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _parse_artifacts(self, state: dict) -> None:
        manifest: ProjectManifest = state["project_manifest"]
        artifacts: list[ParsedArtifact] = []
        registry = IntakeAdapterRegistry()
        for manifest_file in manifest.files:
            if manifest_file.purpose in {"s2-1", "s4-4", "s4-6"}:
                continue
            try:
                artifacts.extend(registry.parse(self.workspace / manifest_file.path, manifest_file))
                manifest_file.parse_status = "parsed"
            except Exception as exc:
                manifest_file.parse_status = "failed"
                manifest_file.error = str(exc)
        state["parsed_artifacts"] = artifacts
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _normalize_evidence(self, state: dict) -> None:
        evidence: list[EvidenceItem] = []
        manifest: ProjectManifest = state["project_manifest"]
        photo_assets: list[PhotoAsset] = []
        mapping_gaps: list[dict] = []
        mappers = {
            "s2-1": map_s2_1,
            "s4-4": map_s4_4,
            "s4-6": map_s4_6,
        }
        for manifest_file in manifest.files:
            mapper = mappers.get(manifest_file.purpose or "")
            if mapper is None:
                continue
            input_path = self.workspace / manifest_file.path
            try:
                extracted: dict[str, PhotoAsset] = {}
                if manifest_file.purpose == "s4-4":
                    extracted = extract_wps_images(
                        input_path,
                        output_dir=(
                            self.workspace
                            / "Work"
                            / "runs"
                            / str(state["run_id"])
                            / "assets"
                            / manifest_file.id
                        ),
                    )
                mapped = mapper(input_path, file_id=manifest_file.id)
                mapped_evidence = [
                    item.model_copy(
                        update={
                            "source": item.source.model_copy(update={"path": manifest_file.path})
                        }
                    )
                    for item in mapped.evidence_items
                ]
                if manifest_file.purpose == "s4-4":
                    mapped_evidence, normalized_assets = canonicalize_photo_bindings(
                        mapped_evidence,
                        extracted,
                        start_index=len(photo_assets) + 1,
                    )
                    photo_assets.extend(
                        asset.model_copy(
                            update={"path": asset.path.relative_to(self.workspace)}
                        )
                        for asset in normalized_assets
                    )
                evidence.extend(mapped_evidence)
                mapping_gaps.extend(
                    {
                        "file_id": manifest_file.id,
                        **gap.model_dump(mode="json"),
                    }
                    for gap in mapped.gaps
                )
                manifest_file.parse_status = "parsed"
            except Exception as exc:
                manifest_file.parse_status = "failed"
                manifest_file.error = str(exc)

        for artifact in state.get("parsed_artifacts", []):
            if artifact.kind == "manual_required":
                mapping_gaps.append(
                    {
                        "file_id": artifact.source.file_id,
                        "kind": "manual_required",
                        **artifact.payload,
                    }
                )
                continue
            if artifact.kind == "workbook_row":
                pairs = [
                    f"{header}={value}"
                    for header, value in zip(
                        artifact.payload["headers"],
                        artifact.payload["values"],
                        strict=False,
                    )
                    if value not in (None, "")
                ]
                if not pairs:
                    continue
                subject = str(artifact.payload["values"][0])
                fact = "; ".join(pairs)
            elif artifact.kind in {"text", "document_paragraph", "pdf_page"}:
                fact = str(artifact.payload.get("text", "")).strip()
                if not fact:
                    continue
                subject = artifact.source.path.name
            elif artifact.kind == "image_metadata":
                subject = artifact.source.path.name
                fact = (
                    f"图片元数据：{artifact.payload['width']}x{artifact.payload['height']}，"
                    f"格式={artifact.payload['format']}，模式={artifact.payload['mode']}"
                )
            else:
                continue
            evidence.append(
                EvidenceItem(
                    id=f"ev-{len(evidence) + 1:04d}",
                    subject=subject,
                    fact=fact,
                    source=artifact.source,
                )
            )
        # Runtime source tools and ClaimLedger use stable E-* identifiers. Mapper
        # internals may emit legacy ev-* ids, so normalize once at the boundary.
        evidence_id_map: dict[str, str] = {}
        normalized_evidence: list[EvidenceItem] = []
        for index, item in enumerate(evidence, start=1):
            if item.id in evidence_id_map:
                raise ValueError(f"duplicate pre-normalization evidence id: {item.id}")
            normalized_id = f"E-{index:04d}"
            evidence_id_map[item.id] = normalized_id
            normalized_evidence.append(item.model_copy(update={"id": normalized_id}))
        evidence = normalized_evidence
        normalized_photo_assets: list[PhotoAsset] = []
        for asset in photo_assets:
            primary_evidence_id = asset.primary_evidence_id
            if primary_evidence_id is None:
                raise ValueError(f"photo {asset.id} is missing its primary evidence binding")
            normalized_primary_id = evidence_id_map.get(primary_evidence_id)
            if normalized_primary_id is None:
                raise ValueError(
                    f"photo {asset.id} references unknown primary evidence "
                    f"{primary_evidence_id}"
                )
            normalized_photo_assets.append(
                asset.model_copy(
                    update={"primary_evidence_id": normalized_primary_id}
                )
            )
        photo_assets = normalized_photo_assets
        state["evidence_items"] = evidence
        state["photo_assets"] = photo_assets
        state["mapping_gaps"] = mapping_gaps
        self.store.write_jsonl(
            "Work/evidence.jsonl",
            [item.model_dump(mode="json") for item in evidence],
        )
        self.store.write_json(
            "Work/photo-manifest.json",
            {"assets": [asset.model_dump(mode="json") for asset in photo_assets]},
        )
        self.store.write_json("Work/mapping-gaps.json", {"gaps": mapping_gaps})
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _evaluate_coverage(self, state: dict) -> None:
        request: ReportRequest = state["request"]
        evidence: list[EvidenceItem] = state.get("evidence_items", [])
        coverage = evaluate_coverage(
            request,
            evidence,
            mapping_gaps=state.get("mapping_gaps", []),
        )
        state["coverage_matrix"] = coverage
        self.store.write_json("Work/coverage.json", coverage.model_dump(mode="json"))
