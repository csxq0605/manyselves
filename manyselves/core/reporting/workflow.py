"""Outer report workflow: orchestration only; professional reasoning stays in AgentLoop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import shutil
import time
from copy import deepcopy
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import Field

from ..usage_ledger import UsageLedger
from .agent_runner import ProviderAttemptRecoveryRequired, ReportingAgentRunner
from .agentic_models import (
    FINAL_REPORT_SECTION_IDS,
    AgentResult,
    AgentRunStatus,
    CrossDecisionPack,
    CrossDecisionXMRVerdict,
    CrossSynthesisInput,
    EditedReportSubmission,
    ModuleDispatchPlan,
    ModuleSubmission,
    StrictModel,
    TaskEnvelope,
    TemplateSkillBoundaryManifest,
    TemplateSkillSubmission,
    extra_numbered_submodule_headings,
)
from .assets import (
    ReportAssetAssembler,
    expand_approved_module_markers,
    find_shallow_submodules,
    validate_aggregate_retention,
    validate_editor_protection,
    validate_editor_quality,
    validate_existing_markdown_modules,
    validate_final_report_markdown,
    validate_module_markdown_consistency,
)
from .claim_ledger import ClaimLedger
from .cost_control import StageCostController
from .delivery import DeliveryPackage, DeliveryReceipt, ProjectDelivery
from .distributed_runtime import LocalEventStore
from .evidence_readiness import EvidenceReadinessPolicy, ReportingBlockedError
from .input_contracts import (
    AggregateEditorInput,
    ChiefEditorInput,
    CrossDecisionPackView,
    FinalAuditSnapshot,
    ModuleAuthoringInput,
    RequestedModuleChange,
    ReviewCompletionRecord,
    TemplateDistillationInput,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from .input_snapshot import RunInputSnapshotStore
from .models import (
    REPORT_MODULE_IDS,
    CostControlMode,
    CoverageMatrix,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    ProjectManifest,
    RevisionRequest,
    ScopeExpansionRequest,
    SpecialTopicPlan,
)
from .output_verifier import OutputVerificationError, verify_current_run_outputs
from .module_skills import ModuleSkillLibrary
from .parallel_runtime import (
    ArtifactRef,
    CohortBarrier,
    CrossOwnerBarrier,
    CrossOwnerCompletion,
    LaneAttemptRecord,
    LaneCompletion,
    LaneExceptionCandidate,
    LaneTaskSpec,
    TaskAttemptStore,
    WorkflowReducer,
    current_bound_project_write_lease,
    validate_bound_project_write_lease,
)
from .rendering.contracts import RenderRequest, RenderResult
from .rendering.handoff_docx import PackagedV2DocxCore
from .rendering.pds_docx_renderer import ApprovedReport, PdsDocxRenderer
from .rendering.source_index_docx_renderer import SourceIndexDocxRenderer
from .report_markdown import (
    CanonicalMarkdownTable,
    CanonicalReportContent,
    compose_canonical_markdown,
)
from .research.knowledge_context import KnowledgeContextBuilder
from .research.project_evidence import ProjectEvidenceIndex, project_evidence_locator
from .review_lifecycle import (
    DeferredMainDecision,
    request_module_revision,
    run_cross_review,
    run_final_review,
    run_module_review,
    verify_cross_owner_barrier,
)
from .revision_diff import build_revision_diff
from .retention import ReportingRetentionPlanner
from .session_summary import SessionSummaryStore
from .scheduling import (
    AdaptiveTaskScheduler,
    SchedulingCandidate,
    TaskTimingHistory,
)
from .source_ledger import SourceLedger
from .special_topics import load_special_topic_plan
from .taxonomy import REPORT_TAXONOMY, compose_module_markdown, resolve_submodule
from .versions import ReportVersion, ReportVersionStore, SkillProvenance

if TYPE_CHECKING:
    from .service import ReportingService


TEMPLATE_SKILL_ROOT = Path("Work/report-template-writing")
TEMPLATE_SKILL_SOURCE = TEMPLATE_SKILL_ROOT / "source.json"


class FullReportCheckpoint(StrictModel):
    """Durable full-report state; every reference is run-scoped and validated on restore."""

    version: int = 3
    workflow_id: str = ""
    run_id: str
    activity: str = "unknown"
    status: str = "unknown"
    preparation_refs: dict[str, str] = Field(default_factory=dict)
    preparation_sha256: dict[str, str] = Field(default_factory=dict)
    input_snapshot_ref: str | None = None
    input_snapshot_digest: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    completed_modules: list[str] = Field(default_factory=list)
    specialist_modules: list[str] = Field(default_factory=list)
    module_dispatch_ref: str | None = None
    module_knowledge_refs: dict[str, str] = Field(default_factory=dict)
    module_lane_barrier_ref: str | None = None
    cross_owner_barrier_ref: str | None = None
    quality_context_ref: str | None = None
    module_review_completion_refs: dict[str, str] = Field(default_factory=dict)
    cross_review_completion_ref: str | None = None
    cross_decision_pack_ref: str | None = None
    cross_decision_pack_sha256: str | None = Field(
        default=None, pattern=r"^[0-9a-f]{64}$"
    )
    chief_candidate_ref: str | None = None
    chief_editor_input_ref: str | None = None
    chief_editor_envelope_ref: str | None = None
    chief_editor_completion_ref: str | None = None
    final_review_restart_round: int | None = Field(default=None, ge=1)
    final_review_completion_ref: str | None = None
    final_audit_snapshot_ref: str | None = None
    delivery_completion_ref: str | None = None
    report_state_ref: str | None = None
    cross_review_completed: bool = False
    final_review_completed: bool = False
    error: str | None = None
    budget: dict | None = None
    pending_cost_boundary_id: str | None = None
    project_write_lease_ref: str | None = None
    project_write_lease_epoch: int | None = Field(default=None, ge=1)


class AgentWorkflowError(RuntimeError):
    pass


class AgentWorkflowBlocked(AgentWorkflowError):
    def __init__(
        self, agent_id: str, task_id: str, reason: str, artifact_refs: list[str] | None = None
    ):
        self.agent_id = agent_id
        self.task_id = task_id
        self.reason = reason
        self.artifact_refs = list(artifact_refs or ())
        super().__init__(f"{agent_id} blocked on {task_id}: {reason}")


class ReportingNeedsDecisionError(RuntimeError):
    """The lead Agent decided that safe autonomous progress is no longer useful."""

    def __init__(self, reason: str, *, keep_agents_alive: bool = True):
        self.keep_agents_alive = keep_agents_alive
        super().__init__(reason)


class ReportingRunBudget:
    """Run telemetry plus an optional safe stage-boundary decision policy."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        max_attempts: int,
        max_tokens: int,
        mode: CostControlMode = "observe",
    ):
        self.ledger = UsageLedger(workspace, run_id)
        self.max_attempts = max_attempts
        self.max_tokens = max_tokens
        self.mode = mode
        self.controller = StageCostController(
            workspace,
            run_id,
            mode=mode,
            attempt_window=max_attempts,
            token_window=max_tokens,
        )
        self._issued_attempts = len(self.ledger.rows())
        self._active_dispatches = 0
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, Any]:
        summary = self.ledger.summarize(group_by="stage")
        totals = summary["totals"]
        return {
            "provider_attempts": totals["provider_attempts"],
            "total_tokens": totals["total_tokens"],
            "input_tokens": totals["input_tokens"],
            "cached_input_tokens": totals["cached_input_tokens"],
            "cache_write_input_tokens": totals[
                "cache_write_input_tokens"
            ],
            "uncached_input_tokens": totals["uncached_input_tokens"],
            "output_tokens": totals["output_tokens"],
            "message_chars": totals["message_chars"],
            "tool_schema_chars": totals["tool_schema_chars"],
            "repeated_message_chars": totals["repeated_message_chars"],
            "repeated_tool_schema_chars": totals["repeated_tool_schema_chars"],
            "duration_ms": totals["duration_ms"],
            "pricing_status": totals["pricing_status"],
            "pricing_table_version": totals["pricing_table_version"],
            "pricing_currency": totals["pricing_currency"],
            "estimated_cost": totals["estimated_cost"],
            "by_stage": summary["groups"],
            "max_provider_attempts": self.max_attempts,
            "max_total_tokens": self.max_tokens,
            "issued_provider_attempts": self._issued_attempts,
            "active_dispatches": self._active_dispatches,
            "limits_enforced": False,
            "hard_request_limits_enforced": False,
            "boundary_policy_active": self.mode
            in {"warn", "pause_at_boundary"},
            "cost_control": self.controller.snapshot(),
        }

    def resolve_resume(self) -> bool:
        return self.controller.resolve_resume(self.snapshot())

    def prepare_boundary(
        self, completed_stage: str, next_stage: str | None
    ) -> dict[str, Any]:
        return self.controller.prepare_boundary(
            completed_stage=completed_stage,
            next_stage=next_stage,
            usage=self.snapshot(),
        )

    def pending_boundary(self) -> dict[str, Any] | None:
        return self.controller.pending_boundary()

    def confirm_boundary_checkpoint(self, boundary_id: str) -> dict[str, Any]:
        return self.controller.confirm_boundary_checkpoint(boundary_id)

    def discard_uncommitted_boundary(
        self,
        boundary_id: str,
        *,
        reason: str,
    ) -> bool:
        return self.controller.discard_uncommitted_boundary(
            boundary_id,
            reason=reason,
        )

    def evaluate_boundary(
        self, completed_stage: str, next_stage: str | None
    ) -> dict[str, Any]:
        usage = self.snapshot()
        decision = self.controller.evaluate(
            completed_stage=completed_stage,
            next_stage=next_stage,
            usage=usage,
        )
        if decision["pause"]:
            raise ReportingNeedsDecisionError(
                "成本控制已在安全阶段边界暂停："
                f"已完成 {completed_stage}，下一阶段 {next_stage or '无'}；"
                f"当前 Provider 调用 {decision['provider_attempts']} 次、"
                f"Token {decision['total_tokens']}。"
                "本轮没有中断 Agent 提交，checkpoint 已保存。"
                "请由 Main 选择继续同一 run、保持等待或明确取消；"
                "成本边界不得删减证据、模块、上下文或交付内容；"
                f"决策状态见 {decision['state_ref']}。"
            )
        return decision

    async def acquire(self, agent_id: str) -> None:
        async with self._lock:
            self._active_dispatches += 1

    async def acquire_provider_attempt(self, agent_id: str, task_id: str) -> None:
        """Reserve one real provider request before it leaves the process."""

        async with self._lock:
            usage = self.snapshot()
            self._issued_attempts = max(
                self._issued_attempts,
                usage["provider_attempts"],
            )
            self._issued_attempts += 1

    async def release(self) -> None:
        async with self._lock:
            self._active_dispatches = max(0, self._active_dispatches - 1)


class ScopeExpansionNeededError(RuntimeError):
    def __init__(self, request: ScopeExpansionRequest):
        self.request = request
        super().__init__(request.reason)


class ReportWorkflowRunner:
    """Execute declared dependencies while leaving review decisions to the lead Agent."""

    def __init__(self, service: "ReportingService", agent_runner: ReportingAgentRunner):
        self.service = service
        self.agent_runner = agent_runner
        self.agents = service.agents
        required = {
            "main-agent",
            "template-distiller",
            "evidence-auditor",
            "cross-module-reviewer",
            "chief-editor",
            "chief-editor-auditor",
            *(f"module-{module_id}-specialist" for module_id in REPORT_MODULE_IDS),
        }
        missing = sorted(required - set(self.agents))
        if missing:
            raise AgentWorkflowError(f"reporting Agent identities are missing: {missing}")
        self._budget: ReportingRunBudget | None = None
        self._main_exception_lock = asyncio.Lock()

    def _raise_if_cancel_requested(self, run_id: str) -> None:
        """Stop before a new task/stage when the durable job requested cancel."""

        events = LocalEventStore(self.service.workspace, run_id).read()
        last_cancel = max(
            (
                event.sequence
                for event in events
                if event.event_type == "CancelRequested"
            ),
            default=0,
        )
        last_queued = max(
            (event.sequence for event in events if event.event_type == "RunQueued"),
            default=0,
        )
        if last_cancel > last_queued:
            raise asyncio.CancelledError("durable run cancellation requested")

    async def _activate_cost_resume(self, state: dict) -> None:
        if self._budget is None or not state.get("resume"):
            return
        if self._budget.resolve_resume():
            await self.service._notice(
                "已按用户的同 run 恢复指令解除上一成本边界暂停；"
                "新的成本窗口从当前 checkpoint 起计算。"
            )

    async def _recover_pending_cost_boundary(
        self,
        checkpoint: dict | None,
    ) -> None:
        """Evaluate a boundary left between durable intent and policy decision."""

        if self._budget is None:
            return
        pending = self._budget.pending_boundary()
        if pending is None:
            return
        boundary_id = str(pending["boundary_id"])
        if (
            not isinstance(checkpoint, dict)
            or checkpoint.get("pending_cost_boundary_id") != boundary_id
        ):
            self._budget.discard_uncommitted_boundary(
                boundary_id,
                reason="workflow_checkpoint_does_not_contain_boundary_id",
            )
            return
        self._budget.confirm_boundary_checkpoint(boundary_id)
        await self._cost_boundary(
            str(pending["completed_stage"]),
            (
                str(pending["next_stage"])
                if pending.get("next_stage") is not None
                else None
            ),
        )

    async def _cost_boundary(
        self,
        completed_stage: str,
        next_stage: str | None,
    ) -> None:
        if self._budget is None:
            return
        decision = self._budget.evaluate_boundary(completed_stage, next_stage)
        if decision["action"] == "warn":
            await self.service._notice(
                "成本观察提醒：已完成 "
                f"{completed_stage}，当前 Provider 调用 {decision['provider_attempts']} 次、"
                f"Token {decision['total_tokens']}；流程将在 checkpoint 后继续 "
                f"{next_stage or '收尾'}。"
            )

    async def _checkpoint_then_cost_boundary(
        self,
        state: dict,
        activity: str,
        status: str,
        completed_stage: str,
        next_stage: str | None,
        *,
        checkpoint_kind: str = "full",
    ) -> None:
        """Persist boundary intent, then checkpoint, then evaluate the policy."""

        if self._budget is not None and self._budget.mode == "observe":
            checkpoint = {
                "full": self._checkpoint,
                "aggregate": self._aggregate_checkpoint,
                "revision": self._revision_checkpoint,
            }.get(checkpoint_kind)
            if checkpoint is None:
                raise ValueError(f"unsupported checkpoint kind: {checkpoint_kind}")
            checkpoint(state, activity, status)
            self._raise_if_cancel_requested(state["run_id"])
            return

        prepared = None
        if self._budget is not None:
            prepared = self._budget.prepare_boundary(
                completed_stage,
                next_stage,
            )
            state["pending_cost_boundary_id"] = prepared["boundary_id"]
        checkpoint = {
            "full": self._checkpoint,
            "aggregate": self._aggregate_checkpoint,
            "revision": self._revision_checkpoint,
        }.get(checkpoint_kind)
        if checkpoint is None:
            raise ValueError(f"unsupported checkpoint kind: {checkpoint_kind}")
        checkpoint(state, activity, status)
        if prepared is not None:
            self._budget.confirm_boundary_checkpoint(
                str(prepared["boundary_id"])
            )
        try:
            await self._cost_boundary(completed_stage, next_stage)
        finally:
            if self._budget is not None and self._budget.pending_boundary() is None:
                state.pop("pending_cost_boundary_id", None)
        if self._budget is not None:
            checkpoint(state, activity, status)

    async def _agent(
        self,
        agent_id: str,
        envelope: TaskEnvelope,
        artifacts: list[str],
        workflow_id: str,
        *,
        session_key: str | None = None,
    ):
        self._raise_if_cancel_requested(envelope.run_id)
        if self._budget is not None:
            await self._budget.acquire(agent_id)
        try:
            board_task = self.service.task_board.create_task(
                source="report-workflow",
                target=agent_id,
                brief=f"reporting:{workflow_id}:{agent_id}:{envelope.task_id}",
                session_id=workflow_id,
            )
            self.service.task_board.start_task(
                board_task.task_id,
                target_agent=agent_id,
                session_id=workflow_id,
            )
            try:
                result = await self.agent_runner.run(
                    self.agents[agent_id],
                    envelope,
                    artifacts,
                    workflow_id=workflow_id,
                    session_key=session_key,
                )
            except asyncio.CancelledError:
                self.service.task_board.cancel_task(
                    board_task.task_id,
                    target_agent=agent_id,
                    session_id=workflow_id,
                )
                raise
            except BaseException:
                self.service.task_board.fail_task(
                    board_task.task_id,
                    target_agent=agent_id,
                    session_id=workflow_id,
                )
                raise
            if result.status is AgentRunStatus.COMPLETED:
                self.service.task_board.complete_task(
                    board_task.task_id,
                    target_agent=agent_id,
                    session_id=workflow_id,
                )
            elif result.status is AgentRunStatus.BLOCKED:
                self.service.task_board.block_task(
                    board_task.task_id,
                    target_agent=agent_id,
                    session_id=workflow_id,
                )
            else:
                self.service.task_board.fail_task(
                    board_task.task_id,
                    target_agent=agent_id,
                    session_id=workflow_id,
                )
            if result.status is AgentRunStatus.BLOCKED:
                raise AgentWorkflowBlocked(
                    agent_id,
                    envelope.task_id,
                    result.reason or "no reason",
                    [f"Work/runs/{envelope.run_id}/results/{envelope.task_id}.json"],
                )
            if result.status is AgentRunStatus.INCOMPLETE:
                result_ref = f"Work/runs/{envelope.run_id}/results/{envelope.task_id}.json"
                returned = (result.raw_output or "").strip()
                if len(returned) > 1200:
                    returned = returned[:1200] + "…"
                raise AgentWorkflowError(
                    f"{agent_id} returned without a typed submission; "
                    f"result_ref={result_ref}; returned_content={returned or '(empty)'}"
                )
            if result.status is not AgentRunStatus.COMPLETED:
                raise AgentWorkflowError(
                    f"{agent_id} ended as {result.status.value}: {result.reason or 'no reason'}"
                )
            return result.payload
        finally:
            if self._budget is not None:
                await self._budget.release()

    def _load_template_skill(self, state: dict) -> bool:
        root = TEMPLATE_SKILL_ROOT
        refs = {
            "core": root / "SKILL.md",
            "analysis": root / "references/analysis-language.md",
            "synthesis": root / "references/synthesis.md",
            "visual": root / "references/visual-organization.md",
            "rubric": root / "references/quality-rubric.md",
        }
        boundary_ref = TEMPLATE_SKILL_ROOT / "boundary.json"
        required = [*refs.values(), boundary_ref, TEMPLATE_SKILL_SOURCE]
        missing = [
            path.as_posix()
            for path in required
            if not (self.service.workspace / path).is_file()
        ]
        if missing:
            raise AgentWorkflowError(
                "固定模板写作 Skill 与当前边界契约不兼容，缺少文件："
                f"{', '.join(missing)}；请先单独运行 "
                "operation=distill_template_skill 更新固定 Skill"
            )
        try:
            boundary = TemplateSkillBoundaryManifest.model_validate_json(
                (self.service.workspace / boundary_ref).read_text(encoding="utf-8")
            )
            source_payload = json.loads(
                (self.service.workspace / TEMPLATE_SKILL_SOURCE).read_text(
                    encoding="utf-8"
                )
            )
            if not isinstance(source_payload, dict):
                raise ValueError("source.json must contain one JSON object")
            expected_hashes = {
                path.relative_to(TEMPLATE_SKILL_ROOT).as_posix(): self._sha256(
                    self.service.workspace / path
                )
                for path in [*refs.values(), boundary_ref]
            }
        except (OSError, ValueError, AttributeError) as exc:
            raise AgentWorkflowError(
                "固定模板写作 Skill 的 boundary.json 或 source.json 无法解析；"
                "请先单独运行 operation=distill_template_skill 更新固定 Skill"
            ) from exc
        mismatched = [
            field
            for field, actual, expected in (
                (
                    "boundary_policy_version",
                    source_payload.get("boundary_policy_version"),
                    boundary.policy_version,
                ),
                (
                    "boundary_ref",
                    source_payload.get("boundary_ref"),
                    boundary_ref.as_posix(),
                ),
                (
                    "artifact_sha256",
                    source_payload.get("artifact_sha256"),
                    expected_hashes,
                ),
            )
            if actual != expected
        ]
        if mismatched:
            raise AgentWorkflowError(
                "固定模板写作 Skill 的 source.json 与当前边界契约或产物哈希不一致："
                f"{', '.join(mismatched)}；请先单独运行 "
                "operation=distill_template_skill 更新固定 Skill"
            )
        template_skill_text = {
            key: (self.service.workspace / path).read_text(encoding="utf-8")
            for key, path in refs.items()
        }
        poisoned = [
            refs[key].as_posix()
            for key, text in template_skill_text.items()
            if "<persisted_result_part" in text.casefold()
        ]
        if poisoned:
            raise AgentWorkflowError(
                "固定模板写作 Skill 含有退休的内部历史令牌："
                f"{', '.join(poisoned)}；必须恢复完整正文或重新蒸馏，禁止把该标记"
                "继续注入报告 Agent"
            )
        state["template_skill_refs"] = {key: path.as_posix() for key, path in refs.items()}
        state["template_skill_text"] = template_skill_text
        state["template_skill_boundary"] = boundary
        return True

    def _require_template_skill(self, state: dict) -> None:
        self._load_template_skill(state)

    def _materialize_template_skill(self, state: dict, submission: TemplateSkillSubmission) -> None:
        root = TEMPLATE_SKILL_ROOT
        files = {
            "SKILL.md": submission.skill_markdown,
            "references/analysis-language.md": submission.analysis_language_reference,
            "references/synthesis.md": submission.synthesis_reference,
            "references/visual-organization.md": submission.visual_organization_reference,
            "references/quality-rubric.md": submission.quality_rubric,
        }
        for relative, content in files.items():
            self.service.store.write_text((root / relative).as_posix(), content.strip() + "\n")
        self.service.store.write_json(
            (root / "boundary.json").as_posix(),
            submission.boundary_manifest.model_dump(mode="json"),
        )
        if not all(
            (self.service.workspace / root / relative).is_file()
            for relative in (*files, "boundary.json")
        ):
            raise AgentWorkflowError("Template Distiller did not materialize the complete Skill")

    def _module_author_inline_context(self, state: dict, module_id: str) -> str:
        """Build current authoring context instead of replaying stale dispatch text."""

        knowledge_ref = state["module_knowledge_refs"][module_id]
        knowledge_path = (self.service.workspace / knowledge_ref).resolve()
        if (
            not knowledge_path.is_relative_to(self.service.workspace)
            or not knowledge_path.is_file()
        ):
            raise AgentWorkflowError(
                f"module {module_id} knowledge is not a readable workspace artifact"
            )
        return (
            self._domain_knowledge_context(
                knowledge_path.read_text(encoding="utf-8"),
                knowledge_ref,
            )
            + "\n\n"
            + self._role_skill_context(state, "module-author")
        )

    async def _distill_template_skill(self, state: dict, workflow_id: str) -> None:
        """Let Template Distiller refresh the fixed project writing Skill."""
        selected, source = self.service.resolve_skill_distillation_template(
            state["run_id"]
        )
        snapshot = self.service.workspace / (
            f"Work/runs/{state['run_id']}/templates/template-for-skill.docx"
        )
        snapshot, snapshot_sha256, snapshot_blob_ref = self.service.snapshot_content(
            selected,
            snapshot,
        )
        snapshot_ref = snapshot.relative_to(self.service.workspace).as_posix()
        distillation_input = TemplateDistillationInput(
            run_id=state["run_id"],
            template_ref=snapshot_ref,
            inspect_max_chars=100000,
            required_part_ids=["skill", "analysis", "synthesis", "visual", "rubric"],
        )
        distillation_input_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/context/template-distillation-input.json",
            distillation_input.model_dump(mode="json"),
        )
        distillation_input_ref = distillation_input_path.relative_to(
            self.service.workspace
        ).as_posix()
        envelope = TaskEnvelope(
            task_id="template-skill-distillation",
            run_id=state["run_id"],
            agent_id="template-distiller",
            objective=(
                "从完整报告模板中蒸馏可迁移的咨询报告写作与推理方法，形成供本次"
                "报告全流程复用的 report-template-writing Skill。产物必须指导后续"
                "角色如何分析、综合、提出行动建议并组织图证，而不是复述模板目录。"
            ),
            input_refs=[distillation_input_ref, snapshot_ref],
            constraints=[
                "第一步且只调用一次 inspect_document(path=input contract 的 template_ref, max_chars=inspect_max_chars)；工具会在本任务中完整返回正文和版式结构，之后禁止再次读取或查找模板",
                "读取后先在内部对照至少三组正文样本，识别观察→证据限定→判断→原因→影响→建议的推进方式；不得输出思考过程",
                "唯一成功的结束方式是调用 submit_result 提交 template_skill_submission；不得用‘现在开始分析’、摘要或计划代替工具提交",
                "skill_markdown 必须是可直接使用的 SKILL.md：仅含 name 与 description 两项 frontmatter；正文写核心工作流、证据边界、综合方法、按需读取 reference 的明确条件",
                "skill_markdown 必须直接链接 references/analysis-language.md、references/synthesis.md、references/visual-organization.md、references/quality-rubric.md，不能创建更深层引用",
                "analysis_language_reference 要提炼分析语句的功能、句群推进、谨慎程度和正反例；不是词频、套话清单或原文摘抄",
                "synthesis_reference 要说明如何跨章节合并重复发现、建立共同原因与风险链、重组结论、形成有责任人/时序/验收依据的行动包",
                "visual_organization_reference 要说明图片和表格在论证中的功能、放置位置、正文引导、图注、交叉引用及禁止的装饰性用法",
                "quality_rubric 要逐项定义可观察的通过标准、失败表现和修改动作，覆盖分析深度、结论组织、建议闭环、章节联动、图证叙事与事实边界",
                "模板中用于教模型如何形成目标正文、综合段落、表格或图证叙事的结构化输出样例必须保留在对应 Skill reference 中；先用占位符去除项目事实，再写成正例、反例或输出骨架，不另建 Output Profile",
                "这些 Skill 样例描述报告内容应如何组织，不得重复 submit_result 的 JSON 字段样例；机器提交形状只服从当前任务 submission schema",
                "提交 boundary_manifest：transferred_categories 必须精确等于 input 的 allowed_transfer_categories，excluded_categories 必须精确等于 required_exclusion_categories；四类可复用方法包含其去事实化结构样例",
                "只迁移写作能力，不复制模板项目事实、具体数值、客户名称或原结论",
                "专家优化版只在本任务中作为一次性 Skill 蒸馏源；不得把其中的具体问题、风险判断、分析结论、建议内容、证据编号或项目措辞写入任何 Skill 文件",
                "不得迁移专业机理、标准名称、适用条件或带单位阈值；它们属于 Knowledge，不属于模板 Skill",
                "禁止把五份长文本直接塞入 submit_result：只用 write_result_part 逐项保存 skill、analysis、synthesis、visual、rubric 的完整内容；先用 list_result_parts 确认状态，ready 项不得重写；最终 submit_result 的对应字段只提交 artifact_refs",
            ],
            allowed_outputs=["template_skill_submission"],
            allowed_tools=[
                "inspect_document",
                "write_result_part",
                "list_result_parts",
                "submit_result",
                "report_blocked",
            ],
            input_contract_kind="template_distillation_input",
            input_contract_ref=distillation_input_ref,
        )
        payload = await self._agent(
            "template-distiller",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key="template-distillation",
        )
        if not isinstance(payload, TemplateSkillSubmission):
            raise AgentWorkflowError("template-distiller returned the wrong template Skill payload")
        self._materialize_template_skill(state, payload)
        self.service.store.write_json(
            TEMPLATE_SKILL_SOURCE.as_posix(),
            {
                "source": source,
                "template_ref": snapshot_ref,
                "template_sha256": snapshot_sha256,
                "template_blob_ref": snapshot_blob_ref.as_posix(),
                "inspection_ref": (f"Work/runs/{state['run_id']}/context/template-inspection.json"),
                "producer": "template-distiller",
                "task_id": envelope.task_id,
                "skill_root": TEMPLATE_SKILL_ROOT.as_posix(),
                "boundary_policy_version": payload.boundary_manifest.policy_version,
                "boundary_ref": (TEMPLATE_SKILL_ROOT / "boundary.json").as_posix(),
                "boundary_sha256": hashlib.sha256(
                    (
                        self.service.workspace
                        / TEMPLATE_SKILL_ROOT
                        / "boundary.json"
                    ).read_bytes()
                ).hexdigest(),
                "artifact_sha256": {
                    path.relative_to(TEMPLATE_SKILL_ROOT).as_posix(): self._sha256(
                        self.service.workspace / path
                    )
                    for path in (
                        TEMPLATE_SKILL_ROOT / "SKILL.md",
                        TEMPLATE_SKILL_ROOT / "references/analysis-language.md",
                        TEMPLATE_SKILL_ROOT / "references/synthesis.md",
                        TEMPLATE_SKILL_ROOT / "references/visual-organization.md",
                        TEMPLATE_SKILL_ROOT / "references/quality-rubric.md",
                        TEMPLATE_SKILL_ROOT / "boundary.json",
                    )
                },
            },
        )
        self._require_template_skill(state)

    async def distill_template_skill(self, state: dict) -> None:
        """Run template distillation as a standalone action with fixed outputs."""

        run_id = state["run_id"]
        request = state["request"]
        workflow_id = f"template-skill-distillation:{run_id}"
        self._budget = ReportingRunBudget(
            self.service.workspace,
            run_id,
            request.max_provider_attempts,
            request.max_total_tokens,
            request.cost_control_mode,
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "template-skill-distillation"
        recovering_cost_boundary = True
        try:
            await self._activate_cost_resume(state)
            recovering_cost_boundary = False
            self._checkpoint(state, activity, "in_progress")
            await self.service._notice(
                "正在单独蒸馏报告模板；本次只更新固定模板写作 Skill，不启动报告写作。"
            )
            await self._distill_template_skill(state, workflow_id)
            self._checkpoint(state, activity, "completed")
            state["output_artifacts"] = [
                OutputArtifact(kind="skill", path=TEMPLATE_SKILL_ROOT / relative)
                for relative in (
                    "SKILL.md",
                    "references/analysis-language.md",
                    "references/synthesis.md",
                    "references/visual-organization.md",
                    "references/quality-rubric.md",
                    "boundary.json",
                    "source.json",
                )
            ]
            await self.service._notice(
                "模板写作 Skill 已更新至固定路径 Work/report-template-writing。"
            )
        except asyncio.CancelledError:
            if not recovering_cost_boundary:
                self._checkpoint(
                    state,
                    activity,
                    "cancelled",
                    "interrupted by user",
                )
            raise
        except Exception as exc:
            if not recovering_cost_boundary:
                self._checkpoint(state, activity, "failed", str(exc))
            raise
        finally:
            await self.agent_runner.close_workflow(workflow_id)

    @staticmethod
    def _template_skill_context(state: dict, *parts: str) -> str:
        texts = state.get("template_skill_text", {})
        return "\n\n".join(
            (
                f'<template_skill_part name="{part}" delivery_mode="inline">\n'
                f"{texts[part]}\n"
                "</template_skill_part>"
            )
            for part in parts
            if texts.get(part)
        )

    @staticmethod
    def _domain_knowledge_context(text: str, provenance_ref: str) -> str:
        """Label sourced domain Knowledge separately from methods and project Evidence."""

        return (
            f'<domain_knowledge delivery_mode="inline" provenance_ref="{provenance_ref}" '
            'project_fact_authority="false">\n'
            f"{text}\n"
            "</domain_knowledge>"
        )

    @classmethod
    def _role_skill_context(
        cls,
        state: dict,
        role: str,
        *,
        target_section_ids: set[str] | None = None,
    ) -> str:
        """Project only the distilled template parts owned by one workflow role."""

        role_parts = {
            "module-author": ("core", "analysis", "visual", "rubric"),
            "module-auditor": ("rubric",),
            "cross-reviewer": ("synthesis", "rubric"),
            "chief-editor": ("core", "analysis", "synthesis", "visual", "rubric"),
            "final-auditor": ("rubric",),
        }
        if role == "chief-revision":
            targets = set(target_section_ids or ())
            if not targets:
                raise ValueError("chief revision Skill routing requires target sections")
            selected = {"rubric"}
            if any(section_id.startswith("3.") for section_id in targets):
                selected.add("synthesis")
            if any(section_id.startswith(("1.", "4.")) for section_id in targets):
                selected.add("analysis")
            role_parts[role] = tuple(
                part
                for part in ("analysis", "synthesis", "rubric")
                if part in selected
            )
        elif role not in role_parts:
            raise ValueError(f"unknown reporting role skill: {role}")
        content = cls._template_skill_context(state, *role_parts[role])
        if not content:
            return ""
        parts = ",".join(role_parts[role])
        return (
            f'<role_skill role="{role}" template_parts="{parts}">\n'
            f"{content}\n"
            "</role_skill>"
        )

    @staticmethod
    def _user_supplement_constraints(
        state: dict,
        *,
        stage: str,
        target_ids: set[str] | None = None,
    ) -> list[str]:
        """Render only active, stage- and target-applicable typed supplements."""

        supplements = state["request"].user_supplements
        superseded = {
            superseded_id for supplement in supplements for superseded_id in supplement.supersedes
        }
        target_ids = target_ids or set()
        applicable = []
        for supplement in supplements:
            if supplement.id in superseded or stage not in supplement.stages:
                continue
            if supplement.scope != "run" and not set(supplement.target_ids).intersection(
                target_ids
            ):
                continue
            applicable.append(supplement)
        return [
            (
                f"用户补充 {item.id}（scope={item.scope}; "
                f"targets={','.join(item.target_ids) or 'run'}）是当前 run 的显式输入："
                f"{item.content}"
            )
            for item in applicable
        ]

    async def _prepare_module_authoring_mode(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run the only authoring architecture: isolated module-level lanes."""

        await self.service._notice(
            "专业模块进入完整模块 lane 并行：每条 lane 独立完成模块写作、"
            "Evidence Auditor 审计、定向修订和原 Auditor 复核；"
            "所有目标模块终态后才可进入 Cross。"
        )
        await self._run_module_lanes(requested_modules, state, workflow_id)

    async def run(self, state: dict) -> None:
        run_id = state["run_id"]
        workflow_id = f"full-power-distribution-report:{run_id}"
        request = state["request"]
        resume_checkpoint: dict | None = None
        if state.get("resume"):
            checkpoint_path = self.service.workspace / f"Work/runs/{run_id}/workflow-state.json"
            if checkpoint_path.is_file():
                resume_checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        self._budget = ReportingRunBudget(
            self.service.workspace,
            run_id,
            request.max_provider_attempts,
            request.max_total_tokens,
            request.cost_control_mode,
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "preparation"
        suspended_for_user = False
        recovering_cost_boundary = True
        try:
            await self._activate_cost_resume(state)
            await self._recover_pending_cost_boundary(resume_checkpoint)
            recovering_cost_boundary = False
            await self.service._notice("正在整理项目资料并建立可追溯证据入口。")
            await self._prepare(state)
            if state.get("resume"):
                # Restore every durable paid-work reference before any new
                # checkpoint or cost-boundary decision can overwrite the prior
                # checkpoint.  Preparation is restored first so resume
                # validation has the exact current-run ledgers available.
                self._restore_resume_state(state, resume_checkpoint)
            readiness = EvidenceReadinessPolicy.evaluate(state["request"], state["coverage_matrix"])
            state["evidence_readiness"] = readiness
            if readiness.should_block:
                self._checkpoint(
                    state,
                    activity,
                    "blocked",
                    ", ".join(readiness.missing_evidence),
                )
                raise ReportingBlockedError(
                    readiness.missing_evidence,
                    readiness.affected_modules,
                )
            await self._checkpoint_then_cost_boundary(
                state,
                activity,
                "completed",
                "preparation",
                "dispatch",
            )
            activity = "dispatch"
            requested_modules = tuple(state["request"].target_modules)
            await self.service._notice(
                "正在从固定路径 Work/report-template-writing 读取模板写作 Skill。"
            )
            self._require_template_skill(state)
            if "module_dispatch" in state:
                await self.service._notice("已恢复同一 run 的任务计划和已完成模块。")
                dispatch = state["module_dispatch"]
            else:
                await self.service._notice(
                    "已读取固定模板写作 Skill，正在准备模块知识上下文并直接分配专业任务。"
                )
                dispatch = self._build_module_dispatch(state, requested_modules)
                await self.service._notice(
                    "固定模板写作 Skill 与模块知识上下文已加载，Main 已按固定模块契约直接分配专业任务。"
                )
            state["module_dispatch"] = dispatch
            self._write_handoff_contracts(state)
            await self._checkpoint_then_cost_boundary(
                state,
                activity,
                "completed",
                "dispatch",
                "module-work",
            )
            activity = "module-work"
            await self._prepare_module_authoring_mode(
                requested_modules,
                state,
                workflow_id,
            )
            if set(requested_modules) != set(REPORT_MODULE_IDS):
                self._checkpoint(state, activity, "completed")
                state["output_artifacts"] = [
                    OutputArtifact(
                        kind="module",
                        path=Path(f"Outputs/Modules/{module_id}.md"),
                        module_id=module_id,
                    )
                    for module_id in requested_modules
                ]
                self._checkpoint(state, "partial-delivery", "completed")
                await self.service._notice("目标模块已完成写作并通过独立模块审计。")
                return
            await self._checkpoint_then_cost_boundary(
                state,
                activity,
                "completed",
                "module-work",
                "cross-module-review",
            )
            activity = "cross-module-review"
            if "cross_review_completion_ref" not in state:
                await self.service._notice("五个模块均已通过各自独立审查，开始跨模块一致性审查。")
                await self._cross_review(state, workflow_id)
                await self._checkpoint_then_cost_boundary(
                    state,
                    activity,
                    "completed",
                    "cross-module-review",
                    "chief-edit",
                )
            else:
                await self.service._notice("已恢复本 run 完成的跨模块审查，直接进入总编。")
            if "final_review_completion_ref" not in state:
                if "chief_candidate_ref" not in state:
                    activity = "chief-edit"
                    await self.service._notice("跨模块审查通过，总编正在整合全文并保护来源语义。")
                    await self._chief_edit(state, workflow_id)
                    await self._checkpoint_then_cost_boundary(
                        state,
                        activity,
                        "completed",
                        "chief-edit",
                        "chief-editor-audit",
                    )
                else:
                    await self.service._notice(
                        "已恢复本 run 通过确定性校验的总编候选稿，直接继续独立全文审计。"
                    )
                activity = "chief-editor-audit"
                await self.service._notice("总编成稿已形成，正在进行独立全文质量与交付就绪审计。")
                await self._final_review_loop(
                    state,
                    workflow_id,
                    chief_envelope=state["chief_editor_envelope"],
                    chief_session_key=state["chief_editor_session_key"],
                    approved_module_text=state["approved_module_text"],
                    claims=[
                        claim
                        for module_id in REPORT_MODULE_IDS
                        for claim in state["module_submissions"][module_id].claims
                    ],
                )
                await self._checkpoint_then_cost_boundary(
                    state,
                    activity,
                    "completed",
                    "chief-editor-audit",
                    "delivery",
                )
            else:
                await self.service._notice("已恢复本 run 完成的总编成稿审计，直接进入确定性渲染。")
            activity = "delivery"
            if "delivery_completion_ref" not in state:
                await self.service._notice("正文与引用已批准，正在使用交接包渲染核心生成 DOCX。")
                self._deliver(state)
            else:
                await self.service._notice("已校验并恢复本 run 的完整交付结果。")
            self._checkpoint(state, activity, "completed")
        except ReportingBlockedError:
            raise
        except ReportingNeedsDecisionError as exc:
            suspended_for_user = exc.keep_agents_alive
            if not recovering_cost_boundary:
                self._checkpoint(
                    state,
                    activity,
                    "waiting_user" if suspended_for_user else "stopped_incomplete",
                    str(exc),
                )
            raise
        except asyncio.CancelledError:
            if not recovering_cost_boundary:
                self._checkpoint(
                    state,
                    activity,
                    "cancelled",
                    "interrupted by user",
                )
            raise
        except Exception as exc:
            if not recovering_cost_boundary:
                self._checkpoint(state, activity, "failed", str(exc))
            raise
        finally:
            if not suspended_for_user:
                await self.agent_runner.close_workflow(workflow_id)

    async def aggregate_existing(self, state: dict) -> None:
        """Create only the chief editor for five already-written module reports."""

        request = state["request"]
        run_id = state["run_id"]
        workflow_id = f"aggregate-existing-report:{run_id}"
        resume_checkpoint: dict | None = None
        if state.get("resume"):
            checkpoint_path = (
                self.service.workspace
                / f"Work/runs/{run_id}/workflow-state.json"
            )
            if checkpoint_path.is_file():
                resume_checkpoint = json.loads(
                    checkpoint_path.read_text(encoding="utf-8")
                )
        configured_refs = request.source_module_refs or {
            module_id: Path(f"Outputs/Modules/{module_id}.md") for module_id in REPORT_MODULE_IDS
        }
        module_refs: dict[str, str] = {}
        structured_modules: dict[str, ModuleSubmission] = {}
        markdown_modules: dict[str, str] = {}
        structured_sources = {}
        input_snapshot = RunInputSnapshotStore(
            self.service.workspace
        ).load(run_id)
        for module_id in REPORT_MODULE_IDS:
            relative = Path(configured_refs[module_id])
            frozen_relative = input_snapshot.resolve(relative)
            source = (self.service.workspace / frozen_relative).resolve()
            if not source.is_relative_to(self.service.workspace) or not source.is_file():
                raise FileNotFoundError(
                    f"existing module report does not exist: {relative.as_posix()}"
                )
            source_text = source.read_text(encoding="utf-8")
            if not source_text.strip():
                raise ValueError(f"existing module report is empty: {relative.as_posix()}")
            if relative.suffix.casefold() == ".json":
                raw = json.loads(source_text)
                if isinstance(raw, dict) and isinstance(raw.get("payload"), dict):
                    raw = raw["payload"]
                module = ModuleSubmission.model_validate(raw)
                if module.module_id != module_id:
                    raise ValueError(
                        f"structured module ref {relative.as_posix()} contains {module.module_id}, expected {module_id}"
                    )
                structured_modules[module_id] = module
                parts = relative.parts
                if len(parts) >= 3 and parts[:2] == ("Work", "runs"):
                    source_run_id = parts[2]
                    for record in SourceLedger(self.service.workspace, source_run_id).records:
                        structured_sources[record.id] = record
            else:
                markdown_modules[module_id] = source_text
            module_refs[module_id] = frozen_relative.as_posix()
        if structured_modules and set(structured_modules) != set(REPORT_MODULE_IDS):
            raise ValueError(
                "aggregate_existing must use five structured module JSON refs together; "
                "mixing Markdown and JSON would drop evidence bindings"
            )
        aggregate_validation_ref = f"Work/runs/{run_id}/reviews/aggregate-module-integrity.json"
        aggregate_subject_ref = f"Work/runs/{run_id}/context/aggregate-source-manifest.json"
        self.service.store.write_json(
            aggregate_subject_ref,
            {
                "source_format": ("structured_module" if structured_modules else "markdown"),
                "module_refs": module_refs,
            },
        )
        shallow_signals: list[str] = []
        try:
            if structured_modules:
                validate_module_markdown_consistency(structured_modules)
                shallow_signals = find_shallow_submodules(structured_modules)
            else:
                validate_existing_markdown_modules(markdown_modules)
        except ValueError as exc:
            self.service.store.write_json(
                aggregate_validation_ref,
                ValidationReport(
                    run_id=run_id,
                    subject_ref=aggregate_subject_ref,
                    validator="aggregate-module-integrity/v1",
                    check_ids=["aggregate.five_modules_and_fixed_sections"],
                    failures=[
                        ValidationFailure(
                            check_id="aggregate.five_modules_and_fixed_sections",
                            target_path="module_refs",
                            message=str(exc),
                        )
                    ],
                    passed=False,
                ).model_dump(mode="json"),
            )
            raise AgentWorkflowError(f"已有模块汇总输入完整性校验未通过：{exc}") from exc
        self.service.store.write_json(
            aggregate_validation_ref,
            ValidationReport(
                run_id=run_id,
                subject_ref=aggregate_subject_ref,
                validator="aggregate-module-integrity/v1",
                check_ids=["aggregate.five_modules_and_fixed_sections"],
                observations=[
                    f"possible_shallow_submodule:{submodule_id}" for submodule_id in shallow_signals
                ],
                passed=True,
            ).model_dump(mode="json"),
        )

        self._budget = ReportingRunBudget(
            self.service.workspace,
            run_id,
            request.max_provider_attempts,
            request.max_total_tokens,
            request.cost_control_mode,
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "aggregate-chief-edit"
        suspended_for_user = False
        recovering_cost_boundary = True
        try:
            await self._activate_cost_resume(state)
            await self._recover_pending_cost_boundary(resume_checkpoint)
            recovering_cost_boundary = False
            self._aggregate_checkpoint(state, activity, "in_progress")
            await self.service._notice(
                "已识别为已有分块报告汇总任务；读取固定模板写作 Skill 后启动总编和独立成稿审计，"
                "不启动模块专家、单模块审计或跨模块审查。"
            )
            self._require_template_skill(state)
            special_topic_plan = load_special_topic_plan(self.service.workspace)
            special_topic_input_refs: list[str] = []
            special_topic_inline_context = ""
            if special_topic_plan is not None:
                special_topic_plan_path = self.service.store.write_json(
                    f"Work/runs/{run_id}/context/special-topic-plan.json",
                    special_topic_plan.model_dump(mode="json"),
                )
                special_topic_knowledge = KnowledgeContextBuilder(
                    self.service.workspace, run_id
                ).build_special_topics(special_topic_plan)
                state["special_topic_plan"] = special_topic_plan
                state["special_topic_plan_ref"] = (
                    special_topic_plan_path.relative_to(self.service.workspace).as_posix()
                )
                state["special_topic_knowledge_ref"] = (
                    special_topic_knowledge.path.as_posix()
                )
                special_topic_input_refs.append(special_topic_knowledge.path.as_posix())
                special_topic_inline_context = "\n\n" + special_topic_knowledge.text
                await self.service._notice(
                    "已从 Inputs 的非空专项问题分析 Markdown 固化第四章标题与要求。"
                )
            else:
                await self.service._notice(
                    "Inputs 未提供非空专项问题分析 Markdown，本次报告省略第四章。"
                )
            if structured_modules:
                editor_input = AggregateEditorInput(
                    run_id=run_id,
                    source_format="structured_module",
                    approved_module_markers={
                        module_id: f"[[APPROVED_MODULE:{module_id}]]"
                        for module_id in REPORT_MODULE_IDS
                    },
                    special_topic_plan=special_topic_plan,
                    structured_modules={
                        module_id: module_content_view(module)
                        for module_id, module in structured_modules.items()
                    },
                )
                editor_input_path = self.service.store.write_json(
                    f"Work/runs/{run_id}/context/aggregate-editor-input.json",
                    editor_input.model_dump(mode="json"),
                )
                editor_input_refs = [
                    editor_input_path.relative_to(self.service.workspace).as_posix()
                ]
            else:
                editor_input = AggregateEditorInput(
                    run_id=run_id,
                    source_format="markdown",
                    approved_module_markers={
                        module_id: f"[[APPROVED_MODULE:{module_id}]]"
                        for module_id in REPORT_MODULE_IDS
                    },
                    special_topic_plan=special_topic_plan,
                    markdown_modules={
                        module_id: (self.service.workspace / module_refs[module_id]).read_text(
                            encoding="utf-8"
                        )
                        for module_id in REPORT_MODULE_IDS
                    },
                )
                editor_input_path = self.service.store.write_json(
                    f"Work/runs/{run_id}/context/aggregate-editor-input.json",
                    editor_input.model_dump(mode="json"),
                )
                editor_input_refs = [
                    editor_input_path.relative_to(self.service.workspace).as_posix()
                ]
            envelope = TaskEnvelope(
                task_id="aggregate-existing",
                run_id=run_id,
                agent_id="chief-editor",
                objective=(
                    "仅整合五份已有分块报告，形成结构连贯的完整 Markdown；"
                    f"用户要求：{request.instruction}"
                ),
                input_refs=[
                    *editor_input_refs,
                    *special_topic_input_refs,
                ],
                constraints=[
                    "输入是已完成的分块报告；不得重新检索项目证据，必须完整保留原文并只按当前实际章节汇总",
                    "不得创造、删除或改变分块报告中的事实、数值、风险等级和建议语义",
                    "必须保留且仅汇总 2.1、2.2、2.3、2.4、2.5 五个模块",
                    "每个 module_narrative 必须包含对应 [[APPROVED_MODULE:2.x]] 标记，可在标记前后增加短过渡；不得重新输出或改写原文，工作流会确定性嵌回批准正文",
                    "aggregate-editor-input 已完整内联在 input_contract 中，是唯一模块内容入口；直接使用该内容，禁止调用 open_artifact/search_text 重读合同或原始模块文件",
                    "总编始终形成第一至第三章；只有 special_topic_plan 存在时才形成第四章 special_topic_analysis",
                    "五个 module_narratives 只提交精确 APPROVED_MODULE 标记，禁止为省 token 压缩批准正文",
                    "任何综合节都必须自足地给出归纳事实、综合判断和决策含义；章节号只能作为句末追溯，不得用‘详见第二章’‘见2.x’或模块编号清单代替汇总分析",
                    "regional_executive_summary 必须按真实区域或责任边界归纳重点、优先行动与验证状态；没有区域划分证据时必须明确边界，禁止编造区域名称",
                    "dimension_risk_analysis 必须逐一比较五个专业维度的主导风险和管理含义；data_gap_analysis 必须归并重复缺口并说明它影响哪些判断和补证优先级；improvement_action_plan 必须列出责任接口、行动、验收指标和剩余风险",
                    *(
                        [
                            "special_topic_analysis 必须严格按 special_topic_plan 的顺序输出全部且仅输出对应的 ### 4.n 子标题；逐节满足 Inputs 中的简要要求，并形成自足分析",
                            "专项分析可使用已内联的项目 Knowledge 和模型世界知识补充机理、方案权衡与行业实践；必须区分当前项目事实、可追溯参考和通用专业判断，禁止把通用知识写成客户事实",
                        ]
                        if special_topic_plan is not None
                        else [
                            "special_topic_plan 为空；禁止提交 special_topic_analysis，最终 Markdown 和 DOCX 必须完全省略第四章"
                        ]
                    ),
                    "Template Distiller 产出的固定模板写作 Skill 已在 inline_context 中提供；按其风格、叙述、思考和质量量表整合，不复制模板客户事实",
                    "可使用模型世界知识解释技术机制、方案权衡和行业实践；不得把通用知识写成当前项目事实",
                    (
                        "运行时自动保护结构化模块的引用绑定；总编只提交 schema 声明字段。"
                        "表格只声明 E-* evidence_ids，图片只能沿用输入资产"
                        if structured_modules
                        else "不得提交不存在的引用、表格或图片绑定"
                    ),
                    *request.execution_requirements,
                ],
                allowed_outputs=["edited_report_submission"],
                input_contract_kind="aggregate_editor_input",
                input_contract_ref=editor_input_refs[0],
                inline_context="\n\n".join(
                    text
                    for text in (
                        self._role_skill_context(state, "chief-editor"),
                        special_topic_inline_context,
                    )
                    if text
                ),
            )
            source_modules = {
                module_id: (
                    structured_modules[module_id].markdown
                    if module_id in structured_modules
                    else (self.service.workspace / module_refs[module_id]).read_text(
                        encoding="utf-8"
                    )
                )
                for module_id in REPORT_MODULE_IDS
            }
            ledger = None
            claims = (
                [
                    claim
                    for module_id in REPORT_MODULE_IDS
                    for claim in structured_modules[module_id].claims
                ]
                if structured_modules
                else []
            )
            payload = self._load_aggregate_chief_completion(
                state,
                envelope,
                source_modules,
                claims,
            )
            if payload is None:
                payload = await self._agent(
                    "chief-editor",
                    envelope,
                    envelope.input_refs,
                    workflow_id,
                    session_key="aggregate-existing",
                )
                if not isinstance(payload, EditedReportSubmission):
                    raise AgentWorkflowError(
                        "chief-editor returned the wrong payload type"
                    )
                if not structured_modules and (
                    payload.protected_claim_ids
                    or payload.tables
                    or payload.photo_ids
                ):
                    raise AgentWorkflowError(
                        "markdown aggregate submission bypassed its input/output contract; "
                        "unverified structured bindings were not accepted or rewritten"
                    )
                payload = expand_approved_module_markers(
                    payload,
                    source_modules,
                )
                state["editor_quality_observations"] = (
                    validate_aggregate_retention(payload, source_modules)
                )
                if claims:
                    validate_editor_protection(payload, claims)
                self._write_aggregate_chief_completion(
                    state,
                    envelope,
                    payload,
                )
            else:
                state["editor_quality_observations"] = (
                    validate_aggregate_retention(payload, source_modules)
                )
                await self.service._notice(
                    "已恢复本 run 经 hash 绑定的汇总总编候选稿，未重复调用 Chief。"
                )
            if structured_modules:
                ledger = ClaimLedger(
                    claims=claims,
                    sources=list(structured_sources.values()),
                )
                state["module_submissions"] = structured_modules
                self.service.store.write_json(
                    f"Work/runs/{run_id}/ledgers/claims.json",
                    ledger.model_dump(mode="json"),
                )
            state["edited_report"] = payload
            await self._checkpoint_then_cost_boundary(
                state,
                activity,
                "completed",
                "aggregate-chief-edit",
                "aggregate-chief-editor-audit",
                checkpoint_kind="aggregate",
            )
            activity = "aggregate-chief-editor-audit"
            if not self._restore_aggregate_final_completion(
                state,
                source_modules,
                claims,
            ):
                await self.service._notice(
                    "总编汇总稿已形成，正在进行独立全文质量与交付就绪审计。"
                )
                await self._final_review_loop(
                    state,
                    workflow_id,
                    chief_envelope=envelope,
                    chief_session_key="aggregate-existing",
                    approved_module_text=source_modules,
                    claims=claims,
                    aggregate_mode=True,
                )
            else:
                await self.service._notice(
                    "已恢复本 run 完成的汇总成稿审计，未重复调用审查 Agent。"
                )
            self._aggregate_checkpoint(state, activity, "completed")
            activity = "aggregate-markdown"
            payload = state["edited_report"]
            filename = request.output_filename or "配电安全专家咨询报告.docx"
            markdown_ref = Path("Outputs/Reports") / f"{Path(filename).stem}.md"
            source_index_ref = Path("Outputs/Reports/证据与来源索引.md")
            source_index_docx_ref = Path("Outputs/Reports/证据与来源索引.docx")
            markdown = self._canonical_markdown(payload)
            if ledger is not None:
                markdown = ledger.bind_citations(markdown)
            self._validate_final_report_structure(state, markdown, "aggregate-final")
            self.service.store.write_text(markdown_ref.as_posix(), markdown)
            source_index_markdown = (
                ledger.source_index_markdown(
                    evidence_items=state.get("evidence_items", []),
                    photo_assets=state.get("photo_assets", []),
                )
                if ledger is not None
                else (
                    "## 证据与来源索引\n\n"
                    "本次汇总输入为既有 Markdown 模块，未携带可验证的结构化 Claim/来源账本；"
                    "因此未生成脚注对应关系或 E-*/R-*/W-* 来源明细。\n"
                )
            )
            self.service.store.write_text(
                source_index_ref.as_posix(),
                source_index_markdown.rstrip() + "\n",
            )
            SourceIndexDocxRenderer.render(
                source_index_markdown.rstrip() + "\n",
                self.service.workspace / source_index_docx_ref,
            )
            state["aggregate_markdown_ref"] = markdown_ref
            state["source_index_ref"] = source_index_ref
            state["source_index_docx_ref"] = source_index_docx_ref
            state["output_artifacts"] = [
                OutputArtifact(kind="report", path=markdown_ref),
                OutputArtifact(kind="report", path=source_index_ref),
                OutputArtifact(kind="report", path=source_index_docx_ref),
            ]
            self.service.store.write_json(
                f"Work/runs/{run_id}/aggregation-handoff.json",
                {
                    "producer": "chief-editor-auditor",
                    "consumer": "docx-renderer",
                    "final_review_completion_ref": state["final_review_completion_ref"],
                    "input_module_refs": module_refs,
                    "structured_module_refs": (module_refs if structured_modules else {}),
                    "claim_ledger_ref": (
                        f"Work/runs/{run_id}/ledgers/claims.json" if structured_modules else None
                    ),
                    "table_count": len(payload.tables),
                    "photo_ids": payload.photo_ids,
                    "output_markdown_ref": markdown_ref.as_posix(),
                    "source_index_ref": source_index_ref.as_posix(),
                    "source_index_docx_ref": source_index_docx_ref.as_posix(),
                },
            )
            self._aggregate_checkpoint(state, activity, "completed")
        except ReportingNeedsDecisionError as exc:
            suspended_for_user = exc.keep_agents_alive
            if not recovering_cost_boundary:
                self._aggregate_checkpoint(
                    state,
                    activity,
                    "waiting_user" if suspended_for_user else "stopped_incomplete",
                    str(exc),
                )
            raise
        except asyncio.CancelledError:
            if not recovering_cost_boundary:
                self._aggregate_checkpoint(
                    state,
                    activity,
                    "cancelled",
                    "interrupted by user",
                )
            raise
        except Exception as exc:
            if not recovering_cost_boundary:
                self._aggregate_checkpoint(
                    state,
                    activity,
                    "failed",
                    str(exc),
                )
            raise
        finally:
            if not suspended_for_user:
                await self.agent_runner.close_workflow(workflow_id)

    async def run_revision(
        self,
        state: dict,
        request: RevisionRequest,
        baseline_edited: EditedReportSubmission,
    ) -> None:
        workflow_id = f"report-revision:{state['run_id']}"
        resume_checkpoint: dict | None = None
        if state.get("resume"):
            checkpoint_path = (
                self.service.workspace
                / f"Work/runs/{state['run_id']}/workflow-state.json"
            )
            if checkpoint_path.is_file():
                resume_checkpoint = json.loads(
                    checkpoint_path.read_text(encoding="utf-8")
                )
        self._budget = ReportingRunBudget(
            self.service.workspace,
            state["run_id"],
            request.max_provider_attempts,
            request.max_total_tokens,
            request.cost_control_mode,
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "revision-restore"
        suspended_for_user = False
        recovering_cost_boundary = True
        try:
            await self._activate_cost_resume(state)
            await self._recover_pending_cost_boundary(resume_checkpoint)
            recovering_cost_boundary = False
            await self.service._notice(
                f"正在从报告版本 {request.baseline_version_id} 恢复结构化状态并执行局部修订。"
            )
            self._require_template_skill(state)
            state["chief_editor_constraints"] = [
                "这是交付后局部修订：未获批准的模块正文必须逐字保持父版本内容",
                "只可更新输入合同授权的目标模块与固定综合章节字段",
            ]
            for module_id in REPORT_MODULE_IDS:
                submission = state["module_submissions"][module_id]
                self.service.store.write_json(
                    f"Work/runs/{state['run_id']}/modules/{module_id}-r{submission.revision}.json",
                    submission.model_dump(mode="json"),
                )
                self.service.store.write_text(
                    f"Outputs/Modules/{module_id}.md", submission.markdown
                )
            if state.get("resume"):
                self._restore_revision_resume_state(state)
            completed_revision_modules = set(state.get("completed_revision_modules", []))
            activity = "revision-module-work"
            pending_revision_modules = tuple(
                module_id
                for module_id in request.target_module_ids
                if module_id not in completed_revision_modules
            )
            for module_index, module_id in enumerate(pending_revision_modules):
                if module_id in completed_revision_modules:
                    continue
                await self._post_delivery_module_revision(module_id, state, request, workflow_id)
                completed_revision_modules.add(module_id)
                state["completed_revision_modules"] = sorted(completed_revision_modules)
                remaining = pending_revision_modules[module_index + 1 :]
                if remaining:
                    await self._checkpoint_then_cost_boundary(
                        state,
                        activity,
                        "in_progress",
                        f"revision-module-{module_id}",
                        f"revision-module-{remaining[0]}",
                        checkpoint_kind="revision",
                    )
                else:
                    self._revision_checkpoint(state, activity, "in_progress")
            activity = "revision-cross-review"
            await self._checkpoint_then_cost_boundary(
                state,
                "revision-module-work",
                "completed",
                "revision-module-work",
                "revision-cross-review",
                checkpoint_kind="revision",
            )
            if "cross_review_completion_ref" not in state:
                await self._cross_review(state, workflow_id)
                await self._checkpoint_then_cost_boundary(
                    state,
                    activity,
                    "completed",
                    "revision-cross-review",
                    "revision-chief-edit",
                    checkpoint_kind="revision",
                )
            else:
                await self.service._notice("已恢复本修订 run 完成的跨模块审查，直接进入总编。")
            if "final_review_completion_ref" not in state:
                activity = "revision-chief-edit"
                if "chief_candidate_ref" not in state:
                    await self._chief_edit(state, workflow_id)
                else:
                    await self.service._notice(
                        "已恢复本修订 run 经校验的总编候选稿，未重复调用 Chief。"
                    )
                unexpected_modules = sorted(
                    module_id
                    for module_id in REPORT_MODULE_IDS
                    if module_id not in request.target_module_ids
                    and state["edited_report"].module_narratives[module_id]
                    != baseline_edited.module_narratives[module_id]
                )
                if unexpected_modules:
                    self._raise_scope_expansion(
                        state,
                        request,
                        unexpected_module_ids=unexpected_modules,
                        reason="Chief Editor 的修订超出了已批准模块范围，需要用户确认扩大范围。",
                    )
                if not state.get("resume") or (
                    resume_checkpoint is None
                    or not resume_checkpoint.get(
                        "chief_editor_completion_ref"
                    )
                ):
                    await self._checkpoint_then_cost_boundary(
                        state,
                        activity,
                        "completed",
                        "revision-chief-edit",
                        "revision-chief-editor-audit",
                        checkpoint_kind="revision",
                    )
                activity = "revision-chief-editor-audit"
                await self._final_review_loop(
                    state,
                    workflow_id,
                    chief_envelope=state["chief_editor_envelope"],
                    chief_session_key=state["chief_editor_session_key"],
                    approved_module_text=state["approved_module_text"],
                    claims=[
                        claim
                        for module_id in REPORT_MODULE_IDS
                        for claim in state["module_submissions"][module_id].claims
                    ],
                )
                self._revision_checkpoint(state, activity, "completed")
            else:
                await self.service._notice(
                    "已恢复本修订 run 完成的总编成稿审计，直接进入重新交付。"
                )
            activity = "revision-delivery"
            self._deliver(state)
            self._revision_checkpoint(state, activity, "completed")
        except ReportingNeedsDecisionError as exc:
            suspended_for_user = exc.keep_agents_alive
            if not recovering_cost_boundary:
                self._revision_checkpoint(
                    state,
                    activity,
                    "waiting_user" if suspended_for_user else "stopped_incomplete",
                    str(exc),
                )
            raise
        except asyncio.CancelledError:
            if not recovering_cost_boundary:
                self._revision_checkpoint(
                    state,
                    activity,
                    "cancelled",
                    "interrupted by user",
                )
            raise
        except Exception as exc:
            if not recovering_cost_boundary:
                self._revision_checkpoint(
                    state,
                    activity,
                    "failed",
                    str(exc),
                )
            raise
        finally:
            if not suspended_for_user:
                await self.agent_runner.close_workflow(workflow_id)

    def _raise_scope_expansion(
        self,
        state: dict,
        request: RevisionRequest,
        *,
        unexpected_module_ids: list[str] | None = None,
        unexpected_submodule_ids: list[str] | None = None,
        unexpected_claim_ids: list[str] | None = None,
        reason: str,
    ) -> None:
        raise ScopeExpansionNeededError(
            ScopeExpansionRequest(
                request_id=f"scope-{uuid4().hex[:12]}",
                run_id=state["run_id"],
                baseline_version_id=request.baseline_version_id,
                requested_module_ids=list(request.target_module_ids),
                requested_submodule_ids=list(request.target_submodule_ids),
                unexpected_module_ids=unexpected_module_ids or [],
                unexpected_submodule_ids=unexpected_submodule_ids or [],
                unexpected_claim_ids=unexpected_claim_ids or [],
                reason=reason,
            )
        )

    async def _post_delivery_module_revision(
        self,
        module_id: str,
        state: dict,
        request: RevisionRequest,
        workflow_id: str,
    ) -> None:
        current: ModuleSubmission = state["module_submissions"][module_id]
        authorized_submodules = {
            submodule_id
            for submodule_id in request.target_submodule_ids
            if resolve_submodule(submodule_id).module_id == module_id
        }
        if not authorized_submodules and request.target_claim_ids:
            authorized_submodules = {
                claim.submodule_id
                for claim in current.claims
                if claim.id in request.target_claim_ids
            }
            missing_claims = sorted(
                set(request.target_claim_ids) - {claim.id for claim in current.claims}
            )
            if missing_claims:
                raise ValueError(
                    f"revision target claims do not exist in module {module_id}: {missing_claims}"
                )
        if not authorized_submodules:
            authorized_submodules = set(REPORT_TAXONOMY[module_id].submodules)

        revision = current.revision + 1
        requested_change = RequestedModuleChange(
            id=f"USER-{module_id}-R{revision}",
            instruction=request.feedback,
            target_submodule_ids=sorted(authorized_submodules),
        )
        payload, _ = await request_module_revision(
            self,
            state=state,
            workflow_id=workflow_id,
            subject=current,
            requested_changes=[requested_change],
        )
        ClaimLedger(
            claims=payload.claims,
            sources=SourceLedger(self.service.workspace, state["run_id"]).records,
        )
        diff = build_revision_diff(current, payload)
        changed_submodules = set(diff["changed_submodule_narratives"])
        claim_submodules = {
            claim.id: claim.submodule_id for claim in [*current.claims, *payload.claims]
        }
        unexpected_claims = sorted(
            claim_id
            for claim_id in diff["changed_claim_ids"]
            if claim_submodules.get(claim_id) not in authorized_submodules
            or (request.target_claim_ids and claim_id not in request.target_claim_ids)
        )
        unexpected_submodules = sorted(changed_submodules - authorized_submodules)
        if unexpected_submodules or unexpected_claims:
            self._raise_scope_expansion(
                state,
                request,
                unexpected_submodule_ids=unexpected_submodules,
                unexpected_claim_ids=unexpected_claims,
                reason="责任专家提出了超出已批准小节或 Claim 的变化，需要用户确认扩大范围。",
            )

        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/reviews/post-delivery-diff-{module_id}-r{revision}.json",
            diff,
        )
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
            payload.model_dump(mode="json"),
        )
        state.setdefault("specialist_submissions", {})[module_id] = payload
        payload = await self._module_review_loop(
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=authorized_submodules,
        )
        state["module_submissions"][module_id] = payload

    def _checkpoint(
        self, state: dict, activity: str, status: str, error: str | None = None
    ) -> None:
        """Persist recoverable workflow/output references without serializing sessions."""
        completed_modules = sorted(state.get("module_submissions", {}))
        checkpoint = FullReportCheckpoint(
            workflow_id=f"full-power-distribution-report:{state['run_id']}",
            run_id=state["run_id"],
            activity=activity,
            status=status,
            preparation_refs=dict(state.get("preparation_refs", {})),
            preparation_sha256=dict(state.get("preparation_sha256", {})),
            input_snapshot_ref=state.get("input_snapshot_ref"),
            input_snapshot_digest=state.get("input_snapshot_digest"),
            completed_modules=completed_modules,
            specialist_modules=sorted(state.get("specialist_submissions", {})),
            module_dispatch_ref=(
                f"Work/runs/{state['run_id']}/workflow/module-dispatch.json"
                if "module_dispatch" in state
                else None
            ),
            module_knowledge_refs=dict(state.get("module_knowledge_refs", {})),
            module_lane_barrier_ref=state.get("module_lane_barrier_ref"),
            cross_owner_barrier_ref=state.get("cross_owner_barrier_ref"),
            quality_context_ref=state.get("quality_context_ref"),
            report_state_ref=("Work/report-state.json" if "edited_report" in state else None),
            module_review_completion_refs=dict(state.get("module_review_completion_refs", {})),
            cross_review_completed="cross_review_completion_ref" in state,
            cross_review_completion_ref=state.get("cross_review_completion_ref"),
            cross_decision_pack_ref=state.get("cross_decision_pack_ref"),
            cross_decision_pack_sha256=state.get("cross_decision_pack_sha256"),
            chief_candidate_ref=state.get("chief_candidate_ref"),
            chief_editor_input_ref=state.get("chief_editor_input_ref"),
            chief_editor_envelope_ref=state.get("chief_editor_envelope_ref"),
            chief_editor_completion_ref=state.get(
                "chief_editor_completion_ref"
            ),
            final_review_restart_round=state.get("final_review_restart_round"),
            final_review_completed="final_review_completion_ref" in state,
            final_review_completion_ref=state.get("final_review_completion_ref"),
            final_audit_snapshot_ref=state.get("final_audit_snapshot_ref"),
            delivery_completion_ref=state.get("delivery_completion_ref"),
            error=error,
            budget=self._budget.snapshot() if self._budget is not None else None,
            pending_cost_boundary_id=state.get(
                "pending_cost_boundary_id"
            ),
            project_write_lease_ref=state.get("project_write_lease_ref"),
            project_write_lease_epoch=state.get("project_write_lease_epoch"),
        )
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            checkpoint.model_dump(mode="json"),
        )

    def _aggregate_checkpoint(
        self,
        state: dict,
        activity: str,
        status: str,
        error: str | None = None,
    ) -> None:
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            {
                "workflow_id": (f"aggregate-existing-report:{state['run_id']}"),
                "run_id": state["run_id"],
                "operation": "aggregate_existing",
                "activity": activity,
                "status": status,
                "report_state_ref": (
                    "Work/report-state.json" if "edited_report" in state else None
                ),
                "final_review_completed": "final_review_completion_ref" in state,
                "final_review_completion_ref": state.get("final_review_completion_ref"),
                "final_audit_snapshot_ref": state.get("final_audit_snapshot_ref"),
                "aggregate_markdown_ref": (
                    str(state["aggregate_markdown_ref"])
                    if state.get("aggregate_markdown_ref")
                    else None
                ),
                "error": error,
                "budget": (self._budget.snapshot() if self._budget is not None else None),
                "pending_cost_boundary_id": state.get(
                    "pending_cost_boundary_id"
                ),
                "project_write_lease_ref": state.get("project_write_lease_ref"),
                "project_write_lease_epoch": state.get(
                    "project_write_lease_epoch"
                ),
            },
        )

    def _load_aggregate_chief_completion(
        self,
        state: dict,
        envelope: TaskEnvelope,
        source_modules: dict[str, str],
        claims: list,
    ) -> EditedReportSubmission | None:
        """Restore one validated aggregate Chief result without another call."""

        run_id = state["run_id"]
        completion_ref = (
            f"Work/runs/{run_id}/aggregate-chief-completion.json"
        )
        completion_path = self.service.workspace / completion_ref
        if not state.get("resume") or not completion_path.is_file():
            return None
        try:
            completion = json.loads(
                completion_path.read_text(encoding="utf-8")
            )
            expected_refs = {
                "candidate": (
                    f"Work/runs/{run_id}/edited-revisions/"
                    "aggregate-chief-r0.json"
                ),
                "envelope": (
                    f"Work/runs/{run_id}/context/"
                    "aggregate-chief-envelope.json"
                ),
                "editor_input": str(envelope.input_contract_ref),
                "source_manifest": (
                    f"Work/runs/{run_id}/context/"
                    "aggregate-source-manifest.json"
                ),
            }
            if (
                completion.get("kind")
                != "aggregate_chief_completion"
                or completion.get("run_id") != run_id
                or completion.get("refs") != expected_refs
            ):
                return None
            hashes = completion.get("artifact_sha256")
            if not isinstance(hashes, dict):
                return None
            for name, ref in expected_refs.items():
                path = (self.service.workspace / ref).resolve()
                run_root = (
                    self.service.workspace / f"Work/runs/{run_id}"
                ).resolve()
                if (
                    not path.is_relative_to(run_root)
                    or not path.is_file()
                    or hashes.get(name) != self._sha256(path)
                ):
                    return None
            persisted_envelope = TaskEnvelope.model_validate_json(
                (
                    self.service.workspace / expected_refs["envelope"]
                ).read_text(encoding="utf-8")
            )
            if persisted_envelope != envelope:
                return None
            candidate = EditedReportSubmission.model_validate_json(
                (
                    self.service.workspace / expected_refs["candidate"]
                ).read_text(encoding="utf-8")
            )
            validate_aggregate_retention(candidate, source_modules)
            if claims:
                validate_editor_protection(candidate, claims)
            validate_final_report_markdown(
                self._canonical_markdown(candidate),
                candidate.special_topic_plan,
            )
            return candidate
        except (OSError, ValueError, json.JSONDecodeError):
            return None

    def _write_aggregate_chief_completion(
        self,
        state: dict,
        envelope: TaskEnvelope,
        candidate: EditedReportSubmission,
    ) -> str:
        run_id = state["run_id"]
        refs = {
            "candidate": (
                f"Work/runs/{run_id}/edited-revisions/"
                "aggregate-chief-r0.json"
            ),
            "envelope": (
                f"Work/runs/{run_id}/context/"
                "aggregate-chief-envelope.json"
            ),
            "editor_input": str(envelope.input_contract_ref),
            "source_manifest": (
                f"Work/runs/{run_id}/context/"
                "aggregate-source-manifest.json"
            ),
        }
        self.service.store.write_json(
            refs["candidate"],
            candidate.model_dump(mode="json"),
        )
        self.service.store.write_json(
            refs["envelope"],
            envelope.model_dump(mode="json"),
        )
        completion_path = self.service.store.write_json(
            f"Work/runs/{run_id}/aggregate-chief-completion.json",
            {
                "kind": "aggregate_chief_completion",
                "version": 1,
                "run_id": run_id,
                "refs": refs,
                "artifact_sha256": {
                    name: self._sha256(self.service.workspace / ref)
                    for name, ref in refs.items()
                },
            },
        )
        return completion_path.relative_to(
            self.service.workspace
        ).as_posix()

    def _restore_aggregate_final_completion(
        self,
        state: dict,
        source_modules: dict[str, str],
        claims: list,
    ) -> bool:
        run_id = state["run_id"]
        final_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        if not state.get("resume") or not (
            self.service.workspace / final_ref
        ).is_file():
            return False
        try:
            completion, artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=final_ref,
                lifecycle="final",
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key="chief-editor-auditor",
            )
            if len(completion.subject_refs) != 1:
                raise ValueError(
                    "aggregate final completion requires one subject"
                )
            edited = EditedReportSubmission.model_validate_json(
                (
                    self.service.workspace / completion.subject_refs[0]
                ).read_text(encoding="utf-8")
            )
            validate_aggregate_retention(edited, source_modules)
            if claims:
                validate_editor_protection(edited, claims)
            validate_final_report_markdown(
                self._canonical_markdown(edited),
                edited.special_topic_plan,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay aggregate final completion: "
                f"{final_ref}: {exc}"
            ) from exc
        state["edited_report"] = edited
        state["final_review_completion_ref"] = final_ref
        snapshot_ref = (
            f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        )
        if (self.service.workspace / snapshot_ref).is_file():
            state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = (
            self._latest_final_residual_risks(artifacts)
        )
        return True

    def _load_current_review_completion(
        self,
        *,
        run_id: str,
        completion_ref: str,
        lifecycle: str,
        reviewer_agent_id: str,
        reviewer_session_key: str | set[str],
        subject_refs: list[str] | None = None,
    ) -> tuple[ReviewCompletionRecord, list[object]]:
        """Load one exact current-protocol completion and all referenced artifacts."""

        run_prefix = f"Work/runs/{run_id}/"

        def read_ref(ref: str) -> tuple[dict, Path]:
            if not ref.startswith(run_prefix):
                raise ValueError(f"review ref is outside current run: {ref}")
            path = (self.service.workspace / ref).resolve()
            run_root = (self.service.workspace / f"Work/runs/{run_id}").resolve()
            if not path.is_relative_to(run_root) or not path.is_file():
                raise ValueError(f"review ref is not a readable current-run artifact: {ref}")
            return json.loads(path.read_text(encoding="utf-8")), path

        raw, _ = read_ref(completion_ref)
        completion = ReviewCompletionRecord.model_validate(raw)
        reviewer_session_matches = (
            completion.reviewer_session_key == reviewer_session_key
            if isinstance(reviewer_session_key, str)
            else completion.reviewer_session_key in reviewer_session_key
        )
        if (
            completion.lifecycle != lifecycle
            or completion.run_id != run_id
            or completion.reviewer_agent_id != reviewer_agent_id
            or not reviewer_session_matches
        ):
            raise ValueError("review completion identity does not match the active lifecycle")
        if subject_refs is not None and completion.subject_refs != subject_refs:
            raise ValueError("review completion subject refs do not match current subjects")
        for ref in completion.subject_refs:
            read_ref(ref)
        for ref, expected_sha256 in completion.artifact_sha256.items():
            _, path = read_ref(ref)
            actual_sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual_sha256 != expected_sha256:
                raise ValueError(f"review artifact hash mismatch after completion: {ref}")

        lifecycle_kinds = {
            "module": {
                "finding": "module_review_finding_submission",
                "verdict": "module_review_verdict_submission",
            },
            "cross": {
                "finding": "cross_review_finding_submission",
                "verdict": "cross_review_verdict_submission",
            },
            "final": {
                "finding": "final_review_finding_submission",
                "verdict": "final_review_verdict_submission",
            },
        }
        artifacts: list[object] = []
        findings_by_id: dict[str, dict] = {}
        regression_findings_by_id: dict[str, dict] = {}
        embedded_new_findings_by_id: dict[str, dict] = {}
        verdict_ids: set[str] = set()

        def artifact_values(
            payload: dict,
            *,
            field: str,
            id_field: str,
            ref: str,
        ) -> list[tuple[str, dict]]:
            values = payload.get(field, [])
            if not isinstance(values, list):
                raise ValueError(f"review completion artifact has non-list {field}: {ref}")
            identified = [
                (value.get(id_field), value) for value in values if isinstance(value, dict)
            ]
            if len(identified) != len(values) or any(
                not isinstance(value_id, str) or not value_id for value_id, _ in identified
            ):
                raise ValueError(f"review completion artifact has invalid {field} ids: {ref}")
            return identified

        expected_finding_kind = lifecycle_kinds[lifecycle]["finding"]
        for index, ref in enumerate(completion.finding_refs):
            payload, _ = read_ref(ref)
            artifact_kind = str(payload.get("kind", ""))
            if artifact_kind != expected_finding_kind:
                raise ValueError(
                    f"review completion finding ref has the wrong artifact kind: {ref}"
                )
            artifacts.append(payload)
            for finding_id, finding in artifact_values(
                payload,
                field="findings",
                id_field="id",
                ref=ref,
            ):
                if finding_id in findings_by_id:
                    raise ValueError("review completion contains duplicate immutable finding ids")
                findings_by_id[finding_id] = finding
                if index > 0:
                    regression_findings_by_id[finding_id] = finding

        expected_verdict_kind = lifecycle_kinds[lifecycle]["verdict"]
        for ref in completion.verdict_refs:
            payload, _ = read_ref(ref)
            artifact_kind = str(payload.get("kind", ""))
            if artifact_kind != expected_verdict_kind:
                raise ValueError(
                    f"review completion verdict ref has the wrong artifact kind: {ref}"
                )
            artifacts.append(payload)
            verdict_ids.update(
                verdict_id
                for verdict_id, _ in artifact_values(
                    payload,
                    field="verdicts",
                    id_field="finding_id",
                    ref=ref,
                )
            )
            for finding_id, finding in artifact_values(
                payload,
                field="new_findings",
                id_field="id",
                ref=ref,
            ):
                if finding_id in embedded_new_findings_by_id:
                    raise ValueError("review completion verdicts repeat a new immutable finding id")
                embedded_new_findings_by_id[finding_id] = finding

        if set(regression_findings_by_id) != set(embedded_new_findings_by_id):
            raise ValueError(
                "review completion regression finding refs do not match verdict new findings"
            )
        if regression_findings_by_id != embedded_new_findings_by_id:
            raise ValueError(
                "review completion regression findings differ from verdict new findings"
            )

        finding_ids = set(findings_by_id)
        resolved_ids = set(completion.resolved_finding_ids)
        if resolved_ids != finding_ids:
            raise ValueError("review completion resolved ids do not equal all immutable findings")
        if not finding_ids.issubset(verdict_ids):
            raise ValueError("review completion lacks reviewer verdicts for findings")
        return completion, artifacts

    @staticmethod
    def _module_reviewer_session_keys(
        module_id: str,
        lifecycle_id: str,
    ) -> set[str]:
        """Accept stable v2 identities and exact legacy v1 lifecycle identities."""

        stable = f"module-auditor-{module_id}"
        return {
            stable,
            f"{stable}-initial",
            f"{stable}-{lifecycle_id}",
        }

    @staticmethod
    def _latest_cross_synthesis(artifacts: list[object]) -> list:
        synthesis = []
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("kind") in {
                "cross_review_finding_submission",
                "cross_review_verdict_submission",
            }:
                synthesis = [
                    CrossSynthesisInput.model_validate(item)
                    for item in artifact.get("synthesis_inputs", [])
                ]
        return synthesis

    @staticmethod
    def _latest_final_residual_risks(artifacts: list[object]) -> list[str]:
        residual_risks: list[str] = []
        for artifact in artifacts:
            if isinstance(artifact, dict) and artifact.get("kind") in {
                "final_review_finding_submission",
                "final_review_verdict_submission",
            }:
                values = artifact.get("residual_risks", [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) for value in values
                ):
                    raise ValueError("final review residual_risks must be a string list")
                residual_risks = values
        return residual_risks

    @staticmethod
    def _canonical_payload_sha256(payload: object) -> str:
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _cross_decision_pack_semantic_sha256(
        self, pack: CrossDecisionPack
    ) -> str:
        """Hash the complete pack payload without its self-referential hash."""

        payload = pack.model_dump(mode="json")
        payload.pop("pack_sha256", None)
        return self._canonical_payload_sha256(payload)

    def _require_current_run_artifact(
        self,
        run_id: str,
        ref: str,
        *,
        label: str,
    ) -> Path:
        """Resolve one immutable artifact while enforcing the current-run boundary."""

        run_prefix = f"Work/runs/{run_id}/"
        if not isinstance(ref, str) or not ref.startswith(run_prefix):
            raise AgentWorkflowError(f"{label} is outside the current run: {ref}")
        lexical = self.service.workspace / ref
        path = lexical.resolve()
        run_root = (self.service.workspace / f"Work/runs/{run_id}").resolve()
        if (
            lexical.is_symlink()
            or not path.is_file()
            or not path.is_relative_to(run_root)
        ):
            raise AgentWorkflowError(
                f"{label} is not a readable current-run artifact: {ref}"
            )
        return path

    def _load_current_cross_decision_pack(
        self,
        state: dict,
    ) -> CrossDecisionPack | None:
        """Load and verify a previously materialized pack, if the checkpoint names one."""

        run_id = state["run_id"]
        ref = state.get("cross_decision_pack_ref")
        expected_hash = state.get("cross_decision_pack_sha256")
        if ref is None and expected_hash is None:
            return None
        if not ref or not expected_hash:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack binding is incomplete (ref/hash required)"
            )
        path = self._require_current_run_artifact(
            run_id, str(ref), label="CrossDecisionPack"
        )
        actual_file_hash = self._sha256(path)
        if actual_file_hash != expected_hash:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack artifact hash does not match its checkpoint"
            )
        try:
            pack = CrossDecisionPack.model_validate_json(
                path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack is unreadable or invalid"
            ) from exc
        if pack.run_id != run_id:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack belongs to another run"
            )
        if pack.pack_sha256 != self._cross_decision_pack_semantic_sha256(pack):
            raise AgentWorkflowError(
                "Chief CrossDecisionPack semantic hash is invalid"
            )
        completion_ref = state.get("cross_review_completion_ref")
        if completion_ref and pack.cross_review_completion_ref != completion_ref:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack is bound to another Cross completion"
            )
        for artifact_ref, expected_artifact_hash in pack.artifact_sha256.items():
            artifact_path = self._require_current_run_artifact(
                run_id, artifact_ref, label="CrossDecisionPack artifact"
            )
            if self._sha256(artifact_path) != expected_artifact_hash:
                raise AgentWorkflowError(
                    "CrossDecisionPack artifact hash mismatch: "
                    f"{artifact_ref}"
                )
        state["cross_decision_pack"] = pack
        return pack

    def _materialize_chief_cross_decision_pack(
        self, state: dict
    ) -> CrossDecisionPack:
        """Materialize the terminal Cross boundary consumed by Chief/Final.

        This reducer consumes the already completed Cross review and IF registry;
        it never performs a second semantic Cross pass.  Every reference and
        E-* binding is checked before writing the immutable pack.
        """

        run_id = state.get("run_id")
        if not isinstance(run_id, str) or not run_id:
            raise AgentWorkflowError("Chief cannot materialize a pack without run_id")
        existing = self._load_current_cross_decision_pack(state)
        if existing is not None:
            return existing

        modules = state.get("module_submissions")
        if not isinstance(modules, dict) or set(modules) != set(REPORT_MODULE_IDS):
            raise AgentWorkflowError(
                "Chief cannot start: CrossDecisionPack requires all five approved modules"
            )
        module_refs = [
            f"Work/runs/{run_id}/modules/{module_id}-r{modules[module_id].revision}.json"
            for module_id in REPORT_MODULE_IDS
        ]
        completion_ref = state.get("cross_review_completion_ref")
        if not isinstance(completion_ref, str) or not completion_ref:
            raise AgentWorkflowError(
                "Chief cannot start: Cross review completion is missing"
            )
        try:
            completion, cross_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=completion_ref,
                lifecycle="cross",
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key={"cross-module-reviewer", "cross-owner-wave"},
                subject_refs=module_refs,
            )
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "Chief cannot start: current Cross completion is invalid"
            ) from exc

        registry = state.get("interface_resolution_registry")
        registry_ref = state.get("interface_resolution_registry_ref")
        if registry is not None and not isinstance(registry, InterfaceResolutionRegistry):
            try:
                registry = InterfaceResolutionRegistry.model_validate(registry)
            except ValueError as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry is invalid"
                ) from exc
        if registry is None and registry_ref:
            registry_path = self._require_current_run_artifact(
                run_id, str(registry_ref), label="interface resolution registry"
            )
            expected_registry_hash = state.get("interface_resolution_registry_sha256")
            actual_registry_hash = self._sha256(registry_path)
            if expected_registry_hash != actual_registry_hash:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry hash is stale"
                )
            try:
                registry = InterfaceResolutionRegistry.model_validate_json(
                    registry_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry is invalid"
                ) from exc
        if registry is not None:
            if registry.run_id != run_id:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry belongs to another run"
                )
            if not registry_ref:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry lacks an immutable ref"
                )
            if registry.pending_request_ids:
                raise AgentWorkflowError(
                    "Chief cannot start while IF/XMR records remain pending: "
                    f"{list(registry.pending_request_ids)}"
                )
            state["interface_resolution_registry"] = registry

        # Verify closure artifacts and map each terminal IF to its immutable
        # closure batch.  The registry remains the source of truth for status.
        closure_source_by_request: dict[str, str] = {}
        closure_history_by_request: dict[str, list[tuple[str, object]]] = {}
        closure_refs = list(state.get("interface_closure_refs", []))
        closure_hashes = dict(state.get("interface_closure_sha256", {}))
        for closure_ref in closure_refs:
            closure_path = self._require_current_run_artifact(
                run_id, str(closure_ref), label="interface closure"
            )
            expected_hash = closure_hashes.get(closure_ref)
            if expected_hash is not None and self._sha256(closure_path) != expected_hash:
                raise AgentWorkflowError(
                    "Chief cannot start: interface closure hash is stale"
                )
            try:
                batch = InterfaceResolutionClosureBatch.model_validate_json(
                    closure_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: interface closure artifact is invalid"
                ) from exc
            if batch.run_id != run_id:
                raise AgentWorkflowError(
                    "Chief cannot start: interface closure belongs to another run"
                )
            for closure in batch.closures:
                closure_source_by_request[closure.request_id] = str(closure_ref)
                closure_history_by_request.setdefault(closure.request_id, []).append(
                    (str(closure_ref), closure)
                )

        if registry_ref:
            registry_path = self._require_current_run_artifact(
                run_id, str(registry_ref), label="interface resolution registry"
            )
        known_evidence_ids = {
            source.id
            for source in SourceLedger(self.service.workspace, run_id).records
            if source.id.startswith("E-")
        }

        def terminal_evidence_ids(values: list[str]) -> list[str]:
            evidence_ids = list(dict.fromkeys(value for value in values if value.startswith("E-")))
            if not evidence_ids:
                raise AgentWorkflowError(
                    "Chief cannot start: terminal Cross IF/XMR decision lacks E-* evidence"
                )
            if known_evidence_ids and not set(evidence_ids).issubset(known_evidence_ids):
                missing = sorted(set(evidence_ids) - known_evidence_ids)
                raise AgentWorkflowError(
                    "Chief cannot start: IF/XMR decision references unknown E-* ids: "
                    f"{missing}"
                )
            return evidence_ids

        if_closures: list = []
        if registry is not None:
            for request_id, resolution in sorted(registry.resolutions.items()):
                request = resolution.request
                status = resolution.closure_status
                if status in {"pending_cross", "reroute_to_owner"}:
                    raise AgentWorkflowError(
                        "Chief cannot start while IF/XMR records remain pending: "
                        f"{request_id}"
                    )
                cross_closure = resolution.cross_closure
                historical_reroute = next(
                    (
                        (source_ref, closure)
                        for source_ref, closure in closure_history_by_request.get(
                            request_id, []
                        )
                        if getattr(closure, "outcome", None) == "reroute_to_owner"
                    ),
                    None,
                )
                # A reroute is intentionally represented in the terminal pack
                # even after r1 has resolved its XMR finding; the final registry
                # no longer reports it as pending, but the owner decision remains
                # part of the Chief/Final audit boundary.
                pack_status = (
                    "reroute_to_owner" if historical_reroute is not None else status
                )
                if historical_reroute is not None:
                    reroute_source_ref, reroute_closure = historical_reroute
                    cross_closure = reroute_closure
                else:
                    reroute_source_ref = None
                evidence_values = [
                    *request.evidence_ids,
                    *resolution.disposition.evidence_ids,
                    *resolution.disposition.checked_evidence_ids,
                ]
                if cross_closure is not None:
                    evidence_values.extend(cross_closure.checked_evidence_ids)
                evidence_ids = terminal_evidence_ids(evidence_values)
                requester_submodule_id = (
                    request.requester_submodule_id
                    or REPORT_TAXONOMY[request.requester_module_id].submodules[0]
                )
                target_submodule_id = (
                    request.target_submodule_id
                    or REPORT_TAXONOMY[request.target_module_id].submodules[0]
                )
                source_ref = closure_source_by_request.get(request_id)
                if reroute_source_ref is not None:
                    source_ref = reroute_source_ref
                if source_ref is None:
                    source_ref = str(registry_ref) if registry_ref else ""
                if not source_ref:
                    raise AgentWorkflowError(
                        f"Chief cannot start: IF closure {request_id} lacks source ref"
                    )
                if pack_status == "answered":
                    answer = resolution.disposition.answer
                    conditions = list(resolution.disposition.conditions)
                    boundary = None
                    residual_risk = None
                    owner_finding_id = None
                elif pack_status == "resolved_by_cross":
                    answer = cross_closure.reason if cross_closure is not None else None
                    conditions = list(resolution.disposition.conditions) or [
                        "Cross 已基于当前五模块证据闭合该接口。"
                    ]
                    boundary = None
                    residual_risk = None
                    owner_finding_id = None
                elif pack_status == "confirmed_missing":
                    answer = None
                    conditions = []
                    boundary = (
                        cross_closure.boundary
                        if cross_closure is not None
                        else resolution.disposition.boundary
                    )
                    residual_risk = (
                        cross_closure.residual_risk
                        if cross_closure is not None
                        else resolution.disposition.unresolved_reason
                    )
                    owner_finding_id = None
                elif pack_status == "reroute_to_owner":
                    answer = None
                    conditions = []
                    boundary = cross_closure.boundary if cross_closure is not None else None
                    residual_risk = (
                        cross_closure.residual_risk
                        if cross_closure is not None
                        else None
                    )
                    owner_finding_id = (
                        cross_closure.owner_finding_id
                        if cross_closure is not None
                        else f"XMR-{request_id}"
                    )
                else:
                    raise AgentWorkflowError(
                        f"Chief cannot start: unsupported terminal IF status {status}"
                    )
                if_closures.append(
                    CrossDecisionIFClosure(
                        request_id=request_id,
                        requester_submodule_id=requester_submodule_id,
                        target_submodule_id=target_submodule_id,
                        question=request.question,
                        status=pack_status,
                        answer=answer,
                        conditions=conditions,
                        evidence_ids=evidence_ids,
                        source_ref=source_ref,
                        boundary=boundary,
                        residual_risk=residual_risk,
                        owner_finding_id=owner_finding_id,
                    )
                )

        # Cross artifacts are the sole source of XMR finding/verdict semantics;
        # this reducer merely translates the already closed immutable records.
        finding_records: dict[str, tuple[dict, str]] = {}
        verdict_records: dict[str, tuple[dict, str]] = {}
        completion_artifact_refs = [
            *completion.finding_refs,
            *completion.verdict_refs,
        ]
        for artifact_ref in completion_artifact_refs:
            artifact_path = self._require_current_run_artifact(
                run_id, artifact_ref, label="Cross review artifact"
            )
            try:
                artifact_payload = json.loads(artifact_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: Cross review artifact is unreadable"
                ) from exc
            for finding in [
                *artifact_payload.get("findings", []),
                *artifact_payload.get("new_findings", []),
            ]:
                if isinstance(finding, dict) and isinstance(finding.get("id"), str):
                    finding_records[finding["id"]] = (finding, artifact_ref)
            for verdict in artifact_payload.get("verdicts", []):
                if isinstance(verdict, dict) and isinstance(verdict.get("finding_id"), str):
                    verdict_records[verdict["finding_id"]] = (verdict, artifact_ref)

        xmr_verdicts: list = []
        xmr_ids = sorted(
            finding_id
            for finding_id in finding_records
            if finding_id.startswith("XMR-")
        )
        for finding_id in xmr_ids:
            finding, finding_ref = finding_records[finding_id]
            verdict_record = verdict_records.get(finding_id)
            if verdict_record is None:
                raise AgentWorkflowError(
                    f"Chief cannot start: XMR finding {finding_id} lacks a verdict"
                )
            verdict, verdict_ref = verdict_record
            if verdict.get("verdict") != "resolved":
                raise AgentWorkflowError(
                    f"Chief cannot start: XMR finding {finding_id} is not resolved"
                )
            evidence_values = [
                *finding.get("evidence_refs", []),
                *verdict.get("evidence_refs", []),
            ]
            evidence_ids = terminal_evidence_ids(evidence_values)
            xmr_verdicts.append(
                CrossDecisionXMRVerdict(
                    finding_id=finding_id,
                    owner_module_id=finding["owner_module_id"],
                    target_submodule_ids=list(finding["target_submodule_ids"]),
                    related_module_ids=list(finding["related_module_ids"]),
                    verdict=verdict["verdict"],
                    reason=str(verdict.get("reason") or verdict.get("summary") or "Cross 已关闭该接口。"),
                    evidence_refs=evidence_ids,
                    source_refs=list(dict.fromkeys([finding_ref, verdict_ref])),
                )
            )

        synthesis_inputs = [
            item
            if isinstance(item, CrossSynthesisInput)
            else CrossSynthesisInput.model_validate(item)
            for item in state.get("cross_synthesis_inputs", [])
        ]
        residual_risks = list(
            dict.fromkeys(
                value.strip()
                for value in [
                    *dict(state.get("interface_residual_risks", {})).values(),
                    *[
                        closure.residual_risk
                        for closure in if_closures
                        if closure.residual_risk
                    ],
                ]
                if isinstance(value, str) and value.strip()
            )
        )

        artifact_refs = {
            completion_ref,
            *(closure.source_ref for closure in if_closures),
            *(ref for verdict in xmr_verdicts for ref in verdict.source_refs),
        }
        artifact_sha256: dict[str, str] = {}
        for artifact_ref in sorted(artifact_refs):
            artifact_path = self._require_current_run_artifact(
                run_id, artifact_ref, label="CrossDecisionPack artifact"
            )
            artifact_sha256[artifact_ref] = self._sha256(artifact_path)
        provisional = CrossDecisionPack(
            run_id=run_id,
            module_ids=list(REPORT_MODULE_IDS),
            cross_review_completion_ref=completion_ref,
            synthesis_inputs=synthesis_inputs,
            if_closures=if_closures,
            xmr_verdicts=xmr_verdicts,
            residual_risks=residual_risks,
            artifact_sha256=artifact_sha256,
            pack_sha256="0" * 64,
        )
        pack = provisional.model_copy(
            update={
                "pack_sha256": self._cross_decision_pack_semantic_sha256(provisional)
            }
        )
        pack_ref = f"Work/runs/{run_id}/reviews/cross-decision-pack.json"
        pack_path = self.service.workspace / pack_ref
        if pack_path.is_file():
            try:
                existing_pack = CrossDecisionPack.model_validate_json(
                    pack_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: existing CrossDecisionPack is invalid"
                ) from exc
            if existing_pack != pack:
                raise AgentWorkflowError(
                    "Chief cannot start: immutable CrossDecisionPack differs from current Cross completion"
                )
            pack = existing_pack
        else:
            self.service.store.write_json(pack_ref, pack.model_dump(mode="json"))
        state["cross_decision_pack"] = pack
        state["cross_decision_pack_ref"] = pack_ref
        state["cross_decision_pack_sha256"] = self._sha256(
            self.service.workspace / pack_ref
        )
        return pack

    def _current_chief_editor_input(self, state: dict) -> ChiefEditorInput:
        pack = self._materialize_chief_cross_decision_pack(state)
        pack_view_payload = pack.model_dump(mode="python")
        pack_view_payload.pop("artifact_sha256", None)
        pack_view_payload.pop("pack_sha256", None)
        pack_view_payload["artifact_refs"] = sorted(pack.artifact_sha256)
        return ChiefEditorInput(
            run_id=state["run_id"],
            approved_module_markers={
                module_id: f"[[APPROVED_MODULE:{module_id}]]"
                for module_id in REPORT_MODULE_IDS
            },
            modules={
                module_id: module_content_view(
                    state["module_submissions"][module_id]
                )
                for module_id in REPORT_MODULE_IDS
            },
            cross_decision=CrossDecisionPackView.model_validate(pack_view_payload),
            cross_decision_pack_ref=state["cross_decision_pack_ref"],
            cross_decision_pack_sha256=state["cross_decision_pack_sha256"],
            # Keep the legacy completion ref in persisted inputs for old
            # checkpoint readers; the pack/ref/hash above are authoritative.
            cross_review_completion_ref=state["cross_review_completion_ref"],
            special_topic_plan=state.get("special_topic_plan"),
        )

    def _chief_request_context(self, state: dict) -> dict:
        revision = state.get("revision_request")
        request = state["request"]
        if isinstance(revision, RevisionRequest):
            request_context = {
                "workflow_kind": "revision",
                "baseline_version_id": revision.baseline_version_id,
                "feedback": revision.feedback,
                "target_module_ids": list(revision.target_module_ids),
                "target_submodule_ids": list(
                    revision.target_submodule_ids
                ),
                "target_claim_ids": list(revision.target_claim_ids),
            }
        else:
            request_context = {
                "workflow_kind": "full",
                "operation": str(
                    getattr(request, "operation", "full_report")
                ),
                "instruction": str(
                    getattr(request, "instruction", "")
                ),
                "target_modules": list(
                    getattr(request, "target_modules", REPORT_MODULE_IDS)
                ),
                "execution_requirements": list(
                    getattr(request, "execution_requirements", [])
                ),
                "missing_evidence_policy": getattr(
                    request, "missing_evidence_policy", None
                ),
            }
        request_context["chief_editor_constraints"] = list(
            state.get("chief_editor_constraints", [])
        )
        request_context["active_supplement_constraints"] = (
            self._user_supplement_constraints(
                state,
                stage="chief_edit",
                target_ids={*REPORT_MODULE_IDS},
            )
        )
        return request_context

    def _chief_special_topic_context(self, state: dict) -> tuple[str | None, str]:
        if state.get("special_topic_plan") is None:
            return None, ""
        ref = (
            f"Work/runs/{state['run_id']}/context/"
            "special-topic-knowledge.md"
        )
        path = self.service.workspace / ref
        if not path.is_file():
            raise AgentWorkflowError(
                "Chief completion lacks its current-run special-topic context"
            )
        text = path.read_text(encoding="utf-8")
        if text.endswith("\n"):
            text = text[:-1]
        return ref, text

    def _chief_expected_envelope_inputs(self, state: dict) -> list[str]:
        # Chief receives only the typed five-module input and optional
        # special-topic context.  Evidence/photo artifacts are assembled by
        # runtime and are intentionally absent from the provider envelope.
        if not state.get("cross_decision_pack_ref"):
            self._materialize_chief_cross_decision_pack(state)
        special_ref, _ = self._chief_special_topic_context(state)
        return [
            f"Work/runs/{state['run_id']}/context/chief-editor-input.json",
            *([special_ref] if special_ref else []),
        ]

    def _chief_expected_inline_context(self, state: dict) -> str:
        _special_ref, special_topic_context = (
            self._chief_special_topic_context(state)
        )
        return "\n\n".join(
            text
            for text in (
                special_topic_context,
                self._role_skill_context(state, "chief-editor"),
            )
            if text
        )

    def _chief_completion_refs(
        self,
        state: dict,
        *,
        envelope_input_refs: list[str],
    ) -> dict:
        run_id = state["run_id"]
        if not state.get("cross_decision_pack_ref"):
            self._materialize_chief_cross_decision_pack(state)
        return {
            "candidate": (
                f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
            ),
            "editor_input": (
                f"Work/runs/{run_id}/context/chief-editor-input.json"
            ),
            "envelope": (
                f"Work/runs/{run_id}/context/chief-editor-envelope.json"
            ),
            "claim_ledger": (
                f"Work/runs/{run_id}/ledgers/claims.json"
            ),
            "cross_decision_pack": state["cross_decision_pack_ref"],
            "cross_review_completion": state[
                "cross_review_completion_ref"
            ],
            "module_subjects": {
                module_id: (
                    f"Work/runs/{run_id}/modules/{module_id}-r"
                    f"{state['module_submissions'][module_id].revision}.json"
                )
                for module_id in REPORT_MODULE_IDS
            },
            "module_review_completions": dict(
                sorted(
                    state.get(
                        "module_review_completion_refs", {}
                    ).items()
                )
            ),
            "envelope_inputs": list(envelope_input_refs),
        }

    @staticmethod
    def _flatten_chief_completion_refs(refs: dict) -> list[str]:
        values = [
            refs.get("candidate"),
            refs.get("editor_input"),
            refs.get("envelope"),
            refs.get("claim_ledger"),
            refs.get("cross_decision_pack"),
            refs.get("cross_review_completion"),
            *dict(refs.get("module_subjects", {})).values(),
            *dict(refs.get("module_review_completions", {})).values(),
            *list(refs.get("envelope_inputs", [])),
        ]
        return list(
            dict.fromkeys(
                str(value) for value in values if isinstance(value, str)
            )
        )

    def _chief_semantic_context_sha256(
        self,
        state: dict,
        editor_input: ChiefEditorInput,
    ) -> str:
        special_ref, _special_text = self._chief_special_topic_context(
            state
        )
        return self._canonical_payload_sha256(
            {
                "version": 1,
                "request": self._chief_request_context(state),
                "editor_input": editor_input.model_dump(mode="json"),
                "template_role_context": self._role_skill_context(
                    state, "chief-editor"
                ),
                "special_topic_context": (
                    {
                        "ref": special_ref,
                        "sha256": self._sha256(
                            self.service.workspace / special_ref
                        ),
                    }
                    if special_ref is not None
                    else None
                ),
            }
        )

    def _write_chief_editor_completion(
        self,
        state: dict,
        *,
        editor_input: ChiefEditorInput,
        envelope: TaskEnvelope,
    ) -> str:
        run_id = state["run_id"]
        refs = self._chief_completion_refs(
            state,
            envelope_input_refs=envelope.input_refs,
        )
        artifact_refs = self._flatten_chief_completion_refs(refs)
        completion_path = self.service.store.write_json(
            f"Work/runs/{run_id}/chief-editor-completion.json",
            {
                "kind": "chief_editor_completion",
                "version": 1,
                "run_id": run_id,
                "workflow_kind": (
                    "revision"
                    if isinstance(
                        state.get("revision_request"), RevisionRequest
                    )
                    else "full"
                ),
                "cross_decision_pack_ref": state["cross_decision_pack_ref"],
                "cross_decision_pack_sha256": state[
                    "cross_decision_pack_sha256"
                ],
                "refs": refs,
                "artifact_sha256": {
                    ref: self._sha256(self.service.workspace / ref)
                    for ref in artifact_refs
                },
                "semantic_context_sha256": (
                    self._chief_semantic_context_sha256(
                        state, editor_input
                    )
                ),
            },
        )
        return completion_path.relative_to(
            self.service.workspace
        ).as_posix()

    def _load_chief_editor_completion(
        self,
        state: dict,
        completion_ref: str,
    ) -> tuple[EditedReportSubmission, TaskEnvelope, dict]:
        run_id = state["run_id"]
        canonical_completion_ref = (
            f"Work/runs/{run_id}/chief-editor-completion.json"
        )
        if completion_ref != canonical_completion_ref:
            raise AgentWorkflowError(
                "Chief completion ref is not the canonical current-run path"
            )
        try:
            self._require_template_skill(state)
            completion_path = self.service.workspace / completion_ref
            completion = json.loads(
                completion_path.read_text(encoding="utf-8")
            )
            expected_workflow_kind = (
                "revision"
                if isinstance(
                    state.get("revision_request"), RevisionRequest
                )
                else "full"
            )
            if (
                completion.get("kind") != "chief_editor_completion"
                or completion.get("version") != 1
                or completion.get("run_id") != run_id
                or completion.get("workflow_kind")
                != expected_workflow_kind
            ):
                raise ValueError(
                    "Chief completion identity does not match this workflow"
                )

            pack = self._materialize_chief_cross_decision_pack(state)
            if completion.get("cross_decision_pack_ref") != state.get(
                "cross_decision_pack_ref"
            ) or completion.get("cross_decision_pack_sha256") != state.get(
                "cross_decision_pack_sha256"
            ):
                raise ValueError(
                    "Chief completion is not bound to the current CrossDecisionPack"
                )
            if pack.cross_review_completion_ref != state.get(
                "cross_review_completion_ref"
            ):
                raise ValueError("Chief completion CrossDecisionPack binding is stale")

            envelope_ref = (
                f"Work/runs/{run_id}/context/"
                "chief-editor-envelope.json"
            )
            envelope = TaskEnvelope.model_validate_json(
                (
                    self.service.workspace / envelope_ref
                ).read_text(encoding="utf-8")
            )
            editor_input_ref = (
                f"Work/runs/{run_id}/context/"
                "chief-editor-input.json"
            )
            expected_input_refs = self._chief_expected_envelope_inputs(
                state
            )
            if (
                envelope.task_id != "chief-edit"
                or envelope.run_id != run_id
                or envelope.agent_id != "chief-editor"
                or envelope.allowed_outputs
                != ["edited_report_submission"]
                or envelope.allowed_tools
                != [
                    "write_result_part",
                    "list_result_parts",
                    "submit_result",
                ]
                or envelope.revision != 0
                or envelope.prior_result_ref is not None
                or envelope.context_summary_refs
                or envelope.target_submodule_ids
                or envelope.input_contract_kind
                != "chief_editor_input"
                or envelope.input_contract_ref != editor_input_ref
                or envelope.input_refs != expected_input_refs
                or envelope.inline_context
                != self._chief_expected_inline_context(state)
            ):
                raise ValueError(
                    "Chief envelope identity or current semantic inputs "
                    "do not match this run"
                )
            required_constraints = [
                *state.get("chief_editor_constraints", []),
                *self._user_supplement_constraints(
                    state,
                    stage="chief_edit",
                    target_ids={*REPORT_MODULE_IDS},
                ),
            ]
            if any(
                constraint not in envelope.constraints
                for constraint in required_constraints
            ):
                raise ValueError(
                    "Chief envelope lacks current request constraints"
                )

            expected_refs = self._chief_completion_refs(
                state,
                envelope_input_refs=expected_input_refs,
            )
            if completion.get("refs") != expected_refs:
                raise ValueError(
                    "Chief completion refs do not match current modules "
                    "and review context"
                )
            artifact_refs = self._flatten_chief_completion_refs(
                expected_refs
            )
            run_root = (
                self.service.workspace / f"Work/runs/{run_id}"
            ).resolve()
            actual_hashes: dict[str, str] = {}
            for ref in artifact_refs:
                lexical = self.service.workspace / ref
                path = lexical.resolve()
                if (
                    lexical.is_symlink()
                    or not path.is_relative_to(run_root)
                    or not path.is_file()
                ):
                    raise ValueError(
                        "Chief completion contains a non-current-run ref: "
                        f"{ref}"
                    )
                actual_hashes[ref] = self._sha256(path)
            if completion.get("artifact_sha256") != actual_hashes:
                raise ValueError(
                    "Chief completion artifact hash mismatch"
                )

            persisted_input = ChiefEditorInput.model_validate_json(
                (
                    self.service.workspace / editor_input_ref
                ).read_text(encoding="utf-8")
            )
            expected_input = self._current_chief_editor_input(state)
            if persisted_input != expected_input:
                raise ValueError(
                    "Chief editor input does not match current approved "
                    "modules and Cross completion"
                )
            if completion.get("semantic_context_sha256") != (
                self._chief_semantic_context_sha256(
                    state, expected_input
                )
            ):
                raise ValueError(
                    "Chief semantic context no longer matches the current "
                    "request"
                )

            candidate_ref = str(expected_refs["candidate"])
            candidate = EditedReportSubmission.model_validate_json(
                (
                    self.service.workspace / candidate_ref
                ).read_text(encoding="utf-8")
            )
            claims = [
                claim
                for module_id in REPORT_MODULE_IDS
                for claim in state["module_submissions"][
                    module_id
                ].claims
            ]
            validate_editor_protection(candidate, claims)
            state["editor_quality_observations"] = (
                validate_editor_quality(
                    candidate, state["module_submissions"]
                )
            )
            validate_final_report_markdown(
                self._canonical_markdown(candidate),
                candidate.special_topic_plan,
            )
            return candidate, envelope, expected_refs
        except AgentWorkflowError:
            raise
        except (
            OSError,
            ValueError,
            TypeError,
            KeyError,
            json.JSONDecodeError,
        ) as exc:
            raise AgentWorkflowError(
                "refusing to replay Chief because its current-run "
                f"completion is invalid: {completion_ref}: {exc}"
            ) from exc

    def _module_authoring_context_sha256(
        self,
        state: dict,
        module_id: str,
    ) -> str:
        """Fingerprint every durable input that makes a module draft reusable."""

        request = state.get("request")

        def ref_record(ref: str | None) -> dict[str, str | None] | None:
            if not ref:
                return None
            path = (self.service.workspace / ref).resolve()
            return {
                "ref": ref,
                "sha256": (
                    self._sha256(path)
                    if path.is_relative_to(self.service.workspace)
                    and path.is_file()
                    else None
                ),
            }

        planned = None
        dispatch = state.get("module_dispatch")
        if dispatch is not None:
            planned = next(
                (
                    item.model_dump(mode="json")
                    for item in dispatch.module_tasks
                    if item.agent_id == f"module-{module_id}-specialist"
                ),
                None,
            )
        supplement_constraints = (
            self._user_supplement_constraints(
                state,
                stage="module_authoring",
                target_ids={
                    module_id,
                    *REPORT_TAXONOMY[module_id].submodules,
                },
            )
            if request is not None
            and hasattr(request, "user_supplements")
            else []
        )
        payload = {
            "version": 3,
            "run_id": state["run_id"],
            "module_id": module_id,
            "required_submodule_ids": list(
                REPORT_TAXONOMY[module_id].submodules
            ),
            "planned_task": planned,
            "execution_requirements": list(
                getattr(request, "execution_requirements", [])
            ),
            "missing_evidence_policy": getattr(
                request, "missing_evidence_policy", None
            ),
            "supplement_constraints": supplement_constraints,
            "coverage": ref_record(
                state.get("preparation_refs", {}).get("coverage")
            ),
            "evidence": ref_record(
                state.get("preparation_refs", {}).get("evidence")
            ),
            "manifest": ref_record(
                state.get("preparation_refs", {}).get("manifest")
            ),
            "knowledge": ref_record(
                state.get("module_knowledge_refs", {}).get(module_id)
            ),
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _verify_resume_cross_owner_barrier(
        self,
        *,
        run_id: str,
        barrier_ref: str | None,
        module_refs: list[str],
    ) -> None:
        """Require the canonical exact-five Cross-owner barrier before Chief replay."""

        expected_ref = f"Work/runs/{run_id}/lanes/cross-r1/owner-barrier.json"
        if barrier_ref != expected_ref:
            raise AgentWorkflowError(
                "Cross completion cannot be replayed without the canonical round-1 "
                "exact-five owner barrier"
            )
        barrier_path = self.service.workspace / expected_ref
        if not barrier_path.is_file():
            raise AgentWorkflowError(
                f"Cross owner barrier is missing from the current run: {expected_ref}"
            )
        try:
            barrier = CrossOwnerBarrier.model_validate_json(
                barrier_path.read_text(encoding="utf-8")
            )
            verify_cross_owner_barrier(
                self,
                barrier,
                barrier_path=barrier_path,
                expected_run_id=run_id,
                expected_round=1,
                expected_modules=set(REPORT_MODULE_IDS),
            )
            barrier_subject_refs = []
            for module_id in sorted(REPORT_MODULE_IDS, key=float):
                completion = CrossOwnerCompletion.model_validate_json(
                    (
                        self.service.workspace
                        / barrier.completion_refs[module_id]
                    ).read_text(encoding="utf-8")
                )
                barrier_subject_refs.append(completion.subject.ref)
            if barrier_subject_refs != module_refs:
                raise AgentWorkflowError(
                    "Cross owner barrier subjects do not match current module revisions"
                )
        except AgentWorkflowError:
            raise
        except (OSError, ValueError, RuntimeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay Cross review because its exact-five owner "
                f"barrier is invalid: {expected_ref}: {exc}"
            ) from exc

    def _guard_failed_cross_owner_terminal_resume(self, *, run_id: str) -> None:
        """Do not silently redispatch a drained failed Cross-owner wave."""

        completion_path = self.service.workspace / (
            f"Work/runs/{run_id}/reviews/cross-completion.json"
        )
        if completion_path.is_file():
            return
        for review_round in (0, 1):
            terminal_path = self.service.workspace / (
                f"Work/runs/{run_id}/lanes/cross-r{review_round}/owner-terminal.json"
            )
            if not terminal_path.is_file():
                continue
            try:
                terminal = json.loads(terminal_path.read_text(encoding="utf-8"))
            except (OSError, ValueError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "refusing to resume after an unreadable Cross owner terminal "
                    f"manifest: {terminal_path}"
                ) from exc
            if terminal.get("run_id") != run_id:
                raise AgentWorkflowError(
                    f"Cross owner terminal manifest belongs to another run: {terminal_path}"
                )
            if terminal.get("status") == "failed":
                raise AgentWorkflowError(
                    "Cross owner wave is durably failed after all admitted owners "
                    f"drained ({terminal_path}); explicit re-dispatch is required"
                )

    def _restore_resume_state(self, state: dict, checkpoint: dict | None = None) -> None:
        run_id = state["run_id"]
        self._guard_failed_cross_owner_terminal_resume(run_id=run_id)
        if checkpoint is None:
            checkpoint_path = self.service.workspace / f"Work/runs/{run_id}/workflow-state.json"
            if not checkpoint_path.is_file():
                return
            checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        try:
            typed_checkpoint = FullReportCheckpoint.model_validate(checkpoint)
        except ValueError as exc:
            raise AgentWorkflowError(f"invalid resume checkpoint: {exc}") from exc
        if typed_checkpoint.run_id != run_id:
            raise AgentWorkflowError("resume checkpoint belongs to another run")
        if typed_checkpoint.final_review_restart_round is not None:
            state["final_review_restart_round"] = typed_checkpoint.final_review_restart_round
        run_prefix = f"Work/runs/{run_id}/"

        def require_run_ref(ref: str, *, label: str) -> str:
            if not ref.startswith(run_prefix):
                raise AgentWorkflowError(f"{label} is outside the current run: {ref}")
            path = (self.service.workspace / ref).resolve()
            run_root = (self.service.workspace / f"Work/runs/{run_id}").resolve()
            if not path.is_relative_to(run_root) or not path.is_file():
                raise AgentWorkflowError(f"{label} is not a readable current-run artifact: {ref}")
            return ref

        state["preparation_refs"] = dict(typed_checkpoint.preparation_refs)
        state["preparation_sha256"] = dict(typed_checkpoint.preparation_sha256)
        dispatch_path = self.service.workspace / f"Work/runs/{run_id}/workflow/module-dispatch.json"
        if dispatch_path.is_file():
            state["module_dispatch"] = ModuleDispatchPlan.model_validate_json(
                dispatch_path.read_text(encoding="utf-8")
            )
            expected_dispatch_ref = dispatch_path.relative_to(self.service.workspace).as_posix()
            if typed_checkpoint.module_dispatch_ref != expected_dispatch_ref:
                raise AgentWorkflowError(
                    "checkpoint does not identify the canonical current-run module dispatch"
                )
            if not typed_checkpoint.module_knowledge_refs:
                raise AgentWorkflowError(
                    "checkpoint lacks module knowledge refs required by module authoring"
                )
            state["module_knowledge_refs"] = {
                module_id: require_run_ref(ref, label=f"module {module_id} knowledge")
                for module_id, ref in typed_checkpoint.module_knowledge_refs.items()
            }
            missing_knowledge = sorted(
                set(state["request"].target_modules) - set(state["module_knowledge_refs"])
            )
            if missing_knowledge:
                raise AgentWorkflowError(
                    f"checkpoint lacks module knowledge refs: {missing_knowledge}"
                )
        if typed_checkpoint.module_lane_barrier_ref is not None:
            state["module_lane_barrier_ref"] = require_run_ref(
                typed_checkpoint.module_lane_barrier_ref,
                label="module lane barrier",
            )
        if typed_checkpoint.cross_owner_barrier_ref is not None:
            state["cross_owner_barrier_ref"] = require_run_ref(
                typed_checkpoint.cross_owner_barrier_ref,
                label="Cross owner barrier",
            )
        source_records = SourceLedger(self.service.workspace, run_id).records
        known_source_ids = {source.id for source in source_records}
        restored_subjects: dict[str, ModuleSubmission] = {}
        approved_subjects: dict[str, ModuleSubmission] = {}
        rejected: dict[str, str] = {}
        modules_root = self.service.workspace / f"Work/runs/{run_id}/modules"
        for module_id in REPORT_MODULE_IDS:
            valid: list[ModuleSubmission] = []
            for path in modules_root.glob(f"{module_id}-r*.json"):
                try:
                    submission = ModuleSubmission.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                    unknown_sources = sorted(set(submission.source_ids) - known_source_ids)
                    if unknown_sources:
                        raise ValueError(
                            f"module subject declares unregistered sources: {unknown_sources}"
                        )
                    ClaimLedger(claims=submission.claims, sources=source_records)
                except (OSError, ValueError) as exc:
                    rejected[module_id] = str(exc)
                    continue
                valid.append(submission)
            if valid:
                restored_subjects[module_id] = max(valid, key=lambda item: item.revision)
                for candidate in sorted(valid, key=lambda item: item.revision, reverse=True):
                    subject_ref = (
                        f"Work/runs/{run_id}/modules/{module_id}-r{candidate.revision}.json"
                    )
                    completion_ref = typed_checkpoint.module_review_completion_refs.get(module_id)
                    if not completion_ref:
                        continue
                    if not (self.service.workspace / completion_ref).is_file():
                        continue
                    parts = Path(completion_ref).parts
                    try:
                        lifecycle_id = parts[parts.index("module") + 1]
                    except (ValueError, IndexError) as exc:
                        raise AgentWorkflowError(
                            f"invalid module completion path: {completion_ref}"
                        ) from exc
                    try:
                        self._load_current_review_completion(
                            run_id=run_id,
                            completion_ref=completion_ref,
                            lifecycle="module",
                            reviewer_agent_id="evidence-auditor",
                            reviewer_session_key=self._module_reviewer_session_keys(
                                module_id,
                                lifecycle_id,
                            ),
                            subject_refs=[subject_ref],
                        )
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        raise AgentWorkflowError(
                            "refusing to replay a module whose current-run review "
                            f"completion is invalid: {completion_ref}: {exc}"
                        ) from exc
                    approved_subjects[module_id] = candidate
                    state.setdefault("module_review_completion_refs", {})[module_id] = (
                        completion_ref
                    )
                    self.service.store.write_text(
                        f"Outputs/Modules/{module_id}.md", candidate.markdown
                    )
                    break
        state["specialist_submissions"] = restored_subjects
        state["module_submissions"] = approved_subjects
        state["rejected_specialist_submissions"] = rejected
        if set(approved_subjects) != set(REPORT_MODULE_IDS):
            return

        module_refs = [
            (
                f"Work/runs/{run_id}/modules/"
                f"{module_id}-r{approved_subjects[module_id].revision}.json"
            )
            for module_id in REPORT_MODULE_IDS
        ]
        cross_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
        if not (self.service.workspace / cross_ref).is_file():
            return
        self._verify_resume_cross_owner_barrier(
            run_id=run_id,
            barrier_ref=typed_checkpoint.cross_owner_barrier_ref,
            module_refs=module_refs,
        )
        try:
            _, cross_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=cross_ref,
                lifecycle="cross",
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key={"cross-module-reviewer", "cross-owner-wave"},
                subject_refs=module_refs,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay Cross review because its current-run "
                f"completion is invalid: {cross_ref}: {exc}"
            ) from exc
        state["cross_review_completion_ref"] = cross_ref
        state["cross_synthesis_inputs"] = self._latest_cross_synthesis(cross_artifacts)

        if typed_checkpoint.cross_decision_pack_ref is not None:
            if typed_checkpoint.cross_decision_pack_sha256 is None:
                raise AgentWorkflowError(
                    "checkpoint CrossDecisionPack is missing its hash"
                )
            state["cross_decision_pack_ref"] = require_run_ref(
                typed_checkpoint.cross_decision_pack_ref,
                label="CrossDecisionPack",
            )
            state["cross_decision_pack_sha256"] = (
                typed_checkpoint.cross_decision_pack_sha256
            )
            self._load_current_cross_decision_pack(state)
        elif typed_checkpoint.chief_editor_completion_ref is not None:
            raise AgentWorkflowError(
                "checkpoint has Chief completion without CrossDecisionPack binding"
            )

        final_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        if (
            not (self.service.workspace / final_ref).is_file()
            and typed_checkpoint.chief_editor_completion_ref is not None
        ):
            candidate, envelope, chief_refs = (
                self._load_chief_editor_completion(
                    state,
                    typed_checkpoint.chief_editor_completion_ref,
                )
            )
            state["edited_report"] = candidate
            state["chief_candidate_ref"] = chief_refs["candidate"]
            state["chief_editor_input_ref"] = chief_refs["editor_input"]
            state["chief_editor_envelope_ref"] = chief_refs["envelope"]
            state["chief_editor_completion_ref"] = (
                typed_checkpoint.chief_editor_completion_ref
            )
            state["chief_editor_envelope"] = envelope
            state["chief_editor_session_key"] = "chief-editor"
            state["approved_module_text"] = {
                module_id: self._approved_module_text(approved_subjects[module_id])
                for module_id in REPORT_MODULE_IDS
            }

        if not (self.service.workspace / final_ref).is_file():
            return
        try:
            final_completion, final_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=final_ref,
                lifecycle="final",
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key="chief-editor-auditor",
            )
            if len(final_completion.subject_refs) != 1:
                raise ValueError("final completion requires exactly one edited subject")
            edited_path = self.service.workspace / final_completion.subject_refs[0]
            edited = EditedReportSubmission.model_validate_json(
                edited_path.read_text(encoding="utf-8")
            )
            claims = [
                claim
                for module_id in REPORT_MODULE_IDS
                for claim in approved_subjects[module_id].claims
            ]
            validate_editor_protection(edited, claims)
            state["editor_quality_observations"] = validate_editor_quality(
                edited, approved_subjects
            )
            validate_final_report_markdown(
                self._canonical_markdown(edited),
                edited.special_topic_plan,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay final review because its current-run "
                f"completion is invalid: {final_ref}: {exc}"
            ) from exc
        state["edited_report"] = edited
        state["approved_module_text"] = {
            module_id: self._approved_module_text(approved_subjects[module_id])
            for module_id in REPORT_MODULE_IDS
        }
        state["final_review_completion_ref"] = final_ref
        snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        if (self.service.workspace / snapshot_ref).is_file():
            state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = self._latest_final_residual_risks(final_artifacts)
        self._restore_delivery_completion(state)

    def _restore_delivery_completion(self, state: dict) -> None:
        """Restore a receipt and, when needed, retry only its archive boundary."""

        run_id = state["run_id"]
        completion_ref = f"Work/runs/{run_id}/delivery-completion.json"
        completion_path = self.service.workspace / completion_ref
        if not completion_path.is_file():
            return
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
        if payload.get("run_id") != run_id:
            raise AgentWorkflowError("delivery completion identity/status is invalid")
        status = str(payload.get("status", ""))
        # ``completed`` is the pre-archive spelling and remains read-compatible.
        legacy_completed = status == "completed"
        if status not in {
            "receipt_persisted",
            "delivered",
            "archive_pending",
            "archived",
            "archive_failed",
            "completed",
        }:
            raise AgentWorkflowError("delivery completion identity/status is invalid")

        receipt_ref = str(payload.get("delivery_receipt_ref", ""))
        run_prefix = f"Work/runs/{run_id}/"
        if not receipt_ref.startswith(run_prefix):
            raise AgentWorkflowError("delivery receipt is outside the current run")
        receipt_path = self.service.workspace / receipt_ref
        if not receipt_path.is_file():
            raise AgentWorkflowError("delivery completion references a missing receipt")
        try:
            receipt = DeliveryReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
            verify_current_run_outputs(
                self.service.workspace,
                run_id,
                [receipt.final_docx, receipt.source_index, receipt.source_index_docx],
                0,
                allow_existing_artifacts=True,
            )
        except (OSError, ValueError, json.JSONDecodeError, OutputVerificationError) as exc:
            # Preserve the old behavior for obsolete ``completed`` records: a
            # malformed completion is left for deterministic regeneration.
            if legacy_completed:
                return
            raise AgentWorkflowError(
                f"delivery receipt failed current-run validation: {receipt_ref}: {exc}"
            ) from exc

        artifacts = [
            OutputArtifact.model_validate(item)
            for item in payload.get("output_artifacts", [])
        ]
        expected_artifacts = self._delivery_output_artifacts(
            final_review_ref=state.get(
                "final_review_completion_ref",
                f"Work/runs/{run_id}/reviews/final-completion.json",
            ),
            delivery_manifest_ref=receipt.manifest_path.resolve().relative_to(
                self.service.workspace
            ),
            source_index_ref=receipt.source_index.resolve().relative_to(
                self.service.workspace
            ),
            source_index_docx_ref=receipt.source_index_docx.resolve().relative_to(
                self.service.workspace
            ),
        )
        if not artifacts:
            if legacy_completed:
                return
            artifacts = expected_artifacts
        elif artifacts != expected_artifacts:
            if legacy_completed:
                return
            raise AgentWorkflowError("delivery completion output declaration is stale")

        version = None
        version_id = payload.get("report_version_id")
        if version_id:
            try:
                version = ReportVersionStore(self.service.workspace).load(str(version_id))
                if version.run_id != run_id:
                    raise ValueError("report version belongs to another run")
            except (OSError, ValueError, FileNotFoundError):
                # A valid receipt remains delivered even when version publication
                # was interrupted; archive-only recovery must not invoke Provider.
                version = None
        if version is not None:
            state["report_version"] = version

        if status in {"receipt_persisted", "delivered", "archive_pending", "archive_failed"}:
            try:
                storage_plan = ReportingRetentionPlanner(self.service.workspace).generate()
                state["storage_usage_ref"] = "Work/storage-usage.json"
                state["retention_plan_ref"] = "Work/retention-plan.json"
                state["storage_usage"] = storage_plan["usage"]
                payload["status"] = "archived"
                payload["delivery_status"] = "archived"
                payload.pop("warning", None)
                self.service.store.write_json(completion_ref, payload)
            except Exception as exc:
                payload["status"] = "archive_failed"
                payload["delivery_status"] = "delivered_with_archive_warning"
                payload["warning"] = str(exc)
                self.service.store.write_json(completion_ref, payload)
                state["delivery_status"] = "delivered_with_archive_warning"
        else:
            state["delivery_status"] = payload.get("delivery_status", "archived")
        state["output_artifacts"] = artifacts
        state["delivery_completion_ref"] = completion_ref
        # Receipt/hash validation above proves these artifacts belong to this
        # exact run. A later resume may reuse them without regenerating work.
        state["delivery_restored"] = True

    def _revision_checkpoint(
        self,
        state: dict,
        activity: str,
        status: str,
        error: str | None = None,
    ) -> None:
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            {
                "workflow_id": f"report-revision:{state['run_id']}",
                "run_id": state["run_id"],
                "activity": activity,
                "status": status,
                "completed_revision_modules": sorted(state.get("completed_revision_modules", [])),
                "cross_review_completed": "cross_review_completion_ref" in state,
                "module_review_completion_refs": dict(
                    state.get("module_review_completion_refs", {})
                ),
                "cross_review_completion_ref": state.get("cross_review_completion_ref"),
                "cross_decision_pack_ref": state.get("cross_decision_pack_ref"),
                "cross_decision_pack_sha256": state.get(
                    "cross_decision_pack_sha256"
                ),
                "chief_candidate_ref": state.get("chief_candidate_ref"),
                "chief_editor_input_ref": state.get(
                    "chief_editor_input_ref"
                ),
                "chief_editor_envelope_ref": state.get(
                    "chief_editor_envelope_ref"
                ),
                "chief_editor_completion_ref": state.get(
                    "chief_editor_completion_ref"
                ),
                "final_review_completed": "final_review_completion_ref" in state,
                "final_review_completion_ref": state.get("final_review_completion_ref"),
                "error": error,
                "budget": self._budget.snapshot() if self._budget is not None else None,
                "pending_cost_boundary_id": state.get(
                    "pending_cost_boundary_id"
                ),
            },
        )

    def _restore_revision_resume_state(self, state: dict) -> None:
        """Restore only subjects closed by the new review completion contract."""

        run_id = state["run_id"]
        checkpoint_path = self.service.workspace / f"Work/runs/{run_id}/workflow-state.json"
        if not checkpoint_path.is_file():
            return
        checkpoint = json.loads(checkpoint_path.read_text(encoding="utf-8"))
        if checkpoint.get("run_id") != run_id:
            raise AgentWorkflowError("revision resume checkpoint belongs to another run")
        checkpoint_module_review_refs = dict(
            checkpoint.get("module_review_completion_refs", {})
        )
        restored: list[str] = []
        for module_id in checkpoint.get("completed_revision_modules", []):
            baseline = state["module_submissions"].get(module_id)
            if baseline is None:
                continue
            candidates: list[ModuleSubmission] = []
            modules_root = self.service.workspace / f"Work/runs/{run_id}/modules"
            for path in modules_root.glob(f"{module_id}-r*.json"):
                try:
                    candidate = ModuleSubmission.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if candidate.revision > baseline.revision:
                    candidates.append(candidate)
            for candidate in sorted(candidates, key=lambda item: item.revision, reverse=True):
                subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r{candidate.revision}.json"
                completion_ref = checkpoint_module_review_refs.get(module_id)
                if not completion_ref:
                    continue
                completion_path = self.service.workspace / completion_ref
                if not completion_path.is_file():
                    continue
                parts = Path(completion_ref).parts
                try:
                    lifecycle_id = parts[parts.index("module") + 1]
                except (ValueError, IndexError) as exc:
                    raise AgentWorkflowError(
                        f"invalid module completion path: {completion_ref}"
                    ) from exc
                try:
                    self._load_current_review_completion(
                        run_id=run_id,
                        completion_ref=completion_ref,
                        lifecycle="module",
                        reviewer_agent_id="evidence-auditor",
                        reviewer_session_key=self._module_reviewer_session_keys(
                            module_id,
                            lifecycle_id,
                        ),
                        subject_refs=[subject_ref],
                    )
                except (OSError, ValueError, json.JSONDecodeError) as exc:
                    raise AgentWorkflowError(
                        "refusing to replay a revision whose current-run review "
                        f"completion is invalid: {completion_ref}: {exc}"
                    ) from exc
                state["module_submissions"][module_id] = candidate
                state.setdefault("module_review_completion_refs", {})[module_id] = completion_ref
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", candidate.markdown)
                restored.append(module_id)
                break
        state["completed_revision_modules"] = restored
        if set(state["module_submissions"]) != set(REPORT_MODULE_IDS):
            return
        module_refs = [
            (
                f"Work/runs/{run_id}/modules/"
                f"{module_id}-r{state['module_submissions'][module_id].revision}.json"
            )
            for module_id in REPORT_MODULE_IDS
        ]
        cross_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
        if not (self.service.workspace / cross_ref).is_file():
            return
        self._verify_resume_cross_owner_barrier(
            run_id=run_id,
            barrier_ref=checkpoint.get("cross_owner_barrier_ref"),
            module_refs=module_refs,
        )
        try:
            _, cross_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=cross_ref,
                lifecycle="cross",
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key={"cross-module-reviewer", "cross-owner-wave"},
                subject_refs=module_refs,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay Cross revision because its current-run "
                f"completion is invalid: {cross_ref}: {exc}"
            ) from exc
        state["cross_review_completion_ref"] = cross_ref
        state["cross_synthesis_inputs"] = self._latest_cross_synthesis(cross_artifacts)
        checkpoint_pack_ref = checkpoint.get("cross_decision_pack_ref")
        checkpoint_pack_hash = checkpoint.get("cross_decision_pack_sha256")
        if checkpoint_pack_ref is not None:
            if not checkpoint_pack_hash:
                raise AgentWorkflowError(
                    "revision checkpoint CrossDecisionPack is missing its hash"
                )
            state["cross_decision_pack_ref"] = str(checkpoint_pack_ref)
            state["cross_decision_pack_sha256"] = str(checkpoint_pack_hash)
            self._load_current_cross_decision_pack(state)
        elif checkpoint.get("chief_editor_completion_ref") is not None:
            raise AgentWorkflowError(
                "revision checkpoint has Chief completion without CrossDecisionPack binding"
            )
        final_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        completion_ref = checkpoint.get("chief_editor_completion_ref")
        if (
            not (self.service.workspace / final_ref).is_file()
            and completion_ref is not None
        ):
            candidate, envelope, chief_refs = (
                self._load_chief_editor_completion(
                    state,
                    str(completion_ref),
                )
            )
            state["edited_report"] = candidate
            state["chief_candidate_ref"] = chief_refs["candidate"]
            state["chief_editor_input_ref"] = chief_refs["editor_input"]
            state["chief_editor_envelope_ref"] = chief_refs["envelope"]
            state["chief_editor_completion_ref"] = str(
                completion_ref
            )
            state["chief_editor_envelope"] = envelope
            state["chief_editor_session_key"] = "chief-editor"
            state["approved_module_text"] = {
                module_id: self._approved_module_text(
                    state["module_submissions"][module_id]
                )
                for module_id in REPORT_MODULE_IDS
            }
        if not (self.service.workspace / final_ref).is_file():
            return
        try:
            final_completion, final_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=final_ref,
                lifecycle="final",
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key="chief-editor-auditor",
            )
            if len(final_completion.subject_refs) != 1:
                raise ValueError("final completion requires exactly one edited subject")
            edited = EditedReportSubmission.model_validate_json(
                (self.service.workspace / final_completion.subject_refs[0]).read_text(
                    encoding="utf-8"
                )
            )
            claims = [
                claim
                for module_id in REPORT_MODULE_IDS
                for claim in state["module_submissions"][module_id].claims
            ]
            validate_editor_protection(edited, claims)
            state["editor_quality_observations"] = validate_editor_quality(
                edited, state["module_submissions"]
            )
            validate_final_report_markdown(
                self._canonical_markdown(edited),
                edited.special_topic_plan,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay final revision because its current-run "
                f"completion is invalid: {final_ref}: {exc}"
            ) from exc
        state["edited_report"] = edited
        state["final_review_completion_ref"] = final_ref
        snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        if (self.service.workspace / snapshot_ref).is_file():
            state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = self._latest_final_residual_risks(final_artifacts)

    async def _prepare(self, state: dict) -> None:
        # Preparation is immutable inside one run. A resumed run must never
        # silently ingest a newer version of Inputs.
        input_snapshot = RunInputSnapshotStore(self.service.workspace).load(
            state["run_id"]
        )
        state["input_snapshot_ref"] = (
            f"Work/runs/{state['run_id']}/input-snapshot.json"
        )
        state["input_snapshot_digest"] = input_snapshot.inventory_digest
        if state.get("resume"):
            self._restore_preparation_snapshot(state)
        else:
            # These are deterministic data transformations, deliberately not LLM personas.
            await self.service._build_manifest(state)
            await self.service._parse_artifacts(state)
            await self.service._normalize_evidence(state)
            await self.service._evaluate_coverage(state)
            if state["request"].operation == "full_report":
                special_topic_plan = load_special_topic_plan(self.service.workspace)
                if special_topic_plan is not None:
                    state["special_topic_plan"] = special_topic_plan
            self._persist_preparation_snapshot(state)
        evidence_index = ProjectEvidenceIndex(
            self.service.workspace, state["run_id"]
        )
        evidence_index.items()
        evidence_index_ref = evidence_index.snapshot_manifest_ref()
        if evidence_index_ref is not None:
            state["evidence_index_ref"] = evidence_index_ref.as_posix()
        ledger = SourceLedger(self.service.workspace, state["run_id"])
        ledger.register_many(
            [
                {
                    "kind": "project_evidence",
                    "evidence_id": item.id,
                    "title": item.subject,
                    "locator": project_evidence_locator(item),
                    "content": item.model_dump_json(),
                }
                for item in state.get("evidence_items", [])
            ]
        )
        if not ledger.path.is_file():
            self.service.store.write_json(
                ledger.path.relative_to(self.service.workspace).as_posix(), []
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _preparation_refs(
        self,
        run_id: str,
        *,
        include_special_topics: bool = False,
    ) -> dict[str, str]:
        root = f"Work/runs/{run_id}/preparation"
        refs = {
            "manifest": f"{root}/manifest.json",
            "evidence": f"{root}/evidence.jsonl",
            "photo_manifest": f"{root}/photo-manifest.json",
            "photo_adjacency": f"{root}/photo-evidence-adjacency.json",
            "mapping_gaps": f"{root}/mapping-gaps.json",
            "coverage": f"{root}/coverage.json",
        }
        if include_special_topics:
            refs["special_topic_plan"] = f"{root}/special-topic-plan.json"
        return refs

    def _persist_preparation_snapshot(self, state: dict) -> None:
        refs = self._preparation_refs(
            state["run_id"],
            include_special_topics="special_topic_plan" in state,
        )
        run_root = self.service.workspace / "Work" / "runs" / state["run_id"]
        final_root = run_root / "preparation"
        completion_ref = f"Work/runs/{state['run_id']}/preparation/completion.json"
        if final_root.exists() or (self.service.workspace / completion_ref).exists():
            raise AgentWorkflowError(
                "immutable preparation snapshot already exists; refusing partial overwrite"
            )
        staging_name = f".preparation-{uuid4().hex}"
        staging_root = run_root / staging_name
        staging_preparation = staging_root / "preparation"
        staging_root.mkdir(parents=True, exist_ok=False)

        def stage_ref(name: str) -> str:
            filename = Path(refs[name]).name
            return (
                staging_preparation / filename
            ).relative_to(self.service.workspace).as_posix()

        try:
            self.service.store.write_json(
                stage_ref("manifest"),
                state["project_manifest"].model_dump(mode="json"),
            )
            self.service.store.write_jsonl(
                stage_ref("evidence"),
                [item.model_dump(mode="json") for item in state["evidence_items"]],
            )
            self.service.store.write_json(
                stage_ref("photo_manifest"),
                {
                    "assets": [
                        item.model_dump(mode="json")
                        for item in state["photo_assets"]
                    ]
                },
            )
            self.service.store.write_json(
                stage_ref("photo_adjacency"),
                state.get(
                    "photo_evidence_adjacency",
                    {
                        "schema_version": 1,
                        "photo_to_evidence": {},
                        "evidence_to_photo": {},
                        "primary_evidence": {},
                    },
                ),
            )
            self.service.store.write_json(
                stage_ref("mapping_gaps"), {"gaps": state["mapping_gaps"]}
            )
            self.service.store.write_json(
                stage_ref("coverage"),
                state["coverage_matrix"].model_dump(mode="json"),
            )
            if "special_topic_plan" in refs:
                self.service.store.write_json(
                    stage_ref("special_topic_plan"),
                    state["special_topic_plan"].model_dump(mode="json"),
                )
            hashes = {
                name: self._sha256(self.service.workspace / stage_ref(name))
                for name in refs
            }
            completion = {
                "schema_version": 1,
                "run_id": state["run_id"],
                "status": "completed",
                "input_snapshot_ref": state.get("input_snapshot_ref"),
                "input_snapshot_digest": state.get("input_snapshot_digest"),
                "preparation_refs": refs,
                "preparation_sha256": hashes,
                "manifest_order": [
                    item.id for item in state["project_manifest"].files
                ],
                "reducer_order": state.get("preparation_parallelism", {}).get(
                    "reducer_order",
                    [item.id for item in state["project_manifest"].files],
                ),
                "evidence_count": len(state["evidence_items"]),
                "photo_count": len(state["photo_assets"]),
            }
            staged_completion = staging_preparation / "completion.json"
            self.service.store.write_json(
                staged_completion.relative_to(self.service.workspace).as_posix(),
                completion,
            )
            os.replace(staging_preparation, final_root)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)
        state["preparation_refs"] = refs
        state["preparation_sha256"] = hashes
        state["preparation_completion_ref"] = completion_ref

    def _restore_preparation_snapshot(self, state: dict) -> None:
        checkpoint_path = (
            self.service.workspace / f"Work/runs/{state['run_id']}/workflow-state.json"
        )
        checkpoint = FullReportCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
        refs = self._preparation_refs(
            state["run_id"],
            include_special_topics="special_topic_plan" in checkpoint.preparation_refs,
        )
        missing = [ref for ref in refs.values() if not (self.service.workspace / ref).is_file()]
        if missing:
            raise AgentWorkflowError(
                f"resume requires a complete immutable preparation snapshot; missing={missing}"
            )
        if checkpoint.preparation_refs != refs:
            raise AgentWorkflowError(
                "checkpoint preparation refs do not match this run's canonical snapshot"
            )
        actual_hashes = {
            name: self._sha256(self.service.workspace / ref) for name, ref in refs.items()
        }
        if checkpoint.preparation_sha256 != actual_hashes:
            raise AgentWorkflowError(
                "immutable preparation snapshot hash mismatch; refusing to resume"
            )
        completion_ref = f"Work/runs/{state['run_id']}/preparation/completion.json"
        completion_path = self.service.workspace / completion_ref
        if not completion_path.is_file():
            raise AgentWorkflowError(
                "resume requires immutable preparation completion"
            )
        try:
            completion = json.loads(completion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "immutable preparation completion is unreadable"
            ) from exc
        if (
            completion.get("schema_version") != 1
            or completion.get("run_id") != state["run_id"]
            or completion.get("status") != "completed"
            or completion.get("preparation_refs") != refs
            or completion.get("preparation_sha256") != actual_hashes
            or completion.get("input_snapshot_ref")
            != state.get("input_snapshot_ref")
            or completion.get("input_snapshot_digest")
            != state.get("input_snapshot_digest")
        ):
            raise AgentWorkflowError(
                "immutable preparation completion does not bind the snapshot"
            )
        manifest_path = self.service.workspace / refs["manifest"]
        evidence_path = self.service.workspace / refs["evidence"]
        photo_path = self.service.workspace / refs["photo_manifest"]
        photo_adjacency_path = self.service.workspace / refs["photo_adjacency"]
        gaps_path = self.service.workspace / refs["mapping_gaps"]
        coverage_path = self.service.workspace / refs["coverage"]
        state["project_manifest"] = ProjectManifest.model_validate_json(
            manifest_path.read_text(encoding="utf-8")
        )
        state["evidence_items"] = [
            EvidenceItem.model_validate_json(line)
            for line in evidence_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        photo_payload = json.loads(photo_path.read_text(encoding="utf-8"))
        state["photo_assets"] = [
            PhotoAsset.model_validate(item) for item in photo_payload.get("assets", [])
        ]
        state["photo_evidence_adjacency"] = json.loads(
            photo_adjacency_path.read_text(encoding="utf-8")
        )
        state["mapping_gaps"] = json.loads(gaps_path.read_text(encoding="utf-8")).get("gaps", [])
        state["coverage_matrix"] = CoverageMatrix.model_validate_json(
            coverage_path.read_text(encoding="utf-8")
        )
        if "special_topic_plan" in refs:
            special_topic_path = self.service.workspace / refs["special_topic_plan"]
            state["special_topic_plan"] = SpecialTopicPlan.model_validate_json(
                special_topic_path.read_text(encoding="utf-8")
            )
        state["preparation_refs"] = refs
        state["preparation_sha256"] = actual_hashes
        state["preparation_completion_ref"] = completion_ref

    def _build_module_dispatch(
        self, state: dict, module_ids: tuple[str, ...]
    ) -> ModuleDispatchPlan:
        """Build Main's deterministic fixed-module dispatch."""

        request = state["request"]
        knowledge = KnowledgeContextBuilder(self.service.workspace, state["run_id"])
        if not self._load_template_skill(state):
            raise AgentWorkflowError(
                "fixed template-writing Skill must exist before module dispatch"
            )
        module_knowledge = {
            module_id: knowledge.build_module(module_id) for module_id in module_ids
        }
        state["module_knowledge_refs"] = {
            module_id: context.path.as_posix() for module_id, context in module_knowledge.items()
        }
        knowledge_snapshot_refs = sorted(
            {
                context.snapshot_ref.as_posix()
                for context in module_knowledge.values()
                if context.snapshot_ref is not None
            }
        )
        state["knowledge_snapshot_refs"] = knowledge_snapshot_refs
        preparation_refs = state["preparation_refs"]
        tasks = [
            TaskEnvelope(
                task_id=f"module-{module_id}",
                run_id=state["run_id"],
                agent_id=f"module-{module_id}-specialist",
                objective=f"完成报告模块 {module_id}。用户要求：{request.instruction}",
                input_refs=[
                    preparation_refs["coverage"],
                    preparation_refs["evidence"],
                    preparation_refs["manifest"],
                    *(
                        [state["preparation_completion_ref"]]
                        if state.get("preparation_completion_ref")
                        else []
                    ),
                    state["module_knowledge_refs"][module_id],
                    *knowledge_snapshot_refs,
                    *(
                        [state["evidence_index_ref"]]
                        if state.get("evidence_index_ref")
                        else []
                    ),
                ],
                constraints=[
                    f"仅分析目标模块 {module_id}",
                    "叶子任务内联当前叶子的 Knowledge 增量与核心写作方法；整模块 Knowledge 和完整写作 Skill 以共享引用提供，仅在增量不足时按需读取",
                    "R-* 是优先参考而非认知边界；可使用模型世界知识解释机理、备选原因和行业实践，但不能把它补成客户事实",
                    "每个固定子模块必须形成带标题的完整正文，至少包含适用的现状、结论、风险机理和可执行建议",
                    (
                        "固定 taxonomy 是唯一合法的数字标题体系；正文只允许使用本任务"
                        " required_submodule_ids 中的编号标题。现状、判断、原因、风险机理、"
                        "建议和验证只能使用普通段落或无编号粗体标签，禁止自行生成下一级编号"
                    ),
                    f"缺失证据策略={request.missing_evidence_policy}",
                    *self._evidence_policy_constraints(request.missing_evidence_policy),
                    *request.execution_requirements,
                    *self._user_supplement_constraints(
                        state,
                        stage="module_authoring",
                        target_ids={
                            module_id,
                            *REPORT_TAXONOMY[module_id].submodules,
                        },
                    ),
                ],
                allowed_outputs=["module_submission"],
                target_submodule_ids=list(REPORT_TAXONOMY[module_id].submodules),
                inline_context=self._module_author_inline_context(state, module_id),
            )
            for module_id in module_ids
        ]
        dispatch = ModuleDispatchPlan(
            module_tasks=tasks,
            rationale="Main 根据固定报告 taxonomy 和用户目标直接分配模块任务。",
        )
        self.service.store.write_run_model(
            state["run_id"],
            "workflow/module-dispatch.json",
            dispatch,
        )
        return dispatch

    def _write_handoff_contracts(self, state: dict) -> Path:
        """Persist the producer/consumer contract matrix used by this exact run."""
        run_id = state["run_id"]
        contracts = [
            {
                "stage": "template-skill-read",
                "producer": "separate template-distiller action",
                "consumer": (
                    "module specialists/auditors, cross-module reviewer, chief editor, "
                    "final auditor"
                ),
                "input": (
                    "hash-verified Work/report-template-writing files projected by role "
                    "and embedded once in task inline_context"
                ),
                "output": (
                    "role-scoped reusable Skill guidance, including fact-free worked examples; "
                    "downstream references are not opened"
                ),
                "content_checks": [
                    "only analysis, synthesis, visual, and quality-check methods",
                    "no domain knowledge, standards/thresholds, project facts, or identifiers",
                    "writing run never reads or distills the template DOCX",
                ],
            },
            {
                "stage": "dispatch",
                "producer": "main-agent",
                "consumer": "module-2.x-specialist",
                "input": "ModuleAuthoringInput + TaskEnvelope",
                "output": "ModuleSubmission",
                "content_checks": [
                    "fixed module and submodule taxonomy",
                    "all Claim source_ids declared by module",
                    "all sources exist in SourceLedger",
                ],
            },
            {
                "stage": "module-validation",
                "producer": "module-2.x-specialist + deterministic validator + evidence-auditor",
                "consumer": "module specialist and downstream workflow",
                "input": "ModuleReviewInput + exact ModuleSubmission + SourceLedger",
                "output": (
                    "ValidationReport + ModuleReviewFinding + RevisionResponse + "
                    "ResolutionVerdict + ReviewCompletionRecord"
                ),
                "content_checks": [
                    "module_id and fixed taxonomy match",
                    "every Claim source_id exists in SourceLedger",
                    "typed module result is persisted before advancing",
                    "keyword/length observations are non-binding ValidationReport signals",
                    "module quality, gaps, evidence validity, inference boundaries, and action closure are judged by the module auditor",
                    "every finding triggers an explicit author response and same-reviewer verdict",
                    "Main enters only for reviewer verdict=escalate",
                ],
            },
            {
                "stage": "cross-review",
                "producer": "five independently reviewed module pipelines",
                "consumer": "cross-module-reviewer",
                "input": "CrossReviewInput with five exact ModuleSubmission artifacts",
                "output": (
                    "coverage + CrossReviewFinding + CrossSynthesisInput + "
                    "original-reviewer ResolutionVerdict"
                ),
                "content_checks": [
                    "cross-module terminology, facts, risk levels, dependencies, propagation, and joint verification only",
                    "no routine re-audit of module-local prose, evidence sufficiency, or image binding",
                    "owner specialist writes back each finding",
                    "module auditor checks only local regression",
                    "the original Cross reviewer alone closes Cross findings",
                ],
            },
            {
                "stage": "synthesis",
                "producer": "chief-editor",
                "consumer": "main-agent",
                "input": (
                    "ChiefEditorInput + project Evidence/photo manifest; Claim/Source ledgers "
                    "remain runtime-only"
                ),
                "output": "EditedReportSubmission + canonical Markdown",
                "content_checks": [
                    "exactly modules 2.1-2.5",
                    "all fixed submodule ids and titles retained",
                    "every approved submodule narrative is deterministically preserved verbatim",
                    "only the currently defined Chapter 1 and Chapter 3 sections are authored",
                    "dynamic Chapter 4 headings and requirements exactly match the immutable Inputs plan",
                    "approved Claim semantics protected",
                    "approved Claim markers preserved exactly once",
                ],
            },
            {
                "stage": "final-review",
                "producer": "chief-editor",
                "consumer": "chief-editor-auditor",
                "input": "FinalReviewInput with exact EditedReportSubmission and canonical Markdown",
                "output": (
                    "FinalReviewFinding + RevisionResponse + ResolutionVerdict + "
                    "ReviewCompletionRecord"
                ),
                "content_checks": [
                    "all currently defined final sections checked",
                    "approved module prose and Claim semantics retained",
                    "only current report sections are reviewed; deleted legacy sections are not reconstructed",
                    "citation, table, image, action, and residual-risk presentation ready for delivery",
                    "every finding triggers scoped chief-editor response and original-reviewer verdict",
                ],
            },
            {
                "stage": "render",
                "producer": "final review completion record",
                "consumer": "deterministic DOCX renderer",
                "input": "RenderRequest(source_markdown_ref, template_ref, output_ref)",
                "output": "RenderResult + readable DOCX",
                "content_checks": [
                    "canonical Markdown exists and is non-empty",
                    "template and DOCX SHA-256 recorded",
                    "current-run delivery receipt matches",
                ],
            },
        ]
        return self.service.store.write_json(
            f"Work/runs/{run_id}/handoff-contracts.json", contracts
        )

    @staticmethod
    def _evidence_policy_constraints(policy: str) -> list[str]:
        if policy == "draft":
            return [
                "缺少客户证据的内容必须明确标注“资料不完整、待核实、低置信度”或等价限制，"
                "禁止写成已确认项目事实；不得因此跳过固定模块或子模块"
            ]
        if policy == "skip":
            return ["必须保留固定报告目录；缺少客户证据的子模块仅标注“未评估”，不得给出专业结论"]
        return []

    def _inherit_module_result_parts(
        self,
        run_id: str,
        module_id: str,
        from_revision: int,
        to_revision: int,
    ) -> list[str]:
        """Seed a revision with untouched durable parts from its parent revision."""
        if from_revision < 0 or to_revision <= from_revision:
            return []
        draft_base = self.service.workspace / (f"Work/runs/{run_id}/drafts/module-{module_id}")
        source_root = draft_base / f"r{from_revision}"
        target_root = draft_base / f"r{to_revision}"
        if not source_root.is_dir():
            return []
        inherited: list[str] = []
        target_root.mkdir(parents=True, exist_ok=True)
        for submodule_id in REPORT_TAXONOMY[module_id].submodules:
            source = source_root / f"{submodule_id}.md"
            target = target_root / f"{submodule_id}.md"
            if source.is_file() and not target.exists():
                shutil.copy2(source, target)
                inherited.append(submodule_id)
        return inherited

    def _lane_task_spec(self, state: dict, module_id: str) -> LaneTaskSpec:
        preparation_inputs = []
        for name, ref in sorted(state.get("preparation_refs", {}).items()):
            path = (self.service.workspace / ref).resolve()
            preparation_inputs.append(
                {
                    "name": name,
                    "ref": ref,
                    "sha256": self._sha256(path) if path.is_file() else None,
                }
            )
        preparation_sha256 = hashlib.sha256(
            json.dumps(
                preparation_inputs,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        semantic_key = hashlib.sha256(
            json.dumps(
                {
                    "run_id": state["run_id"],
                    "module_id": module_id,
                    "preparation_sha256": preparation_sha256,
                    "authoring_context_sha256": self._module_authoring_context_sha256(
                        state, module_id
                    ),
                    "schema_version": "2",
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return LaneTaskSpec(
            lane_id=f"module-{module_id}",
            run_id=state["run_id"],
            module_id=module_id,
            semantic_key=semantic_key,
            preparation_sha256=preparation_sha256,
            collaboration_bundle_sha256=None,
        )

    def _artifact_ref(self, ref: str) -> ArtifactRef:
        path = (self.service.workspace / ref).resolve()
        if not path.is_relative_to(self.service.workspace) or not path.is_file():
            raise AgentWorkflowError(f"lane artifact is missing: {ref}")
        content = path.read_bytes()
        active_lease = current_bound_project_write_lease(self.service.workspace)
        return ArtifactRef(
            ref=ref,
            sha256=hashlib.sha256(content).hexdigest(),
            size=len(content),
            media_type="application/json",
            project_lease_epoch=(
                active_lease.lease_epoch if active_lease is not None else None
            ),
        )

    def _verify_artifact_ref(self, artifact: ArtifactRef) -> None:
        current = self._artifact_ref(artifact.ref)
        if current.sha256 != artifact.sha256 or current.size != artifact.size:
            raise AgentWorkflowError(
                f"lane artifact identity mismatch: {artifact.ref}"
            )

    def _recover_module_lane(
        self,
        module_id: str,
        state: dict,
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict] | None:
        """Recover a verified completion without running its Agent lifecycle again."""

        spec = self._lane_task_spec(state, module_id)
        lane_root = (
            self.service.workspace
            / "Work"
            / "runs"
            / state["run_id"]
            / "lanes"
            / f"module-{module_id}"
        )
        matching: list[tuple[str, LaneCompletion]] = []
        for path in sorted(lane_root.glob("completion-r*.json")):
            try:
                completion = LaneCompletion.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise AgentWorkflowError(
                    f"module lane completion is invalid: {module_id}"
                ) from exc
            if completion.semantic_key != spec.semantic_key:
                continue
            if (
                completion.run_id != state["run_id"]
                or completion.module_id != module_id
                or completion.lane_id != spec.lane_id
            ):
                raise AgentWorkflowError(
                    f"module lane completion ownership mismatch: {module_id}"
                )
            matching.append(
                (path.relative_to(self.service.workspace).as_posix(), completion)
            )
        if not matching:
            return None
        if len(matching) != 1:
            raise AgentWorkflowError(
                f"module lane has duplicate semantic completions: {module_id}"
            )
        completion_ref, completion = matching[0]
        self._verify_artifact_ref(completion.subject)
        self._verify_artifact_ref(completion.review_completion)
        try:
            submission = ModuleSubmission.model_validate_json(
                (self.service.workspace / completion.subject.ref).read_text(
                    encoding="utf-8"
                )
            )
        except (OSError, ValueError) as exc:
            raise AgentWorkflowError(
                f"module lane subject is invalid: {module_id}"
            ) from exc
        if submission.module_id != module_id:
            raise AgentWorkflowError(
                f"module lane subject ownership mismatch: {module_id}"
            )
        lane_state = deepcopy(state)
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            completion.review_completion.ref
        )
        return submission, completion_ref, completion, lane_state

    def _build_lane_completion(
        self,
        state: dict,
        module_id: str,
        submission: ModuleSubmission,
        spec: LaneTaskSpec,
    ) -> tuple[str, LaneCompletion]:
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module_id}-r{submission.revision}.json"
        )
        review_ref = state.get("module_review_completion_refs", {}).get(module_id)
        if not review_ref:
            raise AgentWorkflowError(
                f"module lane lacks review completion: {module_id}"
            )
        try:
            review_record = json.loads(
                (self.service.workspace / review_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise AgentWorkflowError(
                f"module lane review completion is invalid: {module_id}"
            ) from exc
        reviewer_identity_key = str(
            review_record.get("reviewer_session_key") or f"module-auditor-{module_id}"
        )
        registry_path = (
            self.service.workspace
            / f"Work/runs/{state['run_id']}/agent-identities.json"
        )
        try:
            identities = json.loads(
                registry_path.read_text(encoding="utf-8")
            ).get("identities", {})
        except (OSError, ValueError, AttributeError):
            identities = {}
        reviewer_session_id = str(
            identities.get(reviewer_identity_key, {}).get("session_id")
            or reviewer_identity_key
        )
        author_attempt = TaskAttemptStore(
            self.service.workspace, state["run_id"]
        ).current(f"module-{module_id}")
        author_task_attempt_id = (
            author_attempt.task_attempt_id
            if author_attempt is not None
            else f"recovered-module-{module_id}-r{submission.revision}"
        )
        lease_epoch = author_attempt.lease_epoch if author_attempt is not None else 1
        completion = LaneCompletion(
            lane_id=spec.lane_id,
            run_id=state["run_id"],
            module_id=module_id,
            semantic_key=spec.semantic_key,
            subject=self._artifact_ref(subject_ref),
            review_completion=self._artifact_ref(review_ref),
            author_task_attempt_id=author_task_attempt_id,
            reviewer_session_id=reviewer_session_id,
            lease_epoch=lease_epoch,
        )
        completion_ref = (
            f"Work/runs/{state['run_id']}/lanes/module-{module_id}/"
            f"completion-r{submission.revision}.json"
        )
        completion_path = self.service.workspace / completion_ref
        payload = completion.model_dump(mode="json")
        if completion_path.is_file():
            try:
                existing = LaneCompletion.model_validate_json(
                    completion_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise AgentWorkflowError(
                    f"module lane completion is invalid: {module_id}"
                ) from exc
            if existing.completion_sha256() != completion.completion_sha256():
                raise AgentWorkflowError(
                    f"module lane completion changed for the same semantic input: {module_id}"
                )
            completion = existing
        else:
            self.service.store.write_json(completion_ref, payload)
        return completion_ref, completion

    async def _execute_module_lane(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        *,
        defer_main_exceptions: bool = True,
        lane_state_override: dict | None = None,
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict]:
        # A deferred Main exception is resumed from the exact private lane
        # state that reached the cohort boundary.  Rebuilding from the shared
        # state would re-run the author/auditor and could duplicate Provider
        # work before the serialized exception decision.
        lane_state = deepcopy(lane_state_override if lane_state_override is not None else state)
        lane_state["_module_lane"] = module_id
        lane_state["_defer_main_exceptions"] = defer_main_exceptions
        spec = self._lane_task_spec(lane_state, module_id)
        spec_ref = (
            f"Work/runs/{state['run_id']}/lanes/module-{module_id}/task-spec.json"
        )
        self.service.store.write_json(spec_ref, spec.model_dump(mode="json"))
        lane_attempt_id = f"lane-attempt-{uuid4().hex}"
        started_at_ns = time.time_ns()
        event_store = LocalEventStore(self.service.workspace, state["run_id"])
        event_store.append(
            "TaskDispatched",
            stage_id="module-work",
            task_id=spec.lane_id,
            task_attempt_id=lane_attempt_id,
            artifact_refs=[self._artifact_ref(spec_ref)],
            correlation_id=workflow_id,
            payload={"module_id": module_id, "semantic_key": spec.semantic_key},
        )
        attempt_ref = (
            f"Work/runs/{state['run_id']}/lanes/module-{module_id}/attempts/"
            f"{lane_attempt_id}.json"
        )
        self.service.store.write_json(
            attempt_ref,
            LaneAttemptRecord(
                lane_id=spec.lane_id,
                task_attempt_id=lane_attempt_id,
                lease_epoch=1,
                started_at_ns=started_at_ns,
                status="started",
            ).model_dump(mode="json"),
        )
        event_store.append(
            "AttemptStarted",
            stage_id="module-work",
            task_id=spec.lane_id,
            task_attempt_id=lane_attempt_id,
            lease_epoch=1,
            correlation_id=workflow_id,
            payload={"module_id": module_id},
        )
        try:
            submission = await self._module_pipeline(
                module_id,
                lane_state,
                workflow_id,
                review=True,
                checkpoint=False,
            )
            completion_ref, completion = self._build_lane_completion(
                lane_state, module_id, submission, spec
            )
        except BaseException as exc:
            if isinstance(exc, DeferredMainDecision):
                # Keep the exact in-memory lane state available to the cohort
                # drain.  The state is also represented by durable progress
                # and candidate artifacts; this attribute is only a
                # same-process continuation hint.
                try:
                    setattr(exc, "lane_state", lane_state)
                except Exception:
                    pass
            if isinstance(exc, DeferredMainDecision):
                disposition = "escalate"
            elif isinstance(
                exc,
                (AgentWorkflowBlocked, ReportingNeedsDecisionError),
            ):
                disposition = "needs_input"
            else:
                disposition = "failed"
            self.service.store.write_json(
                attempt_ref,
                LaneAttemptRecord(
                    lane_id=spec.lane_id,
                    task_attempt_id=lane_attempt_id,
                    lease_epoch=1,
                    started_at_ns=started_at_ns,
                    finished_at_ns=time.time_ns(),
                    status="failed",
                    error=str(exc),
                ).model_dump(mode="json"),
            )
            candidate_ref = (
                f"Work/runs/{state['run_id']}/lanes/module-{module_id}/"
                f"exceptions/{lane_attempt_id}.json"
            )
            self.service.store.write_json(
                candidate_ref,
                LaneExceptionCandidate(
                    lane_id=spec.lane_id,
                    run_id=state["run_id"],
                    module_id=module_id,
                    disposition=disposition,
                    reason=str(exc),
                    task_attempt_id=lane_attempt_id,
                ).model_dump(mode="json"),
            )
            event_store.append(
                "TaskFailed",
                stage_id="module-work",
                task_id=spec.lane_id,
                task_attempt_id=lane_attempt_id,
                lease_epoch=1,
                correlation_id=workflow_id,
                artifact_refs=[self._artifact_ref(candidate_ref)],
                payload={
                    "module_id": module_id,
                    "error": str(exc),
                    "exception_candidate_ref": candidate_ref,
                },
            )
            raise
        self.service.store.write_json(
            attempt_ref,
            LaneAttemptRecord(
                lane_id=spec.lane_id,
                task_attempt_id=lane_attempt_id,
                lease_epoch=completion.lease_epoch,
                started_at_ns=started_at_ns,
                finished_at_ns=time.time_ns(),
                status="completed",
            ).model_dump(mode="json"),
        )
        event_store.append(
            "TypedResultAccepted",
            stage_id="module-work",
            task_id=spec.lane_id,
            task_attempt_id=lane_attempt_id,
            lease_epoch=completion.lease_epoch,
            artifact_refs=[self._artifact_ref(completion_ref)],
            correlation_id=workflow_id,
            payload={"module_id": module_id},
        )
        return submission, completion_ref, completion, lane_state

    async def _run_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run isolated module lanes and reduce them at one terminal barrier.

        Every requested module gets one worker immediately.  An ordinary lane
        failure is preserved without cancelling already-admitted siblings.
        """

        results: dict[
            str, tuple[ModuleSubmission, str, LaneCompletion, dict]
        ] = {}
        for module_id in requested_modules:
            if module_id in state.get("module_submissions", {}):
                continue
            recovered = self._recover_module_lane(module_id, state)
            if recovered is not None:
                results[module_id] = recovered
        pending = [
            module_id
            for module_id in requested_modules
            if module_id not in state.get("module_submissions", {})
            and module_id not in results
        ]
        event_store = LocalEventStore(self.service.workspace, state["run_id"])
        timing_history = TaskTimingHistory(self.service.workspace)
        candidates = []
        for index, module_id in enumerate(pending):
            provider_router = getattr(self.service, "provider_router", None)
            if provider_router is None:
                priority, critical_path_weight, configured_duration_ms = (
                    50,
                    1.0,
                    60_000,
                )
            else:
                priority, critical_path_weight, configured_duration_ms = (
                    provider_router.scheduling_hints(
                        task_id=f"module-lane:{module_id}",
                        task_kind="module_lane",
                    )
                )
            candidates.append(
                SchedulingCandidate(
                    task_id=module_id,
                    owner_key=module_id,
                    task_kind="module_lane",
                    ordinal=index,
                    priority=priority,
                    critical_path_weight=critical_path_weight,
                    expected_duration_ms=timing_history.estimate_ms(
                        "module_lane",
                        module_id,
                        default=configured_duration_ms,
                    ),
                )
            )
        scheduler = AdaptiveTaskScheduler(candidates)
        event_store.append(
            "StageReady",
            stage_id="module-work",
            correlation_id=workflow_id,
            payload={
                "target_modules": list(requested_modules),
                "concurrency": len(pending),
                "admission": "all_ready_modules",
                "recovered_modules": sorted(results, key=float),
                "scheduling_policy": "longest_critical_path_first_v1",
                "scheduling_candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in candidates
                ],
            },
        )
        failures: list[BaseException] = []
        failures_by_module: dict[str, BaseException] = {}
        deferred_main_modules: set[str] = set()
        deferred_lane_states: dict[str, dict] = {}
        freeze_admission = asyncio.Event()

        async def worker() -> None:
            while True:
                if freeze_admission.is_set():
                    return
                try:
                    self._raise_if_cancel_requested(state["run_id"])
                except asyncio.CancelledError:
                    freeze_admission.set()
                    raise
                candidate = await scheduler.next()
                if candidate is None:
                    return
                module_id = candidate.task_id
                started = time.perf_counter()
                if freeze_admission.is_set():
                    return
                try:
                    results[module_id] = await self._execute_module_lane(
                        module_id,
                        state,
                        workflow_id,
                        # Main exception decisions are always deferred while
                        # an all-ready cohort is draining.  The lane is
                        # resumed below from its persisted private state.
                        defer_main_exceptions=True,
                    )
                    timing_history.record(
                        run_id=state["run_id"],
                        task_id=module_id,
                        task_kind="module_lane",
                        owner_key=module_id,
                        duration_ms=int(
                            (time.perf_counter() - started) * 1000
                        ),
                        status="completed",
                    )
                except DeferredMainDecision as exc:
                    deferred_main_modules.add(module_id)
                    lane_state = getattr(exc, "lane_state", None)
                    if isinstance(lane_state, dict):
                        deferred_lane_states[module_id] = lane_state
                    timing_history.record(
                        run_id=state["run_id"],
                        task_id=module_id,
                        task_kind="module_lane",
                        owner_key=module_id,
                        duration_ms=int(
                            (time.perf_counter() - started) * 1000
                        ),
                        status="deferred",
                    )
                except BaseException as exc:
                    failures.append(exc)
                    failures_by_module[module_id] = exc
                    timing_history.record(
                        run_id=state["run_id"],
                        task_id=module_id,
                        task_kind="module_lane",
                        owner_key=module_id,
                        duration_ms=int(
                            (time.perf_counter() - started) * 1000
                        ),
                        status="failed",
                    )

        worker_count = len(pending)
        workers = [
            asyncio.create_task(
                worker(), name=f"module-lane-worker-{index + 1}"
            )
            for index in range(worker_count)
        ]
        await asyncio.gather(*workers)
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/scheduling/module-lanes.json",
            {
                "policy": "longest_critical_path_first_v1",
                "decisions": [
                    decision.model_dump(mode="json")
                    for decision in scheduler.decisions
                ],
            },
        )
        for module_id in sorted(deferred_main_modules, key=float):
            # A deferred Main decision is the only reason to leave the
            # private lane cohort.  Retry after every initially ready lane has
            # reached a terminal state, and never re-run a verified finding or
            # revision artifact.
            try:
                resumed_lane_state = deferred_lane_states.get(module_id)
                if isinstance(resumed_lane_state, dict):
                    # run_module_review only consults persisted progress during
                    # an explicit resume.  Mark this same-process continuation
                    # as a resume so the already-written finding/candidate is
                    # consumed instead of triggering a second auditor turn.
                    resumed_lane_state["resume"] = True
                results[module_id] = await self._execute_module_lane(
                    module_id,
                    state,
                    workflow_id,
                    defer_main_exceptions=False,
                    lane_state_override=resumed_lane_state,
                )
            except BaseException as exc:
                failures.append(exc)
                failures_by_module[module_id] = exc

        # If a test double or an older helper cannot expose its private state,
        # the durable progress file still allows the next invocation to
        # recover the missing lane.  Keep this map intentionally best-effort;
        # no ordinary failure is replayed inside the same cohort.

        completions: list[tuple[str, LaneCompletion]] = []
        for module_id in requested_modules:
            if module_id in results:
                submission, completion_ref, completion, lane_state = results[module_id]
                state.setdefault("module_submissions", {})[module_id] = submission
                state.setdefault("specialist_submissions", {})[module_id] = lane_state[
                    "specialist_submissions"
                ][module_id]
                state.setdefault("module_review_completion_refs", {})[module_id] = (
                    lane_state["module_review_completion_refs"][module_id]
                )
                state.setdefault("review_exception_refs", []).extend(
                    ref
                    for ref in lane_state.get("review_exception_refs", [])
                    if ref not in state.get("review_exception_refs", [])
                )
            elif module_id in state.get("module_submissions", {}):
                submission = state["module_submissions"][module_id]
                spec = self._lane_task_spec(state, module_id)
                completion_ref, completion = self._build_lane_completion(
                    state, module_id, submission, spec
                )
            else:
                # The lane failed (or was left ambiguous) and has no
                # promotable typed completion.  Preserve successful siblings
                # and record this terminal state in the cohort barrier below.
                continue
            completions.append((completion_ref, completion))

        if failures_by_module:
            failure_refs: dict[str, list[str]] = {}
            for module_id in sorted(failures_by_module, key=float):
                exception_root = (
                    self.service.workspace
                    / f"Work/runs/{state['run_id']}/lanes/module-{module_id}/exceptions"
                )
                failure_refs[module_id] = [
                    path.relative_to(self.service.workspace).as_posix()
                    for path in sorted(exception_root.glob("*.json"))
                    if path.is_file()
                ]
            terminal_ref = (
                f"Work/runs/{state['run_id']}/lanes/"
                + (
                    "module-barrier.json"
                    if set(requested_modules) == set(REPORT_MODULE_IDS)
                    else "partial-module-barrier.json"
                )
            )
            self.service.store.write_json(
                terminal_ref,
                {
                    "kind": "module_lane_terminal_barrier",
                    "version": 1,
                    "run_id": state["run_id"],
                    "target_modules": sorted(requested_modules, key=float),
                    "status": "failed",
                    "scope": (
                        "full"
                        if set(requested_modules) == set(REPORT_MODULE_IDS)
                        else "partial"
                    ),
                    "completion_refs": {
                        completion.module_id: ref for ref, completion in completions
                    },
                    "completion_hashes": {
                        completion.module_id: completion.completion_sha256()
                        for _ref, completion in completions
                    },
                    "failure_refs": failure_refs,
                    "terminal_statuses": {
                        module_id: (
                            "completed"
                            if any(
                                completion.module_id == module_id
                                for _ref, completion in completions
                            )
                            else "failed"
                        )
                        for module_id in sorted(requested_modules, key=float)
                    },
                },
            )
            state["module_lane_barrier_ref"] = terminal_ref
            self._checkpoint(
                state,
                "module-work",
                "failed",
                "; ".join(
                    f"{module_id}: {failures_by_module[module_id]}"
                    for module_id in sorted(failures_by_module, key=float)
                ),
            )
            # Do not enter Cross when any module lane failed.  The caller will
            # surface the first deterministic error while all successful lane
            # artifacts remain recoverable.
            raise failures[0]

        barrier = WorkflowReducer(
            self.service.workspace, state["run_id"]
        ).write_module_barrier(
            list(requested_modules),
            completions,
            partial=set(requested_modules) != set(REPORT_MODULE_IDS),
        )
        state["module_lane_barrier_ref"] = (
            f"Work/runs/{state['run_id']}/lanes/"
            + (
                "module-barrier.json"
                if barrier.scope == "full"
                else "partial-module-barrier.json"
            )
        )
        event_store.append(
            "StageCompleted",
            stage_id="module-work",
            artifact_refs=[self._artifact_ref(state["module_lane_barrier_ref"])],
            correlation_id=workflow_id,
            payload={"target_modules": list(requested_modules)},
        )
        self._checkpoint(state, "module-work", "in_progress")

    async def _module_pipeline(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        *,
        review: bool = True,
        checkpoint: bool = True,
    ) -> ModuleSubmission:
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item for item in state["module_dispatch"].module_tasks if item.agent_id == specialist_id
        )
        resumed_payload = state.get("specialist_submissions", {}).get(module_id)
        forced_fresh_revision = None
        revision = (
            resumed_payload.revision
            if resumed_payload is not None
            else int(forced_fresh_revision)
            if forced_fresh_revision is not None
            else 0
        )
        if (
            state.get("resume")
            and resumed_payload is None
            and forced_fresh_revision is None
        ):
            draft_base = self.service.workspace / (
                f"Work/runs/{state['run_id']}/drafts/module-{module_id}"
            )
            persisted_revisions = [
                int(path.name[1:])
                for path in draft_base.glob("r*")
                if path.is_dir() and path.name[1:].isdigit()
            ]
            if persisted_revisions:
                revision = max(persisted_revisions)

        authoring_context_sha256 = self._module_authoring_context_sha256(
            state,
            module_id,
        )
        draft_base = self.service.workspace / (
            f"Work/runs/{state['run_id']}/drafts/module-{module_id}"
        )
        draft_root = draft_base / f"r{revision}"
        context_marker = draft_root / "_authoring-context.json"
        existing_parts = list(draft_root.glob("*.md"))
        marker_matches = False
        if context_marker.is_file():
            try:
                marker = json.loads(
                    context_marker.read_text(encoding="utf-8")
                )
                marker_matches = (
                    marker.get("authoring_context_sha256")
                    == authoring_context_sha256
                )
            except (OSError, ValueError):
                marker_matches = False
        if existing_parts and not marker_matches:
            persisted_revisions = [
                int(path.name[1:])
                for path in draft_base.glob("r*")
                if path.is_dir() and path.name[1:].isdigit()
            ]
            revision = max([revision, *persisted_revisions]) + 1
            forced_fresh_revision = revision
            draft_root = draft_base / f"r{revision}"
            context_marker = draft_root / "_authoring-context.json"
        self.service.store.write_json(
            context_marker.relative_to(
                self.service.workspace
            ).as_posix(),
            {
                "kind": "module_authoring_draft_context",
                "run_id": state["run_id"],
                "module_id": module_id,
                "revision": revision,
                "authoring_context_sha256": authoring_context_sha256,
            },
        )

        resume_part_constraints: list[str] = []
        resume_allowed_tools: list[str] = []
        saved_parts: list[str] = []
        rewrite_part_ids: list[str] = []
        base_constraints = list(
            dict.fromkeys(
                [
                    *planned.constraints,
                    (
                        "固定 taxonomy 是唯一合法的数字标题体系；正文只允许使用当前"
                        " required_submodule_ids 中的编号标题。现状、判断、原因、风险机理、"
                        "建议和验证只能使用普通段落或无编号粗体标签，禁止自行生成下一级编号"
                    ),
                    (
                        "共享模块会话中，若 Provider 支持同一轮多个工具调用，应在一个"
                        " assistant turn 内为所有 ready 小节分别调用 write_result_part，"
                        "再在下一轮提交小型 module commit；小节是文档 part，不是独立任务或会话"
                    ),
                    *state["request"].execution_requirements,
                    f"缺失证据策略={state['request'].missing_evidence_policy}",
                    *self._evidence_policy_constraints(state["request"].missing_evidence_policy),
                    *self._user_supplement_constraints(
                        state,
                        stage="module_authoring",
                        target_ids={
                            module_id,
                            *REPORT_TAXONOMY[module_id].submodules,
                        },
                    ),
                ]
            )
        )
        if forced_fresh_revision is not None:
            base_constraints.append(
                "输入或用户补充约束已变化；本 revision 必须从当前上下文完整重写全部固定小节，禁止复用旧 draft parts"
            )
        if state.get("resume"):
            if revision > 0 and forced_fresh_revision is None:
                self._inherit_module_result_parts(
                    state["run_id"], module_id, revision - 1, revision
                )
            saved_parts = sorted(path.stem for path in draft_root.glob("*.md"))
            missing_parts = sorted(set(REPORT_TAXONOMY[module_id].submodules) - set(saved_parts))
            for part_id in saved_parts:
                part_path = draft_root / f"{part_id}.md"
                binding_path = draft_root / "_evidence" / f"{part_id}.json"
                binding_ready = False
                if binding_path.is_file():
                    try:
                        binding = json.loads(binding_path.read_text(encoding="utf-8"))
                    except (OSError, ValueError):
                        binding = None
                    binding_ready = isinstance(binding, dict) and isinstance(
                        binding.get("evidence_ids"), list
                    )
                part_content = part_path.read_text(encoding="utf-8")
                if (
                    not binding_ready
                    or "[[CLAIM:" in part_content
                    or "<persisted_result_part" in part_content.casefold()
                    or extra_numbered_submodule_headings(part_id, part_content)
                ):
                    rewrite_part_ids.append(part_id)
            resume_part_constraints = [
                "这是同一 run 的恢复任务；已有正文分段=" + (", ".join(saved_parts) or "无"),
                "固定 taxonomy 尚缺正文分段=" + (", ".join(missing_parts) or "无"),
                "当前协议只接收 write_result_part 逐项保存的完整读者可见正文和 evidence_ids；先用 list_result_parts 确认状态，ready 项不得重写。",
                "先调用 list_result_parts；必须重写或补绑定的 part="
                + (", ".join(rewrite_part_ids) or "无"),
            ]
            if saved_parts:
                resume_allowed_tools = [
                    "search_project_evidence",
                    "open_project_source",
                    "list_result_parts",
                    "write_result_part",
                    "submit_result",
                ]

        base_constraints.append(
            "模块作者只在本模块会话内工作；不得调用 query_peer/reply_peer。跨模块关系由后续五个 Cross owner 处理。"
        )

        module_input = ModuleAuthoringInput(
            run_id=state["run_id"],
            module_id=module_id,
            revision=revision,
            required_submodule_ids=list(REPORT_TAXONOMY[module_id].submodules),
            coverage_ref=state["preparation_refs"]["coverage"],
            evidence_ref=state["preparation_refs"]["evidence"],
            manifest_ref=state["preparation_refs"]["manifest"],
            knowledge_ref=state["module_knowledge_refs"][module_id],
            saved_part_ids=saved_parts,
            rewrite_part_ids=rewrite_part_ids,
        )
        module_input_path = self.service.store.write_json(
            (f"Work/runs/{state['run_id']}/context/module-authoring-{module_id}-r{revision}.json"),
            module_input.model_dump(mode="json"),
        )
        module_input_ref = module_input_path.relative_to(self.service.workspace).as_posix()
        module_authoring_tools = [
            "search_project_evidence",
            "open_project_source",
            "search_reference_library",
            "open_reference",
            "web_search",
            "open_web_source",
            "inspect_document",
            "inspect_image",
            "calculate",
            "open_artifact",
            "search_text",
            "report_gap",
            "write_result_part",
            "list_result_parts",
            "report_blocked",
            "submit_result",
        ]
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"module-{module_id}",
                    "run_id": state["run_id"],
                    "agent_id": specialist_id,
                    "allowed_outputs": ["module_submission"],
                    "allowed_tools": (
                        resume_allowed_tools
                        if resume_allowed_tools
                        else module_authoring_tools
                    ),
                    "target_submodule_ids": (
                        sorted(set(rewrite_part_ids) | set(missing_parts))
                        if state.get("resume") and saved_parts
                        else list(REPORT_TAXONOMY[module_id].submodules)
                    ),
                    "constraints": list(
                        dict.fromkeys([*base_constraints, *resume_part_constraints])
                    ),
                    "revision": revision,
                    "input_refs": [
                        module_input_ref,
                        module_input.coverage_ref,
                        module_input.evidence_ref,
                        module_input.manifest_ref,
                    ],
                    "input_contract_kind": "module_authoring_input",
                    "input_contract_ref": module_input_ref,
                    "inline_context": planned.inline_context or "",
                    "artifact_delivery_modes": {
                        module_input_ref: "inline",
                        module_input.coverage_ref: "reference",
                        module_input.evidence_ref: "reference",
                        module_input.manifest_ref: "reference",
                    },
                }
            ).model_dump(mode="python")
        )
        if resumed_payload is not None:
            payload = resumed_payload
            await self.service._notice(
                f"已恢复模块 {module_id} 的 specialist 提交；继续原 run 的独立模块审计。"
            )
        else:
            payload = await self._agent(
                specialist_id,
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"specialist-{module_id}",
            )
            if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
                raise AgentWorkflowError(f"{specialist_id} returned the wrong module payload")
            payload = ModuleSubmission.model_validate(payload.model_dump(mode="python"))
            self.service.store.write_json(
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
                payload.model_dump(mode="json"),
            )
            state.setdefault("specialist_submissions", {})[module_id] = payload
            if review and checkpoint:
                self._checkpoint(state, "module-work", "in_progress")

        if not review:
            return payload
        return await self._module_review_loop(
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=set(REPORT_TAXONOMY[module_id].submodules),
        )

    async def _module_review_loop(
        self,
        module_id: str,
        payload: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
    ) -> ModuleSubmission:
        return await run_module_review(
            self,
            module_id,
            payload,
            state,
            workflow_id,
            initial_scope=initial_scope,
            lifecycle_id="initial",
        )

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        self._verify_module_lane_barrier(state)
        await run_cross_review(self, state, workflow_id)

    def _verify_module_lane_barrier(self, state: dict) -> CohortBarrier:
        """Fail closed unless the current run has exactly five verified lanes."""

        run_id = state["run_id"]
        expected_ref = f"Work/runs/{run_id}/lanes/module-barrier.json"
        if state.get("module_lane_barrier_ref") != expected_ref:
            raise AgentWorkflowError("Cross requires the canonical five-module barrier")
        barrier_path = self.service.workspace / expected_ref
        try:
            barrier = CohortBarrier.model_validate_json(
                barrier_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise AgentWorkflowError("five-module barrier is missing or invalid") from exc
        expected_modules = set(REPORT_MODULE_IDS)
        if (
            barrier.run_id != run_id
            or barrier.scope != "full"
            or barrier.status != "committed"
            or set(barrier.target_modules) != expected_modules
            or set(barrier.completion_refs) != expected_modules
            or set(barrier.completion_hashes) != expected_modules
        ):
            raise AgentWorkflowError("five-module barrier has incomplete ownership")
        barrier_identity = {
            "run_id": run_id,
            "target_modules": sorted(barrier.target_modules, key=float),
            "completion_refs": barrier.completion_refs,
            "completion_hashes": barrier.completion_hashes,
            "scope": "full",
        }
        if barrier.barrier_sha256 != hashlib.sha256(
            json.dumps(
                barrier_identity,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest():
            raise AgentWorkflowError("five-module barrier identity hash is invalid")
        for module_id in REPORT_MODULE_IDS:
            ref = barrier.completion_refs[module_id]
            path = self.service.workspace / ref
            try:
                completion = LaneCompletion.model_validate_json(
                    path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise AgentWorkflowError(
                    f"module completion is missing or invalid: {module_id}"
                ) from exc
            if (
                completion.run_id != run_id
                or completion.module_id != module_id
                or completion.completion_sha256()
                != barrier.completion_hashes[module_id]
            ):
                raise AgentWorkflowError(
                    f"module completion does not match barrier: {module_id}"
                )
            self._verify_artifact_ref(completion.subject)
            self._verify_artifact_ref(completion.review_completion)
        return barrier

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        interface_registry = state.get("interface_resolution_registry")
        if interface_registry is None and state.get("interface_resolution_registry_ref"):
            registry_ref = str(state["interface_resolution_registry_ref"])
            registry_path = self.service.workspace / registry_ref
            if not registry_path.is_file():
                raise AgentWorkflowError(
                    f"Chief cannot start: interface registry is missing ({registry_ref})"
                )
            expected_hash = state.get("interface_resolution_registry_sha256")
            actual_hash = self._sha256(registry_path)
            if expected_hash != actual_hash:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry hash is stale"
                )
            try:
                interface_registry = InterfaceResolutionRegistry.model_validate_json(
                    registry_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise AgentWorkflowError(
                    "Chief cannot start: interface registry is invalid"
                ) from exc
            state["interface_resolution_registry"] = interface_registry
        if isinstance(interface_registry, InterfaceResolutionRegistry):
            pending_interfaces = interface_registry.pending_request_ids
            if pending_interfaces:
                raise AgentWorkflowError(
                    "Chief cannot start while Cross interface IF/XMR records remain pending: "
                    f"{list(pending_interfaces)}"
                )
        special_topic_plan: SpecialTopicPlan | None = state.get("special_topic_plan")
        special_topic_input_refs: list[str] = []
        special_topic_context = ""
        if special_topic_plan is not None:
            special_topic_knowledge = KnowledgeContextBuilder(
                self.service.workspace, state["run_id"]
            ).build_special_topics(special_topic_plan)
            state["special_topic_knowledge_ref"] = (
                special_topic_knowledge.path.as_posix()
            )
            special_topic_input_refs.append(special_topic_knowledge.path.as_posix())
            special_topic_context = special_topic_knowledge.text
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        claim_ledger = ClaimLedger(
            claims=claims,
            sources=SourceLedger(self.service.workspace, state["run_id"]).records,
        )
        claim_ledger_ref = f"Work/runs/{state['run_id']}/ledgers/claims.json"
        self.service.store.write_json(
            claim_ledger_ref,
            claim_ledger.model_dump(mode="json"),
        )
        self._require_template_skill(state)
        # This call is the only Chief input boundary: it materializes and
        # validates the immutable CrossDecisionPack before exposing five
        # approved ModuleContentView values.
        editor_input = self._current_chief_editor_input(state)
        editor_input_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/context/chief-editor-input.json",
            editor_input.model_dump(mode="json"),
        )
        editor_input_ref = editor_input_path.relative_to(self.service.workspace).as_posix()
        envelope = TaskEnvelope(
            task_id="chief-edit",
            run_id=state["run_id"],
            agent_id="chief-editor",
            objective="整合已批准五模块，形成自然、丰富、有专业差异且可溯源的完整报告。",
            input_refs=[
                editor_input_ref,
                *special_topic_input_refs,
            ],
            constraints=[
                "不得改变批准事实、数值、风险等级和来源语义",
                "批准正文的引用与脚注由运行时保护和装配，总编只提交 schema 声明字段",
                "protected_claim_ids 由 submit_result 根据运行时已批准模块确定性注入；不得自行提交、打开或重传 Claim/Source ledger",
                "正文不得套用统一的事实-证据-风险模板",
                "tables 只提交 CrossDecisionPack 与已批准模块声明的 E-* evidence_ids；photo_ids 提交空数组，图片由运行时按 Evidence 绑定装配",
                "每个 module_narrative 必须逐一保留该模块全部固定 submodule_id 和标题，不得压缩为核心发现摘要",
                "每个已批准子模块正文必须原样包含在所属 module_narrative 中；总编只能增加章节引言、过渡、交叉引用和综合判断，不能删除或缩写专家正文",
                "为避免重复输出和截断，每个 module_narrative 使用对应 [[APPROVED_MODULE:2.x]] 标记作为正文基线，可在标记前后增加短过渡；工作流会确定性嵌回批准正文",
                "chief-editor-input 已完整内联在 input_contract 中，是唯一模块正文入口；不得再打开 Outputs/Modules、Outputs/Reviews 或该合同路径重读",
                "不得恢复已删除的“跨领域关联风险”模块，也不得提交旧版 synthesis_dispositions 或 synthesis_tables 元数据",
                "始终提交 assessment_background、findings_overview、regional_executive_summary、risk_panorama、dimension_risk_analysis、data_gap_analysis、improvement_action_plan；仅当 special_topic_plan 存在时提交 special_topic_analysis",
                "固定综合字段只写正文、禁止自带章节标题",
                *(
                    [
                        "special_topic_analysis 必须严格按 special_topic_plan 输出全部且仅输出 ### 4.n 标题及其正文",
                        "八个综合章节只用同名 part_id 的 write_result_part 逐项持久化；先用 list_result_parts 确认状态，ready 项不得重写",
                    ]
                    if special_topic_plan is not None
                    else [
                        "special_topic_plan 为空；禁止提交 special_topic_analysis，最终 Markdown 和 DOCX 必须完全省略第四章",
                        "七个固定综合章节只用同名 part_id 的 write_result_part 逐项持久化；先用 list_result_parts 确认状态，ready 项不得重写",
                    ]
                ),
                "任何综合节都必须自足地包含归纳事实、综合判断和决策含义；模块号只能用于句末追溯，禁止用‘详见第二章’‘见2.x’或模块编号清单代替分析",
                "regional_executive_summary 必须按真实区域或责任边界归纳重点、优先行动与验证状态；没有区域划分证据时必须明确边界，禁止编造区域名称",
                "dimension_risk_analysis 必须比较五个维度的主导风险和决策含义；data_gap_analysis 必须归并重复缺口并说明结论影响与补证优先级；improvement_action_plan 必须给出责任接口、动作、验收指标和剩余风险",
                *(
                    [
                        "第四章不设固定主题；逐节执行 Inputs 专项问题计划中的简要要求，标题、顺序和数量不得自行增删",
                        "专项问题分析可使用已内联的项目 Knowledge 和模型世界知识补充机理、备选解释、方案权衡、行业实践与验证方法；必须把通用判断与当前项目事实明确区分",
                    ]
                    if special_topic_plan is not None
                    else []
                ),
                *self._user_supplement_constraints(
                    state,
                    stage="chief_edit",
                    target_ids={
                        *REPORT_MODULE_IDS,
                    },
                ),
                "risk_panorama 必须归纳实际主要风险及其判断依据，不得重复五章摘要",
                "原始表图片由运行时确定性全量装配为最小子模块图证汇总表；不得筛选、遗漏或自行放置",
                "写作质量只按已内联的模板 Skill quality-rubric 检查，不得从 Knowledge 补充报告规则",
                "已批准模块正文与当前 Evidence 是项目事实入口；可使用模型世界知识解释机制和方案权衡，但不得新增或改写客户事实",
                *(
                    ["这是同一 run 的恢复任务；先调用 list_result_parts 并复用已保存分段"]
                    if state.get("resume")
                    else []
                ),
                *state.get("chief_editor_constraints", []),
            ],
            allowed_outputs=["edited_report_submission"],
            allowed_tools=[
                "write_result_part",
                "list_result_parts",
                "submit_result",
            ],
            input_contract_kind="chief_editor_input",
            input_contract_ref=editor_input_ref,
            inline_context="\n\n".join(
                text
                for text in (
                    special_topic_context,
                    self._role_skill_context(state, "chief-editor"),
                )
                if text
            ),
        )
        payload = await self._agent(
            "chief-editor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key="chief-editor",
        )
        if not isinstance(payload, EditedReportSubmission):
            raise AgentWorkflowError("chief-editor returned the wrong payload type")
        approved_module_text = {
            module_id: self._approved_module_text(state["module_submissions"][module_id])
            for module_id in REPORT_MODULE_IDS
        }
        payload = expand_approved_module_markers(payload, approved_module_text)
        payload = payload.model_copy(
            update={
                "photo_ids": ReportAssetAssembler.runtime_photo_ids(
                    state.get("evidence_items", []),
                    state.get("photo_assets", []),
                )
            }
        )
        validate_editor_protection(payload, claims)
        state["editor_quality_observations"] = validate_editor_quality(
            payload, state["module_submissions"]
        )
        candidate_ref = f"Work/runs/{state['run_id']}/edited-revisions/chief-r0.json"
        envelope_ref = f"Work/runs/{state['run_id']}/context/chief-editor-envelope.json"
        self.service.store.write_json(candidate_ref, payload.model_dump(mode="json"))
        self.service.store.write_json(envelope_ref, envelope.model_dump(mode="json"))
        state["edited_report"] = payload
        state["chief_editor_envelope"] = envelope
        state["chief_editor_session_key"] = "chief-editor"
        state["approved_module_text"] = approved_module_text
        state["chief_candidate_ref"] = candidate_ref
        state["chief_editor_input_ref"] = editor_input_ref
        state["chief_editor_envelope_ref"] = envelope_ref
        state["chief_editor_completion_ref"] = (
            self._write_chief_editor_completion(
                state,
                editor_input=editor_input,
                envelope=envelope,
            )
        )

    @staticmethod
    def _final_report_section_values(
        edited: EditedReportSubmission,
    ) -> dict[str, object]:
        return {
            "1.1": edited.assessment_background,
            "1.2": edited.findings_overview,
            "1.3": edited.regional_executive_summary,
            **{module_id: edited.module_narratives[module_id] for module_id in REPORT_MODULE_IDS},
            "3.1.1": edited.risk_panorama,
            "3.1.2": edited.dimension_risk_analysis,
            "3.1.3": edited.data_gap_analysis,
            "3.2": edited.improvement_action_plan,
            "4": edited.special_topic_analysis,
        }

    @classmethod
    def _final_revision_diff(
        cls,
        previous: EditedReportSubmission,
        revised: EditedReportSubmission,
    ) -> dict:
        before_sections = cls._final_report_section_values(previous)
        after_sections = cls._final_report_section_values(revised)
        changed_sections = sorted(
            section_id
            for section_id in FINAL_REPORT_SECTION_IDS
            if before_sections[section_id] != after_sections[section_id]
        )
        section_fields = {
            "assessment_background",
            "findings_overview",
            "regional_executive_summary",
            "module_narratives",
            "risk_panorama",
            "dimension_risk_analysis",
            "data_gap_analysis",
            "improvement_action_plan",
            "special_topic_analysis",
        }
        before = previous.model_dump(mode="json")
        after = revised.model_dump(mode="json")
        changed_contract_fields = sorted(
            field
            for field in set(before) | set(after)
            if field not in section_fields and before.get(field) != after.get(field)
        )
        return {
            "changed_section_ids": changed_sections,
            "changed_contract_fields": changed_contract_fields,
        }

    async def _final_review_loop(
        self,
        state: dict,
        workflow_id: str,
        *,
        chief_envelope: TaskEnvelope,
        chief_session_key: str,
        approved_module_text: dict[str, str],
        claims: list,
        aggregate_mode: bool = False,
    ) -> None:
        await run_final_review(
            self,
            state,
            workflow_id,
            chief_envelope=chief_envelope,
            chief_session_key=chief_session_key,
            approved_module_text=approved_module_text,
            claims=claims,
            aggregate_mode=aggregate_mode,
        )

    @staticmethod
    def _approved_module_text(module: ModuleSubmission) -> str:
        """Return the reviewed module body without asking the chief to reproduce it."""

        return compose_module_markdown(module.module_id, module.submodule_narratives)

    def _validate_module_structure(
        self,
        state: dict,
        module: ModuleSubmission,
        phase: str,
    ) -> str:
        """Persist deterministic integrity checks and non-binding semantic signals."""

        validation_ref = (
            f"Work/runs/{state['run_id']}/reviews/module-quality-"
            f"{module.module_id}-r{module.revision}-{phase}.json"
        )
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module.module_id}-r{module.revision}.json"
        )
        subject_path = self.service.workspace / subject_ref
        if not subject_path.is_file():
            raise AgentWorkflowError(
                f"模块确定性校验缺少待校验实体：{subject_ref}"
            )
        canonical = compose_module_markdown(module.module_id, module.submodule_narratives)
        failure: ValidationFailure | None = None
        if module.markdown.strip() != canonical.strip():
            failure = ValidationFailure(
                check_id="module.canonical_markdown",
                target_path="submodule_narratives",
                message="rendered module Markdown differs from canonical narratives",
            )
        shallow_signals = find_shallow_submodules({module.module_id: module})
        report = ValidationReport(
            validation_protocol_version=2,
            run_id=state["run_id"],
            subject_ref=subject_ref,
            subject_revision=module.revision,
            content_sha256=hashlib.sha256(subject_path.read_bytes()).hexdigest(),
            validator="module-structure/v2",
            check_ids=["module.canonical_markdown"],
            failures=[failure] if failure else [],
            observations=[
                f"possible_shallow_submodule:{submodule_id}" for submodule_id in shallow_signals
            ],
            passed=failure is None,
        )
        self.service.store.write_json(
            validation_ref,
            report.model_dump(mode="json"),
        )
        if failure is not None:
            raise AgentWorkflowError(
                f"模块 {module.module_id} 确定性完整性校验未通过：{failure.message}"
            )
        return validation_ref

    def _validate_module_exports(self, state: dict, phase: str) -> None:
        """Persist a deterministic audit before semantic review or delivery."""

        modules = state["module_submissions"]
        exported: dict[str, str] = {}
        for module_id in REPORT_MODULE_IDS:
            path = self.service.workspace / f"Outputs/Modules/{module_id}.md"
            exported[module_id] = path.read_text(encoding="utf-8") if path.is_file() else ""
        validation_ref = f"Work/runs/{state['run_id']}/reviews/module-export-integrity-{phase}.json"
        subject_ref = f"Work/runs/{state['run_id']}/context/module-export-set-{phase}.json"
        self.service.store.write_json(
            subject_ref,
            {
                "module_subject_refs": {
                    module_id: (
                        f"Work/runs/{state['run_id']}/modules/"
                        f"{module_id}-r{modules[module_id].revision}.json"
                    )
                    for module_id in REPORT_MODULE_IDS
                },
                "export_refs": {
                    module_id: f"Outputs/Modules/{module_id}.md" for module_id in REPORT_MODULE_IDS
                },
            },
        )
        try:
            validate_module_markdown_consistency(modules, exported)
        except ValueError as exc:
            self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    validator="module-export-integrity/v1",
                    check_ids=["module_export.equals_canonical_subject"],
                    failures=[
                        ValidationFailure(
                            check_id="module_export.equals_canonical_subject",
                            target_path="export_refs",
                            message=str(exc),
                        )
                    ],
                    passed=False,
                ).model_dump(mode="json"),
            )
            raise AgentWorkflowError(f"模块正文完整性校验未通过：{exc}") from exc
        self.service.store.write_json(
            validation_ref,
            ValidationReport(
                run_id=state["run_id"],
                subject_ref=subject_ref,
                validator="module-export-integrity/v1",
                check_ids=["module_export.equals_canonical_subject"],
                passed=True,
            ).model_dump(mode="json"),
        )

    def _validate_final_report_structure(
        self,
        state: dict,
        markdown: str,
        phase: str,
    ) -> None:
        """Audit every fixed chapter after aggregation and before rendering."""

        validation_ref = f"Work/runs/{state['run_id']}/reviews/report-integrity-{phase}.json"
        subject_ref = f"Work/runs/{state['run_id']}/validation/report-{phase}.md"
        self.service.store.write_text(subject_ref, markdown)
        content_sha256 = hashlib.sha256(
            (self.service.workspace / subject_ref).read_bytes()
        ).hexdigest()
        revision_text = phase.removeprefix("chief-candidate-r")
        subject_revision = (
            int(revision_text)
            if phase.startswith("chief-candidate-r") and revision_text.isdigit()
            else None
        )
        check_ids = ["final_report.fixed_sections_and_markdown"]
        try:
            edited = state.get("edited_report")
            plan = (
                edited.special_topic_plan
                if isinstance(edited, EditedReportSubmission)
                else state.get("special_topic_plan")
            )
            signals = validate_final_report_markdown(markdown, plan)
        except ValueError as exc:
            self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    validation_protocol_version=2,
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    subject_revision=subject_revision,
                    content_sha256=content_sha256,
                    validator="final-report-structure/v2",
                    check_ids=check_ids,
                    failures=[
                        ValidationFailure(
                            check_id=check_ids[0],
                            target_path="markdown",
                            message=str(exc),
                        )
                    ],
                    passed=False,
                ).model_dump(mode="json"),
            )
            raise AgentWorkflowError(f"总报告完整性校验未通过：{exc}") from exc
        self.service.store.write_json(
            validation_ref,
            ValidationReport(
                validation_protocol_version=2,
                run_id=state["run_id"],
                subject_ref=subject_ref,
                subject_revision=subject_revision,
                content_sha256=content_sha256,
                validator="final-report-structure/v2",
                check_ids=check_ids,
                observations=[
                    *state.get("editor_quality_observations", []),
                    *(
                        f"final_report:{name}:{value}"
                        for name, values in signals.items()
                        for value in values
                    ),
                ],
                passed=True,
            ).model_dump(mode="json"),
        )

    def _validated_final_audit_subject(
        self,
        state: dict,
    ) -> tuple[EditedReportSubmission, str]:
        """Load the exact audited subject and bind its canonical prose to delivery."""

        run_id = state["run_id"]
        completion_ref = state["final_review_completion_ref"]
        completion, _ = self._load_current_review_completion(
            run_id=run_id,
            completion_ref=completion_ref,
            lifecycle="final",
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="chief-editor-auditor",
        )
        if len(completion.subject_refs) != 1:
            raise AgentWorkflowError("final audit completion must bind exactly one subject")
        subject_ref = completion.subject_refs[0]
        subject_path = self.service.workspace / subject_ref
        audited = EditedReportSubmission.model_validate_json(
            subject_path.read_text(encoding="utf-8")
        )
        subject_revision_match = re.search(r"chief-r(\d+)\.json$", subject_ref)
        subject_revision = (
            int(subject_revision_match.group(1))
            if subject_revision_match is not None
            else 0
        )
        snapshot_ref = (
            f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        )
        snapshot_path = self.service.workspace / snapshot_ref
        if not snapshot_path.is_file():
            # Legacy same-run recovery: the final completion already binds the
            # exact edited JSON. Reconstruct only its deterministic Markdown
            # projection; no provider or reviewer call is repeated.
            _, canonical = self._delivery_projection(state, audited)
            validate_final_report_markdown(canonical, audited.special_topic_plan)
            canonical_ref = (
                f"Work/runs/{run_id}/validation/report-final-audit-legacy.md"
            )
            validation_ref = (
                f"Work/runs/{run_id}/reviews/report-integrity-final-audit-legacy.json"
            )
            canonical_path = self.service.store.write_text(canonical_ref, canonical)
            canonical_sha256 = hashlib.sha256(canonical_path.read_bytes()).hexdigest()
            validation_path = self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    validation_protocol_version=2,
                    run_id=run_id,
                    subject_ref=canonical_ref,
                    subject_revision=subject_revision,
                    content_sha256=canonical_sha256,
                    validator="final-audit-legacy-snapshot/v2",
                    check_ids=["final_report.fixed_sections_and_markdown"],
                    passed=True,
                ).model_dump(mode="json"),
            )
            snapshot_path = self.service.store.write_json(
                snapshot_ref,
                FinalAuditSnapshot(
                    run_id=run_id,
                    subject_ref=subject_ref,
                    subject_revision=subject_revision,
                    subject_sha256=hashlib.sha256(
                        subject_path.read_bytes()
                    ).hexdigest(),
                    canonical_markdown_ref=canonical_ref,
                    canonical_markdown_sha256=canonical_sha256,
                    validation_report_ref=validation_ref,
                    validation_report_sha256=hashlib.sha256(
                        validation_path.read_bytes()
                    ).hexdigest(),
                    completion_ref=completion_ref,
                    completion_sha256=hashlib.sha256(
                        (self.service.workspace / completion_ref).read_bytes()
                    ).hexdigest(),
                ).model_dump(mode="json"),
            )
        snapshot = FinalAuditSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
        expected_refs = {
            snapshot.subject_ref: snapshot.subject_sha256,
            snapshot.canonical_markdown_ref: snapshot.canonical_markdown_sha256,
            snapshot.validation_report_ref: snapshot.validation_report_sha256,
            snapshot.completion_ref: snapshot.completion_sha256,
        }
        run_root = (self.service.workspace / f"Work/runs/{run_id}").resolve()
        for ref, expected_sha256 in expected_refs.items():
            path = (self.service.workspace / ref).resolve()
            if (
                not path.is_relative_to(run_root)
                or not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected_sha256
            ):
                raise AgentWorkflowError(
                    f"final audit snapshot artifact changed before delivery: {ref}"
                )
        if (
            snapshot.run_id != run_id
            or snapshot.subject_ref != subject_ref
            or snapshot.completion_ref != completion_ref
            or snapshot.subject_revision != subject_revision
        ):
            raise AgentWorkflowError("final audit snapshot identity is stale")
        validation = ValidationReport.model_validate_json(
            (
                self.service.workspace / snapshot.validation_report_ref
            ).read_text(encoding="utf-8")
        )
        if (
            not validation.passed
            or validation.subject_ref != snapshot.canonical_markdown_ref
            or validation.subject_revision != snapshot.subject_revision
            or validation.content_sha256 != snapshot.canonical_markdown_sha256
        ):
            raise AgentWorkflowError("final audit snapshot validation binding is stale")
        _, audited_canonical = self._delivery_projection(state, audited)
        snapshot_canonical = (
            self.service.workspace / snapshot.canonical_markdown_ref
        ).read_text(encoding="utf-8")
        if audited_canonical != snapshot_canonical:
            raise AgentWorkflowError(
                "delivery subject differs from the final audited canonical snapshot"
            )
        current = state.get("edited_report")
        if (
            current is not None
            and current.model_dump(mode="json") != audited.model_dump(mode="json")
        ):
            raise AgentWorkflowError(
                "in-memory edited report changed after final audit completion"
            )
        state["final_audit_snapshot_ref"] = snapshot_ref
        return audited, snapshot_ref

    def _delivery_projection(
        self,
        state: dict,
        edited: EditedReportSubmission,
        claims: list | None = None,
    ) -> tuple[ApprovedReport, str]:
        """Build the exact citation-bound Markdown later passed to the DOCX renderer."""

        if claims is None:
            claims = [
                claim
                for module in state.get("module_submissions", {}).values()
                for claim in module.claims
            ]
        ledger = ClaimLedger(
            claims=claims,
            sources=SourceLedger(self.service.workspace, state["run_id"]).records,
        )
        tables, photos = ReportAssetAssembler(self.service.workspace).build(
            state.get("evidence_items", []),
            state.get("photo_assets", []),
            claims,
            edited,
        )
        report = ApprovedReport(
            title=edited.title,
            assessment_background=edited.assessment_background,
            findings_overview=edited.findings_overview,
            regional_executive_summary=edited.regional_executive_summary,
            module_narratives=dict(edited.module_narratives),
            risk_panorama=edited.risk_panorama,
            dimension_risk_analysis=edited.dimension_risk_analysis,
            data_gap_analysis=edited.data_gap_analysis,
            improvement_action_plan=edited.improvement_action_plan,
            special_topic_plan=edited.special_topic_plan,
            special_topic_analysis=edited.special_topic_analysis,
            ledger=ledger,
            tables=tables,
            photos=photos,
        )
        return report, ledger.bind_citations(PdsDocxRenderer._compose_markdown(report))

    def _deliver(self, state: dict) -> None:
        if "final_review_completion_ref" not in state:
            raise AgentWorkflowError(
                "delivery requires an independent final review completion record"
            )
        edited, final_audit_snapshot_ref = self._validated_final_audit_subject(
            state
        )
        self._validate_module_exports(state, "delivery")
        self._write_handoff_contracts(state)
        state["edited_report"] = edited
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        ledger = ClaimLedger(
            claims=claims,
            sources=SourceLedger(self.service.workspace, state["run_id"]).records,
        )
        claim_ledger_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/ledgers/claims.json",
            ledger.model_dump(mode="json"),
        )
        source_ledger_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/ledgers/sources.json",
            [source.model_dump(mode="json") for source in ledger.sources],
        )
        evidence_snapshot_path = self.service.store.write_jsonl(
            f"Work/runs/{state['run_id']}/evidence.jsonl",
            [item.model_dump(mode="json") for item in state.get("evidence_items", [])],
        )
        approved_module_paths = {
            module_id: self.service.store.write_json(
                f"Work/runs/{state['run_id']}/approved-modules/{module_id}.json",
                state["module_submissions"][module_id].model_dump(mode="json"),
            )
            for module_id in REPORT_MODULE_IDS
        }
        edited_submission_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/edited-submission.json",
            edited.model_dump(mode="json"),
        )
        request_snapshot_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/request-snapshot.json",
            state["request"].model_dump(mode="json"),
        )
        photo_manifest_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/photo-manifest.json",
            {"assets": [asset.model_dump(mode="json") for asset in state.get("photo_assets", [])]},
        )
        report, delivery_markdown = self._delivery_projection(
            state,
            edited,
            claims,
        )
        # Keep the report-state input run-scoped.  The delivery package is the
        # only public snapshot consumed by version publication; a global
        # ``Work/report-state.json`` view would let a later run overwrite the
        # current receipt's provenance.
        report_state_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/report-state.json",
            report.model_dump(mode="json"),
        )
        self._validate_final_report_structure(state, delivery_markdown, "delivery-final")
        markdown_path = self.service.store.write_text(
            "Outputs/Reports/配电安全专家咨询报告.md", delivery_markdown
        )
        source_index_markdown = ledger.source_index_markdown(
            evidence_items=state.get("evidence_items", []),
            photo_assets=state.get("photo_assets", []),
        )
        source_index_path = self.service.store.write_text(
            f"Work/runs/{state['run_id']}/source-index/证据与来源索引.md",
            source_index_markdown.rstrip() + "\n",
        )
        source_index_docx_path = (
            self.service.workspace
            / f"Work/runs/{state['run_id']}/source-index/证据与来源索引.docx"
        )
        SourceIndexDocxRenderer.render(
            source_index_markdown.rstrip() + "\n",
            source_index_docx_path,
        )
        selected_template, template_source = self.service.resolve_report_template(
            state["run_id"]
        )
        template_snapshot = (
            self.service.workspace / f"Work/runs/{state['run_id']}/templates/report_template.docx"
        )
        (
            template_snapshot,
            template_sha256,
            template_blob_ref,
        ) = self.service.snapshot_content(
            selected_template,
            template_snapshot,
        )
        selected_template_ref = (
            selected_template.relative_to(self.service.workspace).as_posix()
            if selected_template.is_relative_to(self.service.workspace)
            else "manyselves/templates/reporting/report_template.docx"
        )
        template_provenance_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/template-provenance.json",
            {
                "source": template_source,
                "selected_path": selected_template_ref,
                "snapshot_path": template_snapshot.relative_to(self.service.workspace).as_posix(),
                "blob_ref": template_blob_ref.as_posix(),
                "sha256": template_sha256,
            },
        )
        output = self.service.workspace / "Outputs/Reports/配电安全专家咨询报告.docx"
        render_request = RenderRequest(
            run_id=state["run_id"],
            source_markdown_ref=markdown_path.relative_to(self.service.workspace),
            template_ref=template_snapshot.relative_to(self.service.workspace),
            output_ref=output.relative_to(self.service.workspace),
        )
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/render-request.json",
            render_request.model_dump(mode="json"),
        )
        render = PdsDocxRenderer(PackagedV2DocxCore(template_snapshot)).render(
            report,
            output,
            approved_markdown=delivery_markdown,
            before_publish=lambda: validate_bound_project_write_lease(
                self.service.workspace
            ),
        )
        render_log = render.model_dump(mode="json")
        render_log["template"] = {
            "source": template_source,
            "selected_path": selected_template_ref,
            "sha256": template_sha256,
        }
        self.service.store.write_json("Outputs/Reports/render-log.json", render_log)
        render_result_ref = Path(f"Work/runs/{state['run_id']}/render-result.json")
        self.service.store.write_json(
            render_result_ref.as_posix(),
            RenderResult(
                status="completed",
                run_id=state["run_id"],
                source_markdown_ref=markdown_path.relative_to(self.service.workspace),
                output_ref=output.relative_to(self.service.workspace),
                render_log_ref=Path("Outputs/Reports/render-log.json"),
                template_sha256=template_sha256,
                output_sha256=hashlib.sha256(output.read_bytes()).hexdigest(),
                protected_prose_verified=True,
            ).model_dump(mode="json"),
        )
        receipt = ProjectDelivery(
            self._delivery_root(self.service.workspace, state["run_id"])
        ).deliver(
            DeliveryPackage(
                # One run owns one immutable report-version namespace.  The
                # shared human-facing output path is bound separately by the
                # output-owner contract after verified completion.
                report_id=state["run_id"],
                version=state["run_id"],
                module_files={
                    module_id: self.service.workspace / f"Outputs/Modules/{module_id}.md"
                    for module_id in REPORT_MODULE_IDS
                },
                final_docx=output,
                report_state=report_state_path,
                source_index=source_index_path,
                source_index_docx=source_index_docx_path,
            )
        )
        receipt_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/delivery-receipt.json",
            receipt.model_dump(mode="json"),
        )
        # A receipt is durable before any version/archive work begins.  A
        # crash after this point is therefore an archive/version recovery, not
        # a reason to invoke a Provider again.
        completion_ref = f"Work/runs/{state['run_id']}/delivery-completion.json"
        state["delivery_status"] = "receipt_persisted"
        state["output_artifacts"] = self._delivery_output_artifacts(
            final_review_ref=state["final_review_completion_ref"],
            delivery_manifest_ref=receipt.manifest_path.relative_to(self.service.workspace),
            source_index_ref=receipt.source_index.relative_to(self.service.workspace),
            source_index_docx_ref=receipt.source_index_docx.relative_to(self.service.workspace),
        )
        self.service.store.write_json(
            completion_ref,
            {
                "run_id": state["run_id"],
                "status": "receipt_persisted",
                "delivery_status": "receipt_persisted",
                "delivery_receipt_ref": receipt_path.relative_to(self.service.workspace).as_posix(),
                "report_version_id": None,
                "final_audit_snapshot_ref": final_audit_snapshot_ref,
                "output_artifacts": [
                    artifact.model_dump(mode="json") for artifact in state["output_artifacts"]
                ],
            },
        )
        provenance_loader = getattr(self.agent_runner, "skill_provenance", None)
        skill_provenance = (
            provenance_loader()
            if provenance_loader is not None
            else [
                SkillProvenance(
                    skill_id=skill.id,
                    version=skill.version,
                    sha256=hashlib.sha256(skill.source_path.read_bytes()).hexdigest(),
                    scope="packaged",
                )
                for skill in ModuleSkillLibrary.packaged().skills
            ]
        )
        template_skill_refs = state.get("template_skill_refs", {})
        template_skill_core = template_skill_refs.get("core")
        if template_skill_core:
            skill_provenance = [
                *skill_provenance,
                SkillProvenance(
                    skill_id="report-template-writing",
                    version=state["run_id"],
                    sha256=hashlib.sha256(
                        (self.service.workspace / template_skill_core).read_bytes()
                    ).hexdigest(),
                    scope="project",
                ),
            ]
        summary_refs = SessionSummaryStore(self.service.workspace).relevant(run_id=state["run_id"])
        summary_refs = list(
            dict.fromkeys([*state.get("inherited_summary_refs", []), *summary_refs])
        )
        version_store = ReportVersionStore(self.service.workspace)
        # The typed receipt owns every package view.  In particular, source
        # indexes are never reconstructed as hand-written Outputs artifacts
        # while publishing a version.
        additional_artifacts: dict[str, Path] = {
            "delivery_manifest": receipt.manifest_path.relative_to(self.service.workspace),
            "claim_ledger": claim_ledger_path.relative_to(self.service.workspace),
            "source_ledger": source_ledger_path.relative_to(self.service.workspace),
            "evidence": evidence_snapshot_path.relative_to(self.service.workspace),
            **{
                f"module_submission:{module_id}": path.relative_to(self.service.workspace)
                for module_id, path in approved_module_paths.items()
            },
            "edited_submission": edited_submission_path.relative_to(self.service.workspace),
            "canonical_markdown": markdown_path.relative_to(self.service.workspace),
            "render_request": Path(f"Work/runs/{state['run_id']}/render-request.json"),
            "render_result": render_result_ref,
            "handoff_contracts": Path(
                f"Work/runs/{state['run_id']}/handoff-contracts.json"
            ),
            "final_review_completion": Path(state["final_review_completion_ref"]),
            "final_audit_snapshot": Path(final_audit_snapshot_ref),
            **(
                {
                    "cross_review_completion": Path(state["cross_review_completion_ref"]),
                }
                if state.get("cross_review_completion_ref")
                else {}
            ),
            "report_request": request_snapshot_path.relative_to(self.service.workspace),
            "photo_manifest": photo_manifest_path.relative_to(self.service.workspace),
            "report_template": template_snapshot.relative_to(self.service.workspace),
            "template_provenance": template_provenance_path.relative_to(
                self.service.workspace
            ),
            **{
                f"template_skill:{part}": Path(ref)
                for part, ref in template_skill_refs.items()
            },
            **{
                f"photo_asset:{asset.id}": asset.path
                for asset in state.get("photo_assets", [])
            },
        }
        version = version_store.publish_from_delivery(
            receipt,
            receipt_path.relative_to(self.service.workspace),
            additional_artifacts=additional_artifacts,
            session_summary_refs=summary_refs,
            skill_provenance=skill_provenance,
        )
        state["delivery_status"] = "delivered"
        self.service.store.write_json(
            completion_ref,
            {
                "run_id": state["run_id"],
                "status": "delivered",
                "delivery_status": "delivered",
                "delivery_receipt_ref": receipt_path.relative_to(self.service.workspace).as_posix(),
                "report_version_id": version.version_id,
                "final_audit_snapshot_ref": final_audit_snapshot_ref,
                "output_artifacts": [
                    artifact.model_dump(mode="json") for artifact in state["output_artifacts"]
                ],
            },
        )
        state["delivery_to_version_cas_metrics"] = (
            version_store.content_store.metrics_snapshot()
        )
        state["report_version"] = version
        state["delivery_status"] = "archive_pending"
        self.service.store.write_json(
            completion_ref,
            {
                "run_id": state["run_id"],
                "status": "archive_pending",
                "delivery_status": "archive_pending",
                "delivery_receipt_ref": receipt_path.relative_to(self.service.workspace).as_posix(),
                "report_version_id": version.version_id,
                "final_audit_snapshot_ref": final_audit_snapshot_ref,
                "output_artifacts": [
                    artifact.model_dump(mode="json") for artifact in state["output_artifacts"]
                ],
            },
        )
        archive_error: str | None = None
        try:
            storage_plan = ReportingRetentionPlanner(self.service.workspace).generate()
            state["storage_usage_ref"] = "Work/storage-usage.json"
            state["retention_plan_ref"] = "Work/retention-plan.json"
            state["storage_usage"] = storage_plan["usage"]
            state["delivery_status"] = "archived"
            final_status = "archived"
        except Exception as exc:
            # The receipt and version are already durable and hash-verified;
            # retention is advisory and must never replay Provider work.
            archive_error = str(exc)
            state["delivery_status"] = "delivered_with_archive_warning"
            state["delivery_warning"] = archive_error
            final_status = "archive_failed"
        self.service.store.write_json(
            completion_ref,
            {
                "run_id": state["run_id"],
                "status": final_status,
                "delivery_status": state["delivery_status"],
                "warning": archive_error,
                "delivery_receipt_ref": receipt_path.relative_to(self.service.workspace).as_posix(),
                "report_version_id": version.version_id,
                "final_audit_snapshot_ref": final_audit_snapshot_ref,
                "output_artifacts": [
                    artifact.model_dump(mode="json") for artifact in state["output_artifacts"]
                ],
            },
        )
        state["delivery_completion_ref"] = completion_ref

    @staticmethod
    def _delivery_root(workspace: Path, run_id: str) -> Path:
        """Keep immutable delivery snapshots inside their owning run."""

        return Path(workspace) / "Work" / "runs" / run_id / "delivery"

    @staticmethod
    def _delivery_output_artifacts(
        *,
        final_review_ref: str,
        delivery_manifest_ref: Path,
        source_index_ref: Path | None = None,
        source_index_docx_ref: Path | None = None,
    ) -> list[OutputArtifact]:
        """Declare only artifacts that the current delivery lifecycle creates."""

        return [
            *(
                OutputArtifact(
                    kind="module", path=Path(f"Outputs/Modules/{module_id}.md"), module_id=module_id
                )
                for module_id in REPORT_MODULE_IDS
            ),
            OutputArtifact(kind="review", path=Path(final_review_ref)),
            OutputArtifact(kind="report", path=Path("Outputs/Reports/配电安全专家咨询报告.md")),
            OutputArtifact(kind="report", path=Path("Outputs/Reports/配电安全专家咨询报告.docx")),
            OutputArtifact(
                kind="report",
                path=source_index_ref or Path("Outputs/Reports/证据与来源索引.md"),
            ),
            OutputArtifact(
                kind="report",
                path=source_index_docx_ref
                or Path("Outputs/Reports/证据与来源索引.docx"),
            ),
            OutputArtifact(
                kind="run",
                path=delivery_manifest_ref,
            ),
        ]

    @staticmethod
    def _canonical_markdown(edited: EditedReportSubmission) -> str:
        """Adapt approved synthesis to the fixed four-block Render contract."""

        return compose_canonical_markdown(
            CanonicalReportContent(
                title=edited.title,
                assessment_background=edited.assessment_background,
                findings_overview=edited.findings_overview,
                regional_executive_summary=edited.regional_executive_summary,
                module_narratives=dict(edited.module_narratives),
                risk_panorama=edited.risk_panorama,
                dimension_risk_analysis=edited.dimension_risk_analysis,
                data_gap_analysis=edited.data_gap_analysis,
                improvement_action_plan=edited.improvement_action_plan,
                special_topic_plan=edited.special_topic_plan,
                special_topic_analysis=edited.special_topic_analysis,
                tables=[
                    CanonicalMarkdownTable(
                        title=table.title,
                        headers=table.headers,
                        rows=table.rows,
                        source_ids=table.source_ids,
                    )
                    for table in edited.tables
                ],
            )
        )
