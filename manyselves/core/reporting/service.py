"""Complete V2 reporting service executed inside the Manyselves runtime."""

import asyncio
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import stat
import tempfile
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from docx import Document
from pydantic import BaseModel, ConfigDict, Field

from ...config.schema import AgentDefaults
from ...interfaces.types import AgentType, SystemNotice
from ..artifacts.content_store import ContentAddressedStore
from ..loops.bus import MessageBus
from ..providers.base import LLMProvider
from ..tools.task_board import TaskBoard
from ..usage_ledger import UsageLedger
from .agent_runner import ReportingAgentRunner
from .assets import ReportAssetAssembler
from .config import load_packaged_agents
from .coverage import evaluate_coverage
from .decisions import EvidenceDecisionStore
from .evidence_readiness import ReportingBlockedError
from .execution_runtime import ProviderRouter
from .input_snapshot import RunInputSnapshotStore
from .intake.manifest import build_manifest
from .intake.wps_images import canonicalize_photo_bindings, extract_wps_images
from .locks import exclusive_reporting_writer_lock
from .mappers import map_s2_1, map_s4_4, map_s4_6
from .models import (
    REPORT_MODULE_IDS,
    CostControlMode,
    EvidenceDecisionAction,
    EvidenceDecisionRequest,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    ProjectManifest,
    ReportRequest,
    RevisionRequest,
    UserSupplement,
)
from .parallel_runtime import (
    ProjectWriteLease,
    ProjectWriteLeaseManager,
    bind_project_write_lease,
    reset_project_write_lease,
    validate_bound_project_write_lease,
)
from .preparation import FilePreparationResult, prepare_manifest_file
from .provider_admission import ProviderAdmissionController
from .rendering import PackagedV2DocxCore, PdsDocxRenderer, RenderRequest, RenderResult
from .rendering.rendered_docx_validator import validate_rendered_markdown_docx
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
        provider_router: ProviderRouter | None = None,
        provider_admission: ProviderAdmissionController | None = None,
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
        self.provider_router = provider_router or ProviderRouter(llm_provider)
        # This controller is owned by the service, not by a module lane or an
        # Agent session.  Consequently all real Provider requests share the
        # same concurrency ceiling and 429 cooldown.  Business task readiness
        # is still controlled independently by the workflow scheduler.
        self.provider_admission = provider_admission or ProviderAdmissionController(
            global_concurrency=8,
            provider_model_concurrency=8,
            cooldown_jitter_seconds=0.25,
        )
        self.store = ReportingStore(self.workspace)
        self.content_store = ContentAddressedStore(self.workspace)
        self.decisions = EvidenceDecisionStore(self.workspace)
        self.agents = load_packaged_agents()
        self._active_agent_runners: dict[str, ReportingAgentRunner] = {}
        template_root = Path(__file__).resolve().parents[2] / "templates" / "reporting"
        self.packaged_report_template_path = template_root / "report_template.docx"

    def snapshot_content(
        self,
        source: Path,
        target: Path,
        *,
        replace_existing_with_view: bool = False,
    ) -> tuple[Path, str, Path]:
        """Ingest bytes once and expose an immutable project-local compatibility view."""

        source = Path(source)
        target = Path(target)
        validate_bound_project_write_lease(self.workspace)
        blob = self.content_store.ingest_file(source)
        trusted = self.content_store.issue_trusted_handle(
            blob,
            lineage_id=(
                "snapshot:"
                + (
                    target.relative_to(self.workspace).as_posix()
                    if target.resolve().is_relative_to(self.workspace)
                    else target.as_posix()
                )
            ),
        )
        if target.exists() or target.is_symlink():
            if not target.is_file():
                raise ValueError(f"content snapshot target is not a file: {target}")
            target_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
            if target_sha256 != blob.sha256:
                raise ValueError(
                    "immutable content snapshot already exists with different bytes: "
                    f"{target}"
                )
            if replace_existing_with_view and target.resolve() != blob.path:
                staged = target.with_name(
                    f".{target.name}.{uuid.uuid4().hex}.cas-view"
                )
                try:
                    self.content_store.link_trusted_view(
                        trusted,
                        staged,
                        final_path=target,
                    )
                    validate_bound_project_write_lease(self.workspace)
                    os.replace(staged, target)
                finally:
                    staged.unlink(missing_ok=True)
        else:
            validate_bound_project_write_lease(self.workspace)
            self.content_store.link_trusted_view(trusted, target)
        return target, blob.sha256, blob.relative_path

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
                provider_router=self.provider_router,
                provider_admission=self.provider_admission,
            )
            self._active_agent_runners[workflow_id] = runner
        return runner

    def _forget_agent_runner(self, workflow_id: str | None) -> None:
        if workflow_id is not None:
            self._active_agent_runners.pop(workflow_id, None)

    def resolve_report_template(
        self, run_id: str | None = None
    ) -> tuple[Path, str]:
        """Resolve the current template with project scope taking precedence."""
        project_template = (
            self.workspace
            / "Work"
            / "runs"
            / run_id
            / "frozen-project"
            / self.PROJECT_TEMPLATE_PATH
            if run_id is not None
            else self.workspace / self.PROJECT_TEMPLATE_PATH
        )
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

    def resolve_skill_distillation_template(
        self, run_id: str | None = None
    ) -> tuple[Path, str]:
        """Select the expert source only for the isolated Skill distillation task."""

        expert_source = (
            self.workspace
            / "Work"
            / "runs"
            / run_id
            / "frozen-project"
            / self.EXPERT_SKILL_SOURCE_PATH
            if run_id is not None
            else self.workspace / self.EXPERT_SKILL_SOURCE_PATH
        )
        if expert_source.exists():
            if not expert_source.is_file():
                raise ValueError(
                    "expert Skill source must be a DOCX file: "
                    f"{self.EXPERT_SKILL_SOURCE_PATH}"
                )
            if not expert_source.resolve().is_relative_to(self.workspace):
                raise ValueError("expert Skill source must stay inside the project workspace")
            return expert_source, "expert-skill-source"
        return self.resolve_report_template(run_id)

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

    def _copy_referenced_existing_assets(
        self,
        run_id: str,
        source_refs: list[Path],
    ) -> None:
        """Copy the E/R/W/P closure used by imported report prose into a new run.

        This is deliberately a recovery-oriented file copy, not a CAS or hash
        identity gate.  Missing legacy sidecars are recorded as gaps so usable
        module prose can still be aggregated.
        """

        source_ids: set[str] = set()
        for ref in source_refs:
            path = self.workspace / ref
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                continue
            source_ids.update(re.findall(r"\b[ERWP]-\d{3,}\b", text))
        if not source_ids:
            return

        gaps: list[dict[str, str]] = []
        evidence_by_id: dict[str, dict] = {}
        global_evidence = self.workspace / "Work/evidence.jsonl"
        if global_evidence.is_file():
            try:
                evidence_lines = global_evidence.read_text(encoding="utf-8").splitlines()
            except (OSError, UnicodeDecodeError):
                evidence_lines = []
                gaps.append({"kind": "evidence", "reason": "unreadable"})
            for line in evidence_lines:
                if not line.strip():
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    gaps.append({"kind": "evidence", "reason": "invalid_jsonl_line"})
                    continue
                evidence_id = str(item.get("id", ""))
                if evidence_id in source_ids:
                    evidence_by_id[evidence_id] = item
                    source_ids.update(
                        str(photo_id)
                        for photo_id in item.get("photo_refs", [])
                        if re.fullmatch(r"P-\d{3,}", str(photo_id))
                    )

        ledger_records: dict[str, dict] = {}
        record_origins: dict[str, Path] = {}
        ledger_paths = sorted(
            (self.workspace / "Work/runs").glob("*/ledgers/sources.json"),
            key=lambda path: path.stat().st_mtime_ns,
            reverse=True,
        )
        wanted_source_ids = {
            item for item in source_ids if re.fullmatch(r"[ERW]-\d{3,}", item)
        }
        for ledger_path in ledger_paths:
            if wanted_source_ids.issubset(ledger_records):
                break
            try:
                records = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                continue
            if not isinstance(records, list):
                continue
            for record in records:
                source_id = str(record.get("id", "")) if isinstance(record, dict) else ""
                if source_id in wanted_source_ids and source_id not in ledger_records:
                    ledger_records[source_id] = record
                    record_origins[source_id] = ledger_path.parents[1]

        run_root = self.workspace / "Work/runs" / run_id
        imported_sources = run_root / "sources"
        imported_sources.mkdir(parents=True, exist_ok=True)
        for source_id in sorted(wanted_source_ids):
            origin = record_origins.get(source_id)
            source_content = origin / "sources" / f"{source_id}.txt" if origin else None
            if source_content is not None and source_content.is_file():
                try:
                    shutil.copyfile(source_content, imported_sources / f"{source_id}.txt")
                except OSError:
                    gaps.append(
                        {"id": source_id, "kind": "source", "reason": "copy_failed"}
                    )
            elif source_id in evidence_by_id:
                try:
                    (imported_sources / f"{source_id}.txt").write_text(
                        json.dumps(evidence_by_id[source_id], ensure_ascii=False),
                        encoding="utf-8",
                    )
                except OSError:
                    gaps.append(
                        {"id": source_id, "kind": "source", "reason": "copy_failed"}
                    )
            else:
                gaps.append({"id": source_id, "kind": "source", "reason": "not_found"})

        if ledger_records:
            self.store.write_json(
                f"Work/runs/{run_id}/ledgers/sources.json",
                [ledger_records[key] for key in sorted(ledger_records)],
            )
        if evidence_by_id:
            self.store.write_jsonl(
                f"Work/runs/{run_id}/evidence.jsonl",
                [evidence_by_id[key] for key in sorted(evidence_by_id)],
            )

        wanted_photo_ids = {
            item for item in source_ids if re.fullmatch(r"P-\d{3,}", item)
        }
        imported_photos: list[dict] = []
        global_photos = self.workspace / "Work/photo-manifest.json"
        if wanted_photo_ids and global_photos.is_file():
            try:
                photo_payload = json.loads(global_photos.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                photo_payload = {}
                gaps.append({"kind": "photo_manifest", "reason": "unreadable"})
            by_id = {
                str(item.get("id")): item
                for item in photo_payload.get("assets", [])
                if isinstance(item, dict)
            }
            for photo_id in sorted(wanted_photo_ids):
                raw = by_id.get(photo_id)
                if raw is None:
                    gaps.append({"id": photo_id, "kind": "photo", "reason": "not_found"})
                    continue
                source_path = self.workspace / Path(str(raw.get("path", "")))
                if not source_path.is_file():
                    gaps.append({"id": photo_id, "kind": "photo", "reason": "file_missing"})
                    continue
                suffix = source_path.suffix if len(source_path.suffix) <= 12 else ""
                target_ref = Path(f"Work/runs/{run_id}/assets/imported/{photo_id}{suffix}")
                target = self.workspace / target_ref
                target.parent.mkdir(parents=True, exist_ok=True)
                try:
                    shutil.copyfile(source_path, target)
                except OSError:
                    gaps.append({"id": photo_id, "kind": "photo", "reason": "copy_failed"})
                    continue
                imported_photos.append({**raw, "path": target_ref.as_posix()})
        if imported_photos:
            self.store.write_json(
                f"Work/runs/{run_id}/context/photo-manifest.json",
                {"assets": imported_photos},
            )
        self.store.write_json(
            f"Work/runs/{run_id}/context/imported-provenance.json",
            {
                "source_ids": sorted(source_ids),
                "copied_source_ids": sorted(ledger_records),
                "copied_evidence_ids": sorted(evidence_by_id),
                "copied_photo_ids": sorted(
                    str(item["id"]) for item in imported_photos
                ),
                "gaps": gaps,
            },
        )

    def prepare_run(self, request: ReportRequest) -> str:
        """Persist a new run request and return its stable id before execution starts."""
        run_id = f"report-{uuid.uuid4().hex[:10]}"
        with exclusive_reporting_writer_lock(self.workspace):
            self.store.ensure_layout()
            self.store.write_json(
                f"Work/runs/{run_id}/request.json",
                request.model_dump(mode="json"),
            )
            explicit_refs: list[Path] = []
            if request.source_markdown_ref is not None:
                explicit_refs.append(request.source_markdown_ref)
            if request.operation == "aggregate_existing":
                if request.source_module_refs is not None:
                    explicit_refs.extend(request.source_module_refs.values())
                else:
                    default_refs = [
                        Path(f"Outputs/Modules/{module_id}.md")
                        for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5")
                    ]
                    explicit_refs.extend(
                        ref for ref in default_refs if (self.workspace / ref).is_file()
                    )
            RunInputSnapshotStore(self.workspace).freeze(
                run_id,
                extra_refs=tuple(explicit_refs),
            )
            if request.operation in {"aggregate_existing", "render_existing"}:
                self._copy_referenced_existing_assets(run_id, explicit_refs)
        return run_id

    async def run_prepared(self, request: ReportRequest, run_id: str) -> ReportingRunResult:
        """Execute a request previously persisted by :meth:`prepare_run`."""
        return await self._execute(request, run_id)

    async def run_prepared_claimed(
        self,
        request: ReportRequest,
        run_id: str,
        project_write_lease: ProjectWriteLease,
        *,
        resume: bool = False,
    ) -> ReportingRunResult:
        """Execute beneath a durable worker's already-acquired project lease."""

        manager = ProjectWriteLeaseManager(self.workspace)
        manager.validate(project_write_lease)
        if (
            project_write_lease.run_id != run_id
            or project_write_lease.operation != request.operation
        ):
            raise ValueError("claimed project lease does not match report request")
        token = bind_project_write_lease(self.workspace, project_write_lease)
        lock_handle = None
        try:
            with exclusive_reporting_writer_lock(self.workspace):
                lock_handle = self._acquire_run_lock(run_id)
                return await self._execute_locked(
                    request,
                    run_id,
                    resume=resume,
                    project_write_lease=project_write_lease,
                )
        finally:
            if lock_handle is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            reset_project_write_lease(token)

    def prepare_revision_run(self, request: RevisionRequest) -> str:
        run_id = f"report-revision-{uuid.uuid4().hex[:10]}"
        with exclusive_reporting_writer_lock(self.workspace):
            self.store.ensure_layout()
            self.store.write_json(
                f"Work/runs/{run_id}/revision-request.json",
                request.model_dump(mode="json"),
            )
            RunInputSnapshotStore(self.workspace).freeze(run_id)
        return run_id

    async def run_revision_claimed(
        self,
        request: RevisionRequest,
        run_id: str,
        project_write_lease: ProjectWriteLease,
        *,
        resume: bool = False,
    ) -> ReportingRunResult:
        from .revisions import RevisionCoordinator

        manager = ProjectWriteLeaseManager(self.workspace)
        manager.validate(project_write_lease)
        if (
            project_write_lease.run_id != run_id
            or project_write_lease.operation != "revision"
        ):
            raise ValueError("claimed project lease does not match revision request")
        token = bind_project_write_lease(self.workspace, project_write_lease)
        lock_handle = None
        workflow_id = f"report-revision:{run_id}"
        try:
            with exclusive_reporting_writer_lock(self.workspace):
                lock_handle = self._acquire_run_lock(run_id)
                result = await RevisionCoordinator(
                    self,
                    self._agent_runner_for(workflow_id),
                )._run_locked(
                    request,
                    run_id=run_id,
                    resume=resume,
                )
            if result.status != "needs_decision":
                self._forget_agent_runner(workflow_id)
            return result
        except BaseException:
            self._forget_agent_runner(workflow_id)
            raise
        finally:
            if lock_handle is not None:
                fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                lock_handle.close()
            reset_project_write_lease(token)

    async def _execute(
        self, request: ReportRequest, run_id: str, *, resume: bool = False
    ) -> ReportingRunResult:
        with exclusive_reporting_writer_lock(self.workspace):
            project_lease = ProjectWriteLeaseManager(self.workspace).acquire(
                run_id,
                request.operation,
            )
            lease_token = bind_project_write_lease(
                self.workspace,
                project_lease.lease,
            )
            lock_handle = None
            try:
                lock_handle = self._acquire_run_lock(run_id)
                return await self._execute_locked(
                    request,
                    run_id,
                    resume=resume,
                    project_write_lease=project_lease.lease,
                )
            finally:
                if lock_handle is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                try:
                    project_lease.release()
                finally:
                    reset_project_write_lease(lease_token)

    def _acquire_run_lock(self, run_id: str):
        safe_run_id = self._safe_run_id(run_id)
        runs_root = self.workspace / "Work" / "runs"
        if runs_root.is_symlink():
            raise ValueError("Work/runs must not be a symbolic link")
        runs_root.mkdir(parents=True, exist_ok=True)
        run_root = runs_root / safe_run_id
        if run_root.is_symlink():
            raise ValueError("report run root must not be a symbolic link")
        run_root.mkdir(parents=True, exist_ok=True)
        lock_path = run_root / ".active.lock"
        if lock_path.is_symlink():
            raise ValueError("report run lock must not be a symbolic link")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        handle = os.fdopen(descriptor, "a+", encoding="utf-8")
        try:
            opened = os.fstat(handle.fileno())
            path_stat = os.lstat(lock_path)
            if (
                not stat.S_ISREG(opened.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or opened.st_nlink != 1
                or path_stat.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise ValueError("report run lock must be one verified regular file")
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = os.lstat(lock_path)
            if (
                locked.st_nlink != 1
                or (opened.st_dev, opened.st_ino)
                != (locked.st_dev, locked.st_ino)
            ):
                raise ValueError("report run lock identity changed while acquiring")
        except BlockingIOError as exc:
            handle.close()
            raise RuntimeError(f"report run is already active: {run_id}") from exc
        except Exception:
            handle.close()
            raise
        return handle

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or "") or run_id in {".", ".."}:
            raise ValueError("run_id must be a single safe path component")
        return run_id

    async def _execute_locked(
        self,
        request: ReportRequest,
        run_id: str,
        *,
        resume: bool = False,
        project_write_lease: ProjectWriteLease | None = None,
    ) -> ReportingRunResult:
        state: dict = {"request": request, "run_id": run_id, "resume": resume}
        if project_write_lease is not None:
            state["project_write_lease_ref"] = (
                ProjectWriteLeaseManager(self.workspace)
                .record_path.relative_to(self.workspace)
                .as_posix()
            )
            state["project_write_lease_epoch"] = project_write_lease.lease_epoch
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
            await self._notice(f"配电报告流程失败，可从检查点恢复：{exc}")
            return result

        if (
            request.operation == "full_report"
            and set(request.target_modules) == set(REPORT_MODULE_IDS)
            and (
                state.get("delivery_status") != "delivered"
                or state.get("delivery_completion_ref")
                != f"Work/runs/{run_id}/delivery-completion.json"
            )
        ):
            result = ReportingRunResult(
                run_id=run_id,
                status="failed",
                error="workflow finished without delivered business lifecycle",
            )
            self._save_run(result)
            await self._notice(
                f"配电报告流程失败：{run_id} 未进入 delivered 业务终态。"
            )
            return result

        artifacts: list[OutputArtifact] = state.get("output_artifacts", [])
        output_paths = self._declared_output_paths(artifacts)
        result = ReportingRunResult(
            run_id=run_id,
            status="completed",
            output_paths=output_paths,
        )
        result = self._finalize_completed_run(result)
        if result.status == "failed":
            await self._notice(f"配电报告流程失败：{result.error}")
            return result
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
        frozen_ref = RunInputSnapshotStore(self.workspace).load(
            str(state["run_id"])
        ).resolve(source_ref)
        self._render_markdown(
            state,
            frozen_ref,
            request.output_filename,
            declared_source_ref=source_ref,
        )

    def _render_markdown(
        self,
        state: dict,
        source_ref: Path,
        output_filename: str | None,
        *,
        declared_source_ref: Path | None = None,
    ) -> None:
        """Render one project-local Markdown artifact through the deterministic component."""

        run_id = state["run_id"]
        source_view = self.workspace / source_ref
        source = source_view.resolve()
        if not source.is_relative_to(self.workspace) or not source_view.is_file():
            raise FileNotFoundError(f"render source does not exist: {source_ref}")
        markdown = source_view.read_text(encoding="utf-8")
        if not markdown.strip():
            raise ValueError("render source Markdown is empty")
        logical_source_ref = declared_source_ref or source_ref
        source_snapshot_ref = (
            source_ref if source_ref != logical_source_ref else None
        )

        selected_template, template_source = self.resolve_report_template(run_id)
        template_snapshot = (
            self.workspace / f"Work/runs/{run_id}/templates/report_template.docx"
        )
        template_snapshot, template_sha256, template_blob_ref = self.snapshot_content(
            selected_template,
            template_snapshot,
        )

        filename = output_filename or f"{logical_source_ref.stem}.docx"
        output_ref = Path("Outputs/Reports") / filename
        output = self.workspace / output_ref
        render_request = RenderRequest(
            run_id=run_id,
            source_markdown_ref=logical_source_ref,
            source_snapshot_ref=source_snapshot_ref,
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
            validation_warnings = validate_rendered_markdown_docx(
                temporary_path,
                markdown,
                expected_title=title,
            )
            validate_bound_project_write_lease(self.workspace)
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
            source_markdown_ref=logical_source_ref,
            source_snapshot_ref=source_snapshot_ref,
            output_ref=output_ref,
            render_log_ref=render_log_ref,
            template_sha256=template_sha256,
            output_sha256=output_sha256,
            protected_prose_verified=not validation_warnings,
            validation_warnings=validation_warnings,
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
                "blob_ref": template_blob_ref.as_posix(),
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
        request_path = self.workspace / f"Work/runs/{current.run_id}/request.json"
        if not request_path.is_file():
            raise FileNotFoundError(f"report request is missing for run: {current.run_id}")
        request = ReportRequest.model_validate_json(request_path.read_text(encoding="utf-8"))
        with exclusive_reporting_writer_lock(self.workspace):
            project_lease = ProjectWriteLeaseManager(self.workspace).acquire(
                current.run_id, request.operation
            )
            token = bind_project_write_lease(self.workspace, project_lease.lease)
            lock_handle = None
            try:
                lock_handle = self._acquire_run_lock(current.run_id)
                return await self._resume_decision_locked(
                    decision_id,
                    action,
                    supplements,
                    project_write_lease=project_lease.lease,
                )
            finally:
                if lock_handle is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                try:
                    project_lease.release()
                finally:
                    reset_project_write_lease(token)

    async def _resume_decision_locked(
        self,
        decision_id: str,
        action: EvidenceDecisionAction,
        supplements: list[UserSupplement] | None,
        *,
        project_write_lease: ProjectWriteLease,
    ) -> ReportingRunResult:
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
        return await self._execute_locked(
            resumed_request,
            decision.run_id,
            resume=True,
            project_write_lease=project_write_lease,
        )

    async def resume_run(
        self,
        run_id: str,
        *,
        cost_control_mode: CostControlMode | None = None,
        max_provider_attempts: int | None = None,
        max_total_tokens: int | None = None,
        supplements: list[UserSupplement] | None = None,
    ) -> ReportingRunResult:
        """Resume a checkpoint and synchronize any newly supplied user facts."""

        _result_path, request_path, _revision_path, _checkpoint_path, _previous = (
            self.validate_resume_run(run_id)
        )
        operation = (
            ReportRequest.model_validate_json(
                request_path.read_text(encoding="utf-8")
            ).operation
            if request_path.is_file()
            else "revision"
        )
        with exclusive_reporting_writer_lock(self.workspace):
            project_lease = ProjectWriteLeaseManager(self.workspace).acquire(
                run_id, operation
            )
            token = bind_project_write_lease(self.workspace, project_lease.lease)
            lock_handle = None
            try:
                lock_handle = self._acquire_run_lock(run_id)
                return await self._resume_run_locked(
                    run_id,
                    cost_control_mode=cost_control_mode,
                    max_provider_attempts=max_provider_attempts,
                    max_total_tokens=max_total_tokens,
                    supplements=supplements,
                    project_write_lease=project_lease.lease,
                )
            finally:
                if lock_handle is not None:
                    fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                    lock_handle.close()
                try:
                    project_lease.release()
                finally:
                    reset_project_write_lease(token)

    async def _resume_run_locked(
        self,
        run_id: str,
        *,
        cost_control_mode: CostControlMode | None,
        max_provider_attempts: int | None,
        max_total_tokens: int | None,
        supplements: list[UserSupplement] | None,
        project_write_lease: ProjectWriteLease,
    ) -> ReportingRunResult:
        """Resume after writer, project lease, and run lock are held."""

        _result_path, request_path, revision_path, _checkpoint_path, _previous = (
            self.validate_resume_run(run_id)
        )
        delivered = self._resume_completed_delivery_only(run_id)
        if delivered is not None:
            return delivered
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
            if cost_control_mode is not None:
                updates["cost_control_mode"] = cost_control_mode
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
            return await self._execute_locked(
                resumed_request,
                run_id,
                resume=True,
                project_write_lease=project_write_lease,
            )

        from .revisions import RevisionCoordinator

        revision = RevisionRequest.model_validate_json(revision_path.read_text(encoding="utf-8"))
        updates = {}
        if cost_control_mode is not None:
            updates["cost_control_mode"] = cost_control_mode
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
        workflow_id = f"report-revision:{run_id}"
        try:
            result = await RevisionCoordinator(
                self,
                self._agent_runner_for(workflow_id),
            )._run_locked(resumed_revision, run_id=run_id, resume=True)
        except BaseException:
            self._forget_agent_runner(workflow_id)
            raise
        if result.status != "needs_decision":
            self._forget_agent_runner(workflow_id)
        return result

    def _resume_completed_delivery_only(self, run_id: str) -> ReportingRunResult | None:
        """Finalize a persisted delivered lifecycle without entering Provider code."""

        completion_path = self.workspace / f"Work/runs/{run_id}/delivery-completion.json"
        if not completion_path.is_file():
            return None
        try:
            payload = json.loads(completion_path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if payload.get("run_id") != run_id:
            return None
        status = str(payload.get("status", ""))
        if status not in {
            "delivered",
            "archive_pending",
            "archived",
            "archive_failed",
            "completed",
        }:
            return None

        try:
            artifacts = [
                OutputArtifact.model_validate(item)
                for item in payload.get("output_artifacts", [])
            ]
        except ValueError:
            # A stale/corrupt completion projection is not a terminal lock.
            # Ignore it and let the normal same-run workflow rebuild delivery
            # from the last valid review boundary.
            return None

        try:
            self._republish_materialized_delivery(run_id)
        except (OSError, ValueError, json.JSONDecodeError):
            # Delivery metadata is audit evidence, not an irreversible
            # completed state. Continue the same run so delivery can be
            # regenerated without re-entering completed Provider stages.
            return None

        payload["status"] = "delivered"
        payload["delivery_status"] = "delivered"
        payload.pop("warning", None)
        self.store.write_json(
            f"Work/runs/{run_id}/delivery-completion.json", payload
        )
        return self._finalize_completed_run(
            ReportingRunResult(
                run_id=run_id,
                status="completed",
                output_paths=self._declared_output_paths(artifacts),
            )
        )

    def _republish_materialized_delivery(self, run_id: str) -> None:
        """Validate and republish a complete current-run delivery package."""

        run_root = (self.workspace / "Work" / "runs" / run_id).resolve()
        receipt_path = run_root / "delivery-receipt.json"
        if not receipt_path.is_file():
            raise ValueError("delivery completion has no current-run receipt")
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        if receipt.get("success") is not True:
            raise ValueError("delivery receipt is not successful")

        def current_run_path(field: str) -> Path:
            raw = Path(str(receipt.get(field, "")))
            path = raw if raw.is_absolute() else self.workspace / raw
            lexical = Path(os.path.abspath(path))
            if not lexical.is_relative_to(run_root):
                raise ValueError(f"delivery receipt {field} is outside the current run")
            return path

        delivery_dir = current_run_path("delivery_dir")
        if not delivery_dir.is_dir():
            raise ValueError("delivery receipt directory is not readable")
        report_state = current_run_path("report_state")
        manifest = current_run_path("manifest_path")
        if not report_state.is_file() or not manifest.is_file():
            raise ValueError("delivery package metadata is incomplete")
        manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
        if (
            manifest_payload.get("status") != "success"
            or manifest_payload.get("modules") != list(REPORT_MODULE_IDS)
        ):
            raise ValueError("delivery manifest identity/status is invalid")

        def copy_file(source_path: Path, target: Path) -> None:
            validate_bound_project_write_lease(self.workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{target.name}.",
                suffix=".tmp",
                dir=target.parent,
                delete=False,
            ) as handle:
                with source_path.open("rb") as source_handle:
                    shutil.copyfileobj(source_handle, handle)
                handle.flush()
                os.fsync(handle.fileno())
                temporary = Path(handle.name)
            try:
                validate_bound_project_write_lease(self.workspace)
                os.replace(temporary, target)
            finally:
                temporary.unlink(missing_ok=True)

        copy_file(
            current_run_path("final_docx"),
            self.workspace / "Outputs/Reports/配电安全专家咨询报告.docx",
        )
        copy_file(
            current_run_path("source_index"),
            self.workspace / "Outputs/Reports/证据与来源索引.md",
        )
        copy_file(
            current_run_path("source_index_docx"),
            self.workspace / "Outputs/Reports/证据与来源索引.docx",
        )
        module_files = receipt.get("module_files")
        if not isinstance(module_files, dict) or set(module_files) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery receipt module_files is not the fixed module set")
        for module_id in REPORT_MODULE_IDS:
            raw = Path(str(module_files[module_id]))
            module_source = raw if raw.is_absolute() else self.workspace / raw
            if (
                not Path(os.path.abspath(module_source)).is_relative_to(run_root)
                or not module_source.is_file()
            ):
                raise ValueError(
                    f"delivery receipt module {module_id} is not a current-run file"
                )
            copy_file(
                module_source,
                self.workspace / f"Outputs/Modules/{module_id}.md",
            )

        markdown_source = run_root / "report/配电安全专家咨询报告.md"
        if markdown_source.is_file():
            copy_file(
                markdown_source,
                self.workspace / "Outputs/Reports/配电安全专家咨询报告.md",
            )

        published_docx = self.workspace / "Outputs/Reports/配电安全专家咨询报告.docx"
        if not self._output_is_readable(published_docx):
            raise ValueError("republished delivery DOCX is not readable")

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
        completed_but_incomplete = (
            previous is not None
            and previous.status == "completed"
            and (
                not previous.output_paths
                or any(
                    not self._output_is_readable(path)
                    for path in previous.output_paths
                )
            )
        )
        run_resumable = (
            previous is None
            or previous.status != "completed"
            or completed_but_incomplete
        )
        if not run_resumable:
            raise ValueError(
                "a genuinely completed run with readable outputs must be revised instead"
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

        run_id = run_id or f"report-revision-{uuid.uuid4().hex[:10]}"
        workflow_id = f"report-revision:{run_id}"
        try:
            result = await RevisionCoordinator(
                self,
                self._agent_runner_for(workflow_id),
            ).run(request, run_id=run_id)
        except BaseException:
            self._forget_agent_runner(workflow_id)
            raise
        if result.status != "needs_decision":
            self._forget_agent_runner(workflow_id)
        return result

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

    def _finalize_completed_run(
        self,
        result: ReportingRunResult,
    ) -> ReportingRunResult:
        """Durably save completion only after declared outputs are readable."""

        if result.status != "completed":
            raise ValueError("only a delivered run can be finalized")
        missing = [
            str(path)
            for path in result.output_paths
            if not self._output_is_readable(path)
        ]
        if not result.output_paths or missing:
            failed = result.model_copy(
                update={
                    "status": "failed_before_delivery",
                    "error": (
                        "completed run has no readable declared outputs"
                        + (f": {missing}" if missing else "")
                    ),
                }
            )
            failed_path = self._save_run(failed)
            self.store.fsync_directory(failed_path.parent)
            return failed
        try:
            result_path = self._save_run(result)
            self.store.fsync_directory(result_path.parent)
        except Exception as exc:
            failed = result.model_copy(
                update={
                    "status": "failed",
                    "error": (
                        "completed run finalization failed: "
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )
            failed_path = self._save_run(failed)
            self.store.fsync_directory(failed_path.parent)
            return failed
        return result

    def _output_is_readable(self, value: Path | str) -> bool:
        """Return whether a declared output is a nonempty readable file."""

        path = Path(value)
        path = path if path.is_absolute() else self.workspace / path
        try:
            if not path.is_file() or path.stat().st_size <= 0:
                return False
            if path.suffix.casefold() == ".docx":
                Document(path)
        # A malformed OPC package can raise python-docx-specific exceptions in
        # addition to OSError/ValueError.  Output validation must classify all
        # parser failures as unreadable so the same run remains recoverable.
        except Exception:
            return False
        return True

    def _declared_output_paths(
        self,
        artifacts: list[OutputArtifact],
    ) -> list[Path]:
        """Project typed declarations to paths without stat, timestamp, hash, or CAS checks."""

        paths: list[Path] = []
        for artifact in artifacts:
            path = artifact.path
            visible = path if path.is_absolute() else self.workspace / path
            if visible not in paths:
                paths.append(visible)
        return paths

    async def _build_manifest(self, state: dict) -> None:
        input_snapshot = RunInputSnapshotStore(self.workspace).load(
            str(state["run_id"])
        )
        state["input_snapshot_ref"] = (
            f"Work/runs/{state['run_id']}/input-snapshot.json"
        )
        state["input_snapshot_digest"] = input_snapshot.inventory_digest
        manifest = build_manifest(
            self.workspace,
            input_root=input_snapshot.scope_root(self.workspace, "Inputs"),
        )
        state["project_manifest"] = manifest
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _parse_artifacts(self, state: dict) -> None:
        manifest: ProjectManifest = state["project_manifest"]
        request: ReportRequest = state["request"]
        indexed_files = list(enumerate(manifest.files))

        async def run_one(
            semaphore: asyncio.Semaphore,
            order: int,
            manifest_file,
        ) -> FilePreparationResult:
            async with semaphore:
                return await asyncio.to_thread(
                    prepare_manifest_file,
                    self.workspace,
                    str(state["run_id"]),
                    manifest_file,
                    order,
                )

        if request.preparation_mode == "deterministic_workers" and len(indexed_files) > 1:
            semaphore = asyncio.Semaphore(
                min(request.preparation_concurrency, len(indexed_files))
            )
            results = await asyncio.gather(
                *(
                    run_one(semaphore, order, manifest_file)
                    for order, manifest_file in indexed_files
                )
            )
        else:
            results = [
                prepare_manifest_file(
                    self.workspace,
                    str(state["run_id"]),
                    manifest_file,
                    order,
                )
                for order, manifest_file in indexed_files
            ]
        results = sorted(results, key=lambda item: item.manifest_order)
        if [item.manifest_order for item in results] != list(range(len(manifest.files))):
            raise RuntimeError("preparation worker results are not a complete manifest order")
        by_id = {item.file_id: item for item in results}
        if len(by_id) != len(results):
            raise RuntimeError("preparation worker returned duplicate file identity")
        for manifest_file in manifest.files:
            result = by_id.get(manifest_file.id)
            if result is None or result.source_sha256 != manifest_file.sha256:
                raise RuntimeError("preparation worker source identity mismatch")
            manifest_file.parse_status = result.status
            manifest_file.error = result.error
            self.store.write_json(
                (
                    f"Work/runs/{state['run_id']}/preparation-workers/results/"
                    f"{result.manifest_order:04d}-{result.file_id}.json"
                ),
                result.model_dump(mode="json"),
            )
        state["preparation_worker_results"] = results
        state["parsed_artifacts"] = [
            artifact
            for result in results
            for artifact in result.parsed_artifacts
        ]
        state["preparation_parallelism"] = {
            "mode": request.preparation_mode,
            "worker_count": (
                min(request.preparation_concurrency, len(indexed_files))
                if request.preparation_mode == "deterministic_workers"
                else 1
            ),
            "file_count": len(indexed_files),
            "reducer_order": [item.file_id for item in results],
        }
        self.store.write_json("Work/manifest.json", manifest.model_dump(mode="json"))

    async def _normalize_evidence(self, state: dict) -> None:
        evidence: list[EvidenceItem] = []
        manifest: ProjectManifest = state["project_manifest"]
        photo_assets: list[PhotoAsset] = []
        mapping_gaps: list[dict] = []
        worker_results: list[FilePreparationResult] | None = state.get(
            "preparation_worker_results"
        )
        if worker_results is not None:
            for result in sorted(
                worker_results, key=lambda item: item.manifest_order
            ):
                if result.status != "parsed":
                    continue
                mapped_evidence = result.provisional_evidence
                if any(item.photo_refs for item in mapped_evidence):
                    mapped_evidence, normalized_assets = canonicalize_photo_bindings(
                        mapped_evidence,
                        result.raw_photo_assets,
                        start_index=len(photo_assets) + 1,
                    )
                    final_asset_root = (
                        self.workspace
                        / "Work"
                        / "runs"
                        / str(state["run_id"])
                        / "assets"
                        / result.file_id
                    )
                    for asset in normalized_assets:
                        final_path = final_asset_root / asset.path.name
                        final_path, _sha256, _blob_ref = self.snapshot_content(
                            asset.path,
                            final_path,
                        )
                        photo_assets.append(
                            asset.model_copy(
                                update={
                                    "path": final_path.relative_to(self.workspace)
                                }
                            )
                        )
                evidence.extend(mapped_evidence)
                mapping_gaps.extend(
                    {"file_id": result.file_id, **gap}
                    for gap in result.mapping_gaps
                )
        else:
            # Compatibility path for direct boundary tests and older adapters.
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
                    mapped = mapper(input_path, file_id=manifest_file.id)
                    mapped_evidence = [
                        item.model_copy(
                            update={
                                "source": item.source.model_copy(
                                    update={"path": manifest_file.path}
                                )
                            }
                        )
                        for item in mapped.evidence_items
                    ]
                    referenced_photo_ids = {
                        photo_id
                        for item in mapped_evidence
                        for photo_id in item.photo_refs
                    }
                    extracted: dict[str, PhotoAsset] = {}
                    if referenced_photo_ids:
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
                            required_image_ids=referenced_photo_ids,
                        )
                        extracted = {
                            asset_key: asset.model_copy(
                                update={
                                    "path": self.snapshot_content(
                                        asset.path,
                                        asset.path,
                                        replace_existing_with_view=True,
                                    )[0]
                                }
                            )
                            for asset_key, asset in extracted.items()
                        }
                    if referenced_photo_ids:
                        mapped_evidence, normalized_assets = canonicalize_photo_bindings(
                            mapped_evidence,
                            extracted,
                            start_index=len(photo_assets) + 1,
                        )
                        photo_assets.extend(
                            asset.model_copy(
                                update={
                                    "path": asset.path.relative_to(self.workspace)
                                }
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
        ReportAssetAssembler.runtime_photo_ids(evidence, photo_assets)
        photo_to_evidence: dict[str, list[str]] = {
            asset.id: [] for asset in photo_assets
        }
        evidence_to_photo: dict[str, list[str]] = {}
        for item in evidence:
            evidence_to_photo[item.id] = list(item.photo_refs)
            for photo_id in item.photo_refs:
                photo_to_evidence.setdefault(photo_id, []).append(item.id)
        photo_adjacency = {
            "schema_version": 1,
            "photo_to_evidence": photo_to_evidence,
            "evidence_to_photo": evidence_to_photo,
            "primary_evidence": {
                asset.id: asset.primary_evidence_id for asset in photo_assets
            },
        }
        state["evidence_items"] = evidence
        state["photo_assets"] = photo_assets
        state["photo_evidence_adjacency"] = photo_adjacency
        state["mapping_gaps"] = mapping_gaps
        self.store.write_jsonl(
            "Work/evidence.jsonl",
            [item.model_dump(mode="json") for item in evidence],
        )
        self.store.write_json(
            "Work/photo-manifest.json",
            {"assets": [asset.model_dump(mode="json") for asset in photo_assets]},
        )
        self.store.write_json("Work/photo-evidence-adjacency.json", photo_adjacency)
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
