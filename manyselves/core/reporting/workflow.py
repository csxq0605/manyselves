"""Outer report workflow: orchestration only; professional reasoning stays in AgentLoop."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import shutil
import time
from contextvars import Token
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from pydantic import Field

from manyselves.capabilities.distribution_reporting.domain.claim_ledger import ClaimLedger
from manyselves.capabilities.distribution_reporting.domain.evidence_readiness import (
    EvidenceReadinessPolicy,
    ReportingBlockedError,
)
from manyselves.capabilities.distribution_reporting.domain.final_specialization import (
    final_lane_specialization,
)
from manyselves.capabilities.distribution_reporting.domain.photo_bindings import (
    runtime_photo_ids,
)
from manyselves.capabilities.distribution_reporting.domain.report_markdown import (
    CanonicalMarkdownTable,
    CanonicalReportContent,
    compose_canonical_markdown,
)
from manyselves.capabilities.distribution_reporting.domain.revision_diff import (
    build_revision_diff,
)
from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    activate_report_taxonomy,
    compose_module_markdown,
    parse_report_taxonomy_workbook,
    reset_report_taxonomy,
    resolve_submodule,
)
from manyselves.capabilities.distribution_reporting.runtime.delivery_tools import (
    DeliveryTools,
    _DeliveryPreparationDependencies,
)
from manyselves.capabilities.distribution_reporting.runtime.intake.special_topics import (
    load_special_topic_plan,
)
from manyselves.capabilities.distribution_reporting.runtime.intake.wps_images import (
    extract_wps_images,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CHIEF_SECTION_RESULT_PART_IDS,
    FINAL_REPORT_SECTION_IDS,
    TEMPLATE_ROLE_SKILL_IDS,
    AgentRunStatus,
    ChapterScopedFinalReviewFinding,
    ChiefChapterLaneRevisionSubmission,
    ChiefChapterLaneSubmission,
    CrossDecisionPack,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FinalChapterLaneFindingSubmission,
    FinalChapterLaneVerdictSubmission,
    ModuleDispatchPlan,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    TemplateSkillBoundaryManifest,
    TemplateSkillSubmission,
    extra_numbered_submodule_headings,
    numbered_markdown_headings,
)
from manyselves.capabilities.distribution_reporting.runtime.models.delivery import (
    DeliveryContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    AggregateEditorInput,
    ChiefChapterLaneInput,
    ChiefEditorInput,
    CrossDecisionPackView,
    FinalAuditSnapshot,
    FinalChapterLaneInput,
    ModuleAuthoringInput,
    RequestedModuleChange,
    ReviewCompletionRecord,
    TemplateDistillationInput,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    ProjectManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    REPORT_MODULE_IDS,
    CostControlMode,
    CoverageMatrix,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    RevisionRequest,
    ScopeExpansionRequest,
    SpecialTopicPlan,
    chapter_section_ids,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    MainExceptionDecisionAcceptance,
    ModuleInitialReviewAcceptance,
    ModuleInitialReviewPreparation,
    ModuleRecheckAcceptance,
    ModuleRecheckPreparation,
    ModuleReviewPreflightProgress,
    ModuleRevisionPreparation,
)
from manyselves.capabilities.distribution_reporting.runtime.rendering.pds_docx_renderer import (
    ApprovedReport,
    PdsDocxRenderer,
)
from manyselves.capabilities.distribution_reporting.runtime.rendering.source_index_docx_renderer import (
    SourceIndexDocxRenderer,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (
    ProjectEvidenceIndex,
    project_evidence_locator,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    AggregateState,
    ArtifactRef,
    CohortBarrier,
    CrossOwnerBarrier,
    CrossOwnerCompletion,
    LaneAttemptRecord,
    LaneCompletion,
    LaneExceptionCandidate,
    LaneTaskSpec,
    RecoveryStateStore,
    TaskAttemptStore,
    WorkflowReducer,
    current_bound_project_write_lease,
)

from ...kernel.definitions import RecoveryPolicyDefinition
from ..usage_ledger import UsageLedger
from .agent_runner import ReportingAgentRunner
from .assets import (
    ReportAssetAssembler,
    expand_approved_module_markers,
    validate_aggregate_retention,
    validate_editor_protection,
    validate_editor_quality,
    validate_existing_markdown_modules,
    validate_final_report_markdown,
    validate_module_markdown_consistency,
)
from .chapter_parallel import CHAPTER_SECTION_IDS, active_chapters
from .config import AgentDefinition as ReportingAgentDefinition
from .cost_control import StageCostController
from .distributed_runtime import LocalEventStore
from .input_snapshot import RunInputSnapshotStore
from .research.knowledge_context import KnowledgeContextBuilder
from .review_lifecycle import (
    DeferredMainDecision,
    accept_module_initial_review,
    accept_module_initial_review_preflight_revision,
    accept_module_recheck,
    accept_module_recheck_preflight_revision,
    accept_module_revision,
    prepare_module_initial_review,
    prepare_module_initial_review_step,
    prepare_module_recheck,
    prepare_module_revision,
    request_module_revision,
    resume_module_initial_review,
    resume_module_recheck,
    run_cross_review,
    run_module_review,
    verify_cross_owner_barrier,
)
from .scheduling import (
    AdaptiveTaskScheduler,
    SchedulingCandidate,
    TaskTimingHistory,
)

if TYPE_CHECKING:
    from .service import ReportingService


TEMPLATE_SKILL_ROOT = Path("Work/report-template-role-skills")
TEMPLATE_SKILL_SOURCE = TEMPLATE_SKILL_ROOT / "source.json"
FINAL_REVIEW_COMPLETION_SESSION_KEYS = frozenset(
    {"chief-editor-auditor", "final-chapter-wave"}
)


@dataclass(slots=True)
class _ModuleLaneAttemptContext:
    """Durable bookkeeping context for one module lane attempt."""

    module_id: str
    state: dict
    lane_state: dict
    workflow_id: str
    spec: LaneTaskSpec
    spec_ref: str
    lane_attempt_id: str
    started_at_ns: int
    event_store: LocalEventStore
    attempt_ref: str


@dataclass(slots=True)
class _ModuleAuthoringPreparationContext:
    """Prepared current-module authoring input before the Provider turn."""

    module_id: str
    state: dict
    workflow_id: str
    specialist_id: str
    envelope: TaskEnvelope
    resumed_payload: ModuleSubmission | None
    revision: int
    review: bool
    checkpoint: bool


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


class StageRecoveryCoordinator:
    """Thin adapter for stage/lane recovery owned by the runtime.

    The workflow deliberately does not interpret ``workflow-state.json`` as a
    checkpoint.  ``RecoveryStateStore`` is the sole source of stage/lane and
    aggregate status; workflow-state remains a projection for UI/inspection.
    The store records business state only and is never consulted with hashes,
    CAS handles, or provider turn history.
    """

    def __init__(self, runner: "ReportWorkflowRunner", state: dict[str, Any]):
        self.runner = runner
        self.state = state
        self.run_id = str(state["run_id"])
        self.store = RecoveryStateStore(runner.service.workspace, self.run_id)

    async def mark_lane(self, stage: str, lane_id: str, *, status: str = "completed", result_ref: str | None = None, error: str | None = None, revision: int = 0) -> Any:
        """Project one lane terminal into the runtime recovery store."""

        return self.store.record_lane_attempt(
            {
                "run_id": self.run_id,
                "stage": stage,
                "lane_id": lane_id,
                "task_id": lane_id,
                "attempt": 1,
                "revision": revision,
                "status": status,
                "result_ref": result_ref,
                "error": error,
            }
        )

    async def mark_aggregate(self, stage: str, *, lane_ids: list[str], result_ref: str | None = None, revision: int = 0, status: str = "completed", error: str | None = None) -> Any:
        return self.store.record_aggregate(
            AggregateState(
                run_id=self.run_id,
                stage=stage,
                lane_ids=list(lane_ids),
                result_ref=result_ref,
                revision=revision,
                status=status,
                error=error,
            )
        )

    @property
    def has_backend(self) -> bool:
        return True

    async def load_completed_lanes(
        self,
        stage: str,
        lane_ids: list[str] | None = None,
        *,
        expected_revisions: dict[str, int] | None = None,
    ) -> dict[str, Any]:
        loaded = self.store.load_completed_lanes(
            stage,
            lane_ids,
            expected_revisions=expected_revisions,
        )
        return dict(loaded)

    async def retry_lanes(
        self,
        stage: str,
        lane_ids: list[str],
        lane_runner: Any | None = None,
    ) -> Any:
        """Explicitly dispatch only the named failed lanes."""

        if lane_runner is None:
            return self.store.retry_failed_lanes(stage, lane_ids)
        result = self.store.retry_lanes(stage, lane_ids, lane_runner)
        if inspect.isawaitable(result):
            return await result
        return result

    async def aggregate(self, stage: str, reducer: Any | None = None) -> Any:
        """Freeze/drain a parallel stage and run exactly one reducer."""

        reduced = reducer() if reducer is not None else None
        if inspect.isawaitable(reduced):
            reduced = await reduced
        if reduced is None:
            return None
        if isinstance(reduced, AggregateState):
            aggregate = reduced
        elif isinstance(reduced, dict):
            aggregate = AggregateState.model_validate(
                {"run_id": self.run_id, "stage": stage, **reduced}
            )
        else:
            aggregate = AggregateState(
                run_id=self.run_id,
                stage=stage,
                status="completed",
            )
        return self.store.record_aggregate(aggregate)

    async def invalidate_lanes(self, stage: str, lane_ids: list[str]) -> Any:
        return self.store.invalidate_lanes(stage, lane_ids)

    async def rollback_to_aggregate(self, stage: str) -> Any:
        return self.store.rollback_to_aggregate(stage)


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

    def _recovery_store(self, state: dict) -> RecoveryStateStore:
        """Return the run's business-state recovery store.

        This is intentionally constructed from the workspace/run identity on
        every orchestration boundary.  ``workflow-state.json`` is only a
        status projection and is never used to decide whether paid work is
        reusable.
        """

        return RecoveryStateStore(self.service.workspace, str(state["run_id"]))

    @staticmethod
    def _recovery_stage_name(stage: str) -> str:
        return stage.replace("_", "-")

    def _record_recovery_lane(
        self,
        state: dict,
        *,
        stage: str,
        lane_id: str,
        status: str,
        result_ref: str | None = None,
        error: str | None = None,
        revision: int = 0,
    ) -> None:
        """Persist one lane terminal without content identity checks."""

        store = self._recovery_store(state)
        current = store.load_lane_state(self._recovery_stage_name(stage), lane_id)
        self._recovery_store(state).record_lane_attempt(
            {
                "run_id": state["run_id"],
                "stage": self._recovery_stage_name(stage),
                "lane_id": lane_id,
                "task_id": lane_id,
                "attempt": (current.attempt + 1) if current is not None else 1,
                "revision": revision,
                "status": status,
                "result_ref": result_ref,
                "error": error,
            }
        )

    def _record_recovery_aggregate(
        self,
        state: dict,
        *,
        stage: str,
        lane_ids: list[str],
        result_ref: str | None = None,
        revision: int = 0,
        status: str = "completed",
        error: str | None = None,
    ) -> None:
        """Persist one stage aggregate after the all-ready lane drain."""

        store = self._recovery_store(state)
        current = store.load_aggregate(self._recovery_stage_name(stage))
        if current is not None and revision <= current.revision:
            revision = current.revision + 1
        aggregate = AggregateState(
            run_id=state["run_id"],
            stage=self._recovery_stage_name(stage),
            lane_ids=list(lane_ids),
            result_ref=result_ref,
            revision=revision,
            status=status,
            error=error,
        )
        try:
            store.record_aggregate(aggregate)
        except Exception as exc:
            stage_name = self._recovery_stage_name(stage)
            store.recover_aggregate_failure(
                stage_name,
                lane_ids,
                previous_stage=self._previous_recovery_stage(stage_name),
                reason=str(exc),
            )
            raise

    @staticmethod
    def _previous_recovery_stage(stage: str) -> str | None:
        """Return the last successful aggregate before a parallel stage."""

        if stage == "cross":
            return "module"
        if stage == "chief":
            return "cross"
        if stage == "final-initial":
            return "chief"
        if stage.startswith("chief-revision-r"):
            try:
                revision = int(stage.rsplit("r", 1)[1])
            except ValueError:
                return "final-initial"
            return "final-initial" if revision <= 1 else f"final-recheck-r{revision - 1}"
        if stage.startswith("final-recheck-r"):
            return "chief-revision-r" + stage.rsplit("r", 1)[1]
        if stage == "final":
            return "chief"
        return None

    def _retry_recovery_aggregate(
        self,
        state: dict,
        *,
        stage: str,
        reason: str,
    ) -> None:
        """Record an explicit reducer retry; no Provider lane is replayed."""

        self._recovery_store(state).retry_aggregate(
            self._recovery_stage_name(stage),
            reason=reason,
        )

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
        recovery_policy: RecoveryPolicyDefinition | None = None,
        definition_override: ReportingAgentDefinition | None = None,
    ):
        self._raise_if_cancel_requested(envelope.run_id)
        if self._budget is not None:
            await self._budget.acquire(agent_id)
        visible_agent_id = (
            session_key
            if session_key is not None
            and session_key.startswith(
                ("module-auditor-", "cross-owner-", "chief-chapter-", "final-chapter-")
            )
            else agent_id
        )
        try:
            board_task = self.service.task_board.create_task(
                source="report-workflow",
                target=visible_agent_id,
                brief=f"reporting:{workflow_id}:{agent_id}:{envelope.task_id}",
                session_id=workflow_id,
            )
            self.service.task_board.start_task(
                board_task.task_id,
                target_agent=visible_agent_id,
                session_id=workflow_id,
            )
            try:
                result = await self.agent_runner.run(
                    definition_override or self.agents[agent_id],
                    envelope,
                    artifacts,
                    workflow_id=workflow_id,
                    session_key=session_key,
                    recovery_policy=recovery_policy,
                )
            except asyncio.CancelledError:
                self.service.task_board.cancel_task(
                    board_task.task_id,
                    target_agent=visible_agent_id,
                    session_id=workflow_id,
                )
                raise
            except BaseException:
                self.service.task_board.fail_task(
                    board_task.task_id,
                    target_agent=visible_agent_id,
                    session_id=workflow_id,
                )
                raise
            if result.status is AgentRunStatus.COMPLETED:
                self.service.task_board.complete_task(
                    board_task.task_id,
                    target_agent=visible_agent_id,
                    session_id=workflow_id,
                )
            elif result.status is AgentRunStatus.BLOCKED:
                self.service.task_board.block_task(
                    board_task.task_id,
                    target_agent=visible_agent_id,
                    session_id=workflow_id,
                )
            else:
                self.service.task_board.fail_task(
                    board_task.task_id,
                    target_agent=visible_agent_id,
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
            skill_id: root / skill_id / "SKILL.md"
            for skill_id in TEMPLATE_ROLE_SKILL_IDS
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
            f"{skill_id}/SKILL.md": content
            for skill_id, content in submission.skills.items()
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
            + self._template_skill_context(state, f"author-{module_id}")
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
            required_part_ids=list(TEMPLATE_ROLE_SKILL_IDS),
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
                "报告全流程复用的角色与模块 Skill。直接产出五个模块作者 Skill、"
                "五个模块 Auditor Skill、三个 Chief 章节 Skill 和一个 Final Auditor Skill；"
                "Cross 不使用模板 Skill。"
            ),
            input_refs=[distillation_input_ref, snapshot_ref],
            constraints=[
                "第一步且只调用一次 inspect_document(path=input contract 的 template_ref, max_chars=inspect_max_chars)；工具会在本任务中完整返回正文和版式结构，之后禁止再次读取或查找模板",
                "读取后先在内部对照至少三组正文样本，识别观察→证据限定→判断→原因→影响→建议的推进方式；不得输出思考过程",
                "唯一成功的结束方式是调用 submit_result 提交 template_skill_submission；不得用‘现在开始分析’、摘要或计划代替工具提交",
                "skills 必须精确包含 input required_part_ids；每一项都是可直接内联使用的完整 SKILL.md，仅含 name 与 description 两项 frontmatter，不链接共享 references",
                "author-2.1 至 author-2.5 分别面向该模块作者，将模板方法组织为证据限定、分析推进、行动建议、图证叙事和作者自检；模块差异只来自职责与报告位置，不得虚构专业知识",
                "auditor-2.1 至 auditor-2.5 分别面向该模块 Auditor，只提炼可观察审计准则、失败表现和复核动作，不得包含作者写作指令",
                "chief-editor-chapter-1、chief-editor-chapter-3、chief-editor-chapter-4 分别只包含对应章节的组织、综合、行动或专项分析方法；禁止生成共享 chief-editor Skill；final-auditor 只包含跨章节通用的最终验收准则和复核动作",
                "不得产出 Cross Skill。Cross 的专业化由各 2.x 检查 prompt 完成，不写作，也不继承模板方法",
                "模板中用于教相应身份完成任务的结构化样例必须去除项目事实后直接保留在该身份 Skill 中；不要另建共享 reference 或 Output Profile",
                "这些 Skill 样例描述报告内容应如何组织，不得重复 submit_result 的 JSON 字段样例；机器提交形状只服从当前任务 submission schema",
                "四类可复用方法必须包含去事实化结构样例；边界 manifest 由运行时从当前 input contract 确定性生成，不得由模型提交",
                "只迁移写作能力，不复制模板项目事实、具体数值、客户名称或原结论",
                "专家优化版只在本任务中作为一次性 Skill 蒸馏源；不得把其中的具体问题、风险判断、分析结论、建议内容、证据编号或项目措辞写入任何 Skill 文件",
                "不得迁移专业机理、标准名称、适用条件或带单位阈值；它们属于 Knowledge，不属于模板 Skill",
                "禁止把十四份长文本直接塞入 submit_result：用 write_result_part 按 required_part_ids 保存完整 Skill；先用 list_result_parts 确认状态，最终 skills 映射只提交对应 artifact_refs",
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
                        *(
                            TEMPLATE_SKILL_ROOT / skill_id / "SKILL.md"
                            for skill_id in TEMPLATE_ROLE_SKILL_IDS
                        ),
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
                "正在单独蒸馏报告模板；本次只更新固定模板职责 Skill，不启动报告写作。"
            )
            await self._distill_template_skill(state, workflow_id)
            self._checkpoint(state, activity, "completed")
            state["output_artifacts"] = [
                OutputArtifact(kind="skill", path=TEMPLATE_SKILL_ROOT / relative)
                for relative in (
                    *(f"{skill_id}/SKILL.md" for skill_id in TEMPLATE_ROLE_SKILL_IDS),
                    "boundary.json",
                    "source.json",
                )
            ]
            await self.service._notice(
                "模板职责 Skill 已更新至固定路径 Work/report-template-role-skills。"
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
    def _template_skill_context(state: dict, skill_id: str) -> str:
        texts = state.get("template_skill_text", {})
        content = texts.get(skill_id, "")
        if not content:
            return ""
        return (
            f'<template_role_skill id="{skill_id}" delivery_mode="inline">\n'
            f"{content}\n"
            "</template_role_skill>"
        )

    @classmethod
    def _chief_template_skill_context(
        cls,
        state: dict,
        chapter_ids: tuple[str, ...] | list[str] | set[str],
    ) -> str:
        """Inline complete Chief Skills for exactly the requested chapters."""

        requested = set(chapter_ids)
        unsupported = requested - set(CHAPTER_SECTION_IDS)
        if unsupported:
            raise AgentWorkflowError(
                f"unsupported Chief Skill chapters: {sorted(unsupported)}"
            )
        return "\n\n".join(
            context
            for chapter_id in CHAPTER_SECTION_IDS
            if chapter_id in requested
            for context in [
                cls._template_skill_context(
                    state, f"chief-editor-chapter-{chapter_id}"
                )
            ]
            if context
        )

    @classmethod
    def _final_template_skill_context(cls, state: dict, chapter_id: str) -> str:
        """Inline only the shared Final Skill; the contract owns chapter focus."""

        if chapter_id not in CHAPTER_SECTION_IDS:
            raise AgentWorkflowError(f"unsupported Final Skill chapter: {chapter_id}")
        return cls._template_skill_context(state, "final-auditor")

    @staticmethod
    def _domain_knowledge_context(text: str, provenance_ref: str) -> str:
        """Label sourced domain Knowledge separately from methods and project Evidence."""

        return (
            f'<domain_knowledge delivery_mode="inline" provenance_ref="{provenance_ref}" '
            'project_fact_authority="false">\n'
            f"{text}\n"
            "</domain_knowledge>"
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
        # workflow-state.json is a status projection only.  Recovery decisions
        # come from RecoveryStateStore lane/aggregate records.
        resume_checkpoint: dict | None = None
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
        taxonomy_token: Token | None = None
        try:
            await self._activate_cost_resume(state)
            await self._recover_pending_cost_boundary(resume_checkpoint)
            recovering_cost_boundary = False
            await self.service._notice("正在整理项目资料并建立可追溯证据入口。")
            taxonomy_token = await self._prepare(state)
            if state.get("resume"):
                await self.service._notice(
                    "按 RecoveryStateStore 恢复已完成 lane；workflow-state.json 仅作状态投影。"
                )
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
                "正在从固定路径 Work/report-template-role-skills 读取模板职责 Skill。"
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
                    chief_envelope=state.get("chief_editor_envelope"),
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
            if taxonomy_token is not None:
                reset_report_taxonomy(taxonomy_token)

    async def aggregate_existing(self, state: dict) -> None:
        """Create only the chief editor for five already-written module reports."""

        request = state["request"]
        run_id = state["run_id"]
        workflow_id = f"aggregate-existing-report:{run_id}"
        # Status projection only; aggregate recovery is owned by
        # RecoveryStateStore below.
        resume_checkpoint: dict | None = None
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
        imported_evidence_path = (
            self.service.workspace / f"Work/runs/{run_id}/evidence.jsonl"
        )
        state["evidence_items"] = (
            [
                EvidenceItem.model_validate_json(line)
                for line in imported_evidence_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if imported_evidence_path.is_file()
            else []
        )
        imported_photo_path = (
            self.service.workspace / f"Work/runs/{run_id}/context/photo-manifest.json"
        )
        state["photo_assets"] = []
        if imported_photo_path.is_file():
            imported_photo_payload = json.loads(
                imported_photo_path.read_text(encoding="utf-8")
            )
            state["photo_assets"] = [
                PhotoAsset.model_validate(item)
                for item in imported_photo_payload.get("assets", [])
            ]
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
        try:
            if structured_modules:
                validate_module_markdown_consistency(structured_modules)
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
                    self.service.workspace,
                    run_id,
                    global_root=getattr(self.service, "global_root", None),
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
                            "special_topic_analysis 必须严格按 special_topic_plan 的顺序输出全部且仅输出对应的 ### 4.n 顶层小节；允许在所属 4.n 内使用 #### 4.n.m 等从属小标题，但不得新增顶层 4.n 小节",
                            "专项分析可使用已内联的项目/全局 Knowledge 和模型世界知识补充机理、方案权衡与行业实践；必须区分当前项目事实、可追溯参考和通用专业判断，禁止把通用知识写成客户事实",
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
                        self._chief_template_skill_context(
                            state,
                            active_chapters(
                                include_chapter_four=special_topic_plan is not None
                            ),
                        ),
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
            imported_source_records = SourceLedger(
                self.service.workspace, run_id
            ).records
            ledger = (
                ClaimLedger(claims=[], sources=imported_source_records)
                if imported_source_records
                else None
            )
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
                validate_aggregate_retention(payload, source_modules)
                if claims:
                    validate_editor_protection(payload, claims)
                self._write_aggregate_chief_completion(
                    state,
                    envelope,
                    payload,
                )
            else:
                validate_aggregate_retention(payload, source_modules)
                await self.service._notice(
                    "已恢复本 run 的汇总总编候选稿，未重复调用 Chief。"
                )
            if structured_modules:
                ledger = ClaimLedger(
                    claims=claims,
                    sources=(
                        imported_source_records
                        or list(structured_sources.values())
                    ),
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
        # workflow-state.json is never used as a revision recovery controller.
        resume_checkpoint: dict | None = None
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
                await self.service._notice(
                    "修订 run 按 RecoveryStateStore 恢复 lane；不恢复任意 Provider/tool 中途 turn。"
                )
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
                    chief_envelope=state.get("chief_editor_envelope"),
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
        """Persist a status projection (never a recovery controller)."""
        completed_modules = sorted(state.get("module_submissions", {}))
        projection = {
            "workflow_id": f"full-power-distribution-report:{state['run_id']}",
            "run_id": state["run_id"],
            "activity": activity,
            "status": status,
            "completed_modules": completed_modules,
            "completed_module_count": len(completed_modules),
            "module_review_completion_refs": dict(state.get("module_review_completion_refs", {})),
            "stage_refs": {
                key: state.get(key)
                for key in (
                    "module_lane_barrier_ref",
                    "cross_owner_barrier_ref",
                    "cross_review_completion_ref",
                    "chief_editor_completion_ref",
                    "final_review_completion_ref",
                    "delivery_completion_ref",
                )
                if state.get(key)
            },
            "aggregate_refs": dict(state.get("aggregate_refs", {})),
            "output_refs": [
                str(item.path)
                for item in state.get("output_artifacts", [])
                if getattr(item, "path", None) is not None
            ],
            "error": error,
            "budget": self._budget.snapshot() if self._budget is not None else None,
        }
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            projection,
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
            for name, ref in expected_refs.items():
                self._require_current_run_artifact(
                    run_id,
                    ref,
                    label=f"aggregate Chief {name}",
                )
            persisted_envelope = TaskEnvelope.model_validate_json(
                (
                    self.service.workspace / expected_refs["envelope"]
                ).read_text(encoding="utf-8")
            )
            if (
                persisted_envelope.run_id != run_id
                or persisted_envelope.task_id != "aggregate-existing"
                or persisted_envelope.agent_id != "chief-editor"
                or persisted_envelope.revision != envelope.revision
                or persisted_envelope.input_contract_kind != "aggregate_editor_input"
                or persisted_envelope.input_contract_ref != expected_refs["editor_input"]
            ):
                return None
            for input_ref in persisted_envelope.input_refs:
                self._require_current_run_artifact(
                    run_id,
                    input_ref,
                    label="aggregate Chief input",
                )
            editor_input = json.loads(
                (
                    self.service.workspace / expected_refs["editor_input"]
                ).read_text(encoding="utf-8")
            )
            if (
                not isinstance(editor_input, dict)
                or editor_input.get("run_id") != run_id
                or editor_input.get("source_format")
                not in {"structured_module", "markdown"}
            ):
                return None
            source_manifest = json.loads(
                (
                    self.service.workspace / expected_refs["source_manifest"]
                ).read_text(encoding="utf-8")
            )
            if not isinstance(source_manifest, dict):
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
                reviewer_session_key=FINAL_REVIEW_COMPLETION_SESSION_KEYS,
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
        reviewer_session_key: str | set[str] | frozenset[str],
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
        chapter_scoped_final = (
            lifecycle == "final"
            and completion.reviewer_session_key == "final-chapter-wave"
        )
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

        expected_finding_kind = (
            "final_chapter_lane_finding_submission"
            if chapter_scoped_final
            else lifecycle_kinds[lifecycle]["finding"]
        )
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
                if index > 0 and not chapter_scoped_final:
                    regression_findings_by_id[finding_id] = finding

        expected_verdict_kind = (
            "final_chapter_lane_verdict_submission"
            if chapter_scoped_final
            else lifecycle_kinds[lifecycle]["verdict"]
        )
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
                if chapter_scoped_final:
                    if finding_id in findings_by_id:
                        raise ValueError(
                            "review completion contains duplicate immutable finding ids"
                        )
                    findings_by_id[finding_id] = finding

        if (
            not chapter_scoped_final
            and set(regression_findings_by_id) != set(embedded_new_findings_by_id)
        ):
            raise ValueError(
                "review completion regression finding refs do not match verdict new findings"
            )
        if (
            not chapter_scoped_final
            and regression_findings_by_id != embedded_new_findings_by_id
        ):
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
        """Accept only the stable module-owned Auditor identity."""

        del lifecycle_id
        return {f"module-auditor-{module_id}"}

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
                "final_chapter_lane_finding_submission",
                "final_chapter_lane_verdict_submission",
            }:
                values = artifact.get("residual_risks", [])
                if not isinstance(values, list) or not all(
                    isinstance(value, str) for value in values
                ):
                    raise ValueError("final review residual_risks must be a string list")
                residual_risks = values
        return residual_risks

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
        if ref is None:
            return None
        if not isinstance(ref, str) or not ref:
            raise AgentWorkflowError("Chief CrossDecisionPack ref is invalid")
        expected_ref = f"Work/runs/{run_id}/reviews/cross-decision-pack.json"
        if ref != expected_ref:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack ref is not the canonical current-run pack"
            )
        path = self._require_current_run_artifact(
            run_id, str(ref), label="CrossDecisionPack"
        )
        try:
            raw_pack = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw_pack, dict):
                raise ValueError("CrossDecisionPack JSON must be an object")
            pack = CrossDecisionPack.model_validate(raw_pack)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack is unreadable or invalid"
            ) from exc
        self._validate_cross_decision_pack_business(state, pack)
        state["cross_decision_pack"] = pack
        return pack

    def _validate_cross_decision_pack_business(
        self,
        state: dict,
        pack: CrossDecisionPack,
    ) -> None:
        """Validate the current-run Cross pack without digest/CAS decisions."""

        run_id = str(state["run_id"])
        if pack.run_id != run_id:
            raise AgentWorkflowError("Chief CrossDecisionPack belongs to another run")
        if set(pack.module_ids) != set(REPORT_MODULE_IDS):
            raise AgentWorkflowError(
                "Chief CrossDecisionPack must cover all five report modules"
            )
        completion_ref = state.get("cross_review_completion_ref")
        if completion_ref and pack.cross_review_completion_ref != completion_ref:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack is bound to another Cross completion"
            )
        completion_path = self._require_current_run_artifact(
            run_id,
            pack.cross_review_completion_ref,
            label="CrossDecisionPack completion",
        )
        try:
            raw_completion = json.loads(
                completion_path.read_text(encoding="utf-8")
            )
            completion = self._load_business_review_completion(raw_completion)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "Chief CrossDecisionPack completion is unreadable or invalid"
            ) from exc
        if (
            completion.run_id != run_id
            or completion.lifecycle != "cross"
            or completion.reviewer_agent_id != "cross-module-reviewer"
            or completion.reviewer_session_key
            not in {"cross-module-reviewer", "cross-owner-wave"}
        ):
            raise AgentWorkflowError(
                "Chief CrossDecisionPack completion has the wrong Cross owner"
            )
        for artifact_ref in (
            *completion.subject_refs,
            *completion.finding_refs,
            *completion.verdict_refs,
        ):
            artifact_path = self._require_current_run_artifact(
                run_id,
                artifact_ref,
                label="Cross completion artifact",
            )
            try:
                artifact_payload = json.loads(
                    artifact_path.read_text(encoding="utf-8")
                )
                if artifact_ref in completion.subject_refs:
                    subject = ModuleSubmission.model_validate(artifact_payload)
                    module_id = Path(artifact_ref).stem.split("-r", 1)[0]
                    if subject.module_id != module_id:
                        raise ValueError(
                            "Cross completion subject module identity does not match its ref"
                        )
                elif artifact_ref in completion.finding_refs:
                    CrossReviewFindingSubmission.model_validate(artifact_payload)
                else:
                    CrossReviewVerdictSubmission.model_validate(artifact_payload)
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
                raise AgentWorkflowError(
                    "Cross completion artifact is not a typed current-run artifact: "
                    f"{artifact_ref}"
                ) from exc
        modules = state.get("module_submissions")
        if isinstance(modules, dict) and set(modules) == set(REPORT_MODULE_IDS):
            expected_subject_refs = [
                f"Work/runs/{run_id}/modules/"
                f"{module_id}-r{modules[module_id].revision}.json"
                for module_id in REPORT_MODULE_IDS
            ]
            if completion.subject_refs != expected_subject_refs:
                raise AgentWorkflowError(
                    "Chief CrossDecisionPack completion subjects do not match current module revisions"
                )

    @staticmethod
    def _load_business_review_completion(raw: object) -> ReviewCompletionRecord:
        """Validate review ownership/refs while ignoring digest metadata."""

        if not isinstance(raw, dict):
            raise ValueError("review completion JSON must be an object")
        return ReviewCompletionRecord.model_validate(raw)

    def _materialize_chief_cross_decision_pack(
        self, state: dict
    ) -> CrossDecisionPack:
        """Materialize the terminal Cross boundary consumed by Chief/Final.

        This reducer consumes the already completed five-module Cross review;
        it never performs a second semantic Cross pass.  The immutable review
        completion is the sole provenance boundary admitted to Chief.
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

        synthesis_inputs = [
            item
            if isinstance(item, CrossSynthesisInput)
            else CrossSynthesisInput.model_validate(item)
            for item in state.get("cross_synthesis_inputs", [])
        ]
        pack = CrossDecisionPack(
            run_id=run_id,
            module_ids=list(REPORT_MODULE_IDS),
            cross_review_completion_ref=completion_ref,
            synthesis_inputs=synthesis_inputs,
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
            existing_state = dict(state)
            existing_state["cross_decision_pack_ref"] = pack_ref
            self._validate_cross_decision_pack_business(existing_state, existing_pack)
            pack = existing_pack
        else:
            self.service.store.write_json(pack_ref, pack.model_dump(mode="json"))
        state["cross_decision_pack"] = pack
        state["cross_decision_pack_ref"] = pack_ref
        return pack

    def _current_chief_editor_input(self, state: dict) -> ChiefEditorInput:
        pack = self._materialize_chief_cross_decision_pack(state)
        pack_view_payload = pack.model_dump(mode="python")
        pack_view_payload["artifact_refs"] = [pack.cross_review_completion_ref]
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
            # Keep the completion ref as a second typed business reference.
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
                self._chief_template_skill_context(
                    state,
                    active_chapters(
                        include_chapter_four=state.get("special_topic_plan") is not None
                    ),
                ),
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
                "refs": refs,
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
        """Load the legacy monolithic Chief completion contract.

        Active orchestration now dispatches chapter lanes and does not call
        this loader.  It remains available only for compatibility/inspection
        of historical runs; its archival hash gates must not be treated as the
        active recovery policy.
        """

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
            validate_editor_quality(candidate, state["module_submissions"])
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
        """Validate a drained Cross wave before owner-scoped explicit resume.

        A failed terminal is evidence, not a command to replay the cohort.  The
        Cross lifecycle verifies and reuses every completed owner pipeline and
        dispatches only owners that have no valid promoted completion.  Invalid
        or cross-run terminal manifests still fail closed here.
        """

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
            target_modules = terminal.get("target_modules")
            statuses = terminal.get("terminal_statuses")
            if (
                terminal.get("kind") != "cross_owner_terminal_barrier"
                or target_modules != list(REPORT_MODULE_IDS)
                or not isinstance(statuses, dict)
                or set(statuses) != set(REPORT_MODULE_IDS)
                or any(
                    status not in {"completed", "failed"}
                    for status in statuses.values()
                )
                or terminal.get("status") != "failed"
            ):
                raise AgentWorkflowError(
                    "refusing to resume after an invalid Cross owner terminal "
                    f"manifest: {terminal_path}"
                )
            if terminal.get("version") == 2:
                retry_scope = terminal.get("retry_scope")
                expected_retry_scope = sorted(
                    (
                        module_id
                        for module_id, status in statuses.items()
                        if status == "failed"
                    ),
                    key=float,
                )
                if retry_scope != expected_retry_scope:
                    raise AgentWorkflowError(
                        "Cross owner terminal retry scope does not match its failed "
                        f"owners: {terminal_path}"
                    )

    def _restore_resume_state(self, state: dict, checkpoint: dict | None = None) -> None:
        """Legacy checkpoint replayer retained for offline compatibility tests.

        The active run path never calls this method: ``workflow-state.json`` is
        a status projection, while ``RecoveryStateStore`` owns stage/lane
        recovery.  Keep this legacy implementation isolated from new recovery
        decisions (including its historical content-hash checks).
        """

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
            state["cross_decision_pack_ref"] = require_run_ref(
                typed_checkpoint.cross_decision_pack_ref,
                label="CrossDecisionPack",
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
                reviewer_session_key=FINAL_REVIEW_COMPLETION_SESSION_KEYS,
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
            validate_editor_quality(edited, approved_subjects)
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
        """Restore only a complete, readable current-run delivery package."""

        run_id = state["run_id"]
        completion_ref = f"Work/runs/{run_id}/delivery-completion.json"
        completion_path = self.service.workspace / completion_ref
        if not completion_path.is_file():
            return
        try:
            payload = json.loads(completion_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if payload.get("run_id") != run_id:
            return
        status = str(payload.get("status", ""))
        if status not in {
            "delivered",
            "archive_pending",
            "archived",
            "archive_failed",
            "completed",
        }:
            return

        try:
            artifacts = [
                OutputArtifact.model_validate(item)
                for item in payload.get("output_artifacts", [])
            ]
            self.service._republish_materialized_delivery(run_id)
        except (OSError, ValueError, json.JSONDecodeError):
            # Keep the completion file as audit evidence, but do not let it
            # suppress deterministic regeneration of the delivery stage.
            return
        state["delivery_status"] = "delivered"
        if status != "delivered" or payload.get("delivery_status") != "delivered":
            payload["status"] = "delivered"
            payload["delivery_status"] = "delivered"
            payload.pop("warning", None)
            self.service.store.write_json(completion_ref, payload)
        state["output_artifacts"] = artifacts
        state["delivery_completion_ref"] = completion_ref
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
        """Legacy revision checkpoint replayer, not an active recovery path.

        Production revision orchestration uses ``RecoveryStateStore`` lane and
        aggregate records.  This helper is retained for compatibility tests
        and historical inspection only; its old checkpoint/hash behavior is
        intentionally quarantined from active runs.
        """

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
        if checkpoint_pack_ref is not None:
            state["cross_decision_pack_ref"] = str(checkpoint_pack_ref)
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
                reviewer_session_key=FINAL_REVIEW_COMPLETION_SESSION_KEYS,
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
            validate_editor_quality(edited, state["module_submissions"])
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

    async def _prepare(self, state: dict) -> Token:
        # Preparation is immutable inside one run. A resumed run must never
        # silently ingest a newer version of Inputs.
        taxonomy_token: Token | None = None
        try:
            input_snapshot = RunInputSnapshotStore(self.service.workspace).load(
                state["run_id"]
            )
            state["input_snapshot_ref"] = (
                f"Work/runs/{state['run_id']}/input-snapshot.json"
            )
            state["input_snapshot_digest"] = input_snapshot.inventory_digest
            if state.get("resume"):
                taxonomy_token = self._restore_preparation_snapshot_projection(state)
            else:
                # These are deterministic data transformations, deliberately not LLM personas.
                await self.service._build_manifest(state)
                taxonomy_token = self._prepare_report_taxonomy(state)
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
            assert taxonomy_token is not None
            return taxonomy_token
        except BaseException:
            if taxonomy_token is not None:
                reset_report_taxonomy(taxonomy_token)
            raise

    def _prepare_report_taxonomy(self, state: dict) -> Token:
        """Bind the run taxonomy parsed from its frozen S4-6 workbook."""

        manifest: ProjectManifest = state["project_manifest"]
        sources = [item for item in manifest.files if item.purpose == "s4-6"]
        if len(sources) > 1:
            raise AgentWorkflowError(
                "report taxonomy requires exactly one S4-6 workbook; "
                f"found={[item.path.as_posix() for item in sources]}"
            )
        if sources:
            source = sources[0]
            source_path = self.service.workspace / (
                source.snapshot_ref or source.path
            )
            payload = parse_report_taxonomy_workbook(
                source_path,
                source_ref=(source.snapshot_ref or source.path).as_posix(),
                source_sha256=source.sha256,
            )
        else:
            raise AgentWorkflowError(
                "report taxonomy requires the current run's S4-6 workbook snapshot"
            )
        state["report_taxonomy"] = payload
        return activate_report_taxonomy(payload)

    def _restore_preparation_snapshot_projection(self, state: dict) -> Token:
        """Load the current run's preparation records without checkpoint gates."""

        run_id = state["run_id"]
        taxonomy_ref = f"Work/runs/{run_id}/preparation/report-taxonomy.json"
        refs = self._preparation_refs(
            run_id,
            include_taxonomy=(self.service.workspace / taxonomy_ref).is_file(),
        )
        completion_ref = f"Work/runs/{run_id}/preparation/completion.json"
        missing = [ref for ref in refs.values() if not (self.service.workspace / ref).is_file()]
        if missing:
            raise AgentWorkflowError(
                f"resume requires preparation artifacts; missing={missing}"
            )
        manifest_path = self.service.workspace / refs["manifest"]
        evidence_path = self.service.workspace / refs["evidence"]
        photo_path = self.service.workspace / refs["photo_manifest"]
        adjacency_path = self.service.workspace / refs["photo_adjacency"]
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
            adjacency_path.read_text(encoding="utf-8")
        )
        state["mapping_gaps"] = json.loads(gaps_path.read_text(encoding="utf-8")).get("gaps", [])
        state["coverage_matrix"] = CoverageMatrix.model_validate_json(
            coverage_path.read_text(encoding="utf-8")
        )
        if "report_taxonomy" not in refs:
            raise AgentWorkflowError("preparation snapshot is missing report taxonomy")
        state["report_taxonomy"] = json.loads(
            (self.service.workspace / refs["report_taxonomy"]).read_text(
                encoding="utf-8"
            )
        )
        special_ref = f"Work/runs/{run_id}/preparation/special-topic-plan.json"
        if (self.service.workspace / special_ref).is_file():
            state["special_topic_plan"] = SpecialTopicPlan.model_validate_json(
                (self.service.workspace / special_ref).read_text(encoding="utf-8")
            )
        state["preparation_refs"] = refs
        state["preparation_completion_ref"] = completion_ref
        self._repair_runtime_photo_projection(state)
        return activate_report_taxonomy(state["report_taxonomy"])

    def _repair_runtime_photo_projection(self, state: dict) -> None:
        """Complete a same-run photo projection from its frozen XLSX inputs.

        This does not rewrite the immutable preparation files or evidence, so
        already-completed recovery lanes keep the exact inputs they paid for.
        It only supplies assets that an older preparation pass omitted even
        though its evidence already retained the workbook ``DISPIMG`` keys.
        """

        evidence: list[EvidenceItem] = state.get("evidence_items", [])
        photos: list[PhotoAsset] = state.get("photo_assets", [])
        referenced = {
            photo_id for item in evidence for photo_id in item.photo_refs
        }
        missing = referenced - {photo.id for photo in photos}
        if not missing:
            runtime_photo_ids(evidence, photos)
            return

        manifest: ProjectManifest = state["project_manifest"]
        manifest_by_id = {item.id: item for item in manifest.files}
        refs_by_file: dict[str, set[str]] = {}
        for item in evidence:
            unresolved = set(item.photo_refs) & missing
            if unresolved:
                refs_by_file.setdefault(item.source.file_id, set()).update(unresolved)

        recovered: list[PhotoAsset] = []
        run_id = str(state["run_id"])
        for file_id, required_ids in refs_by_file.items():
            manifest_file = manifest_by_id.get(file_id)
            if manifest_file is None:
                continue
            source_ref = manifest_file.snapshot_ref or manifest_file.path
            source_path = self.service.workspace / source_ref
            if source_path.suffix.casefold() not in {".xlsx", ".xlsm"}:
                continue
            extracted = extract_wps_images(
                source_path,
                output_dir=(
                    self.service.workspace
                    / "Work"
                    / "runs"
                    / run_id
                    / "recovery"
                    / "photo-extraction"
                    / file_id
                ),
                required_image_ids=required_ids,
            )
            for raw_id, asset in extracted.items():
                owner = next(
                    (
                        item.id
                        for item in evidence
                        if raw_id in item.photo_refs and item.submodule_id is not None
                    ),
                    None,
                )
                if owner is None:
                    continue
                safe_name = hashlib.sha256(raw_id.encode("utf-8")).hexdigest()[:16]
                final_path = (
                    self.service.workspace
                    / "Work"
                    / "runs"
                    / run_id
                    / "assets"
                    / file_id
                    / f"recovered-{safe_name}{asset.path.suffix.casefold()}"
                )
                final_path, sha256, _blob_ref = self.service.snapshot_content(
                    asset.path,
                    final_path,
                )
                recovered.append(
                    asset.model_copy(
                        update={
                            "id": raw_id,
                            "path": final_path.relative_to(self.service.workspace),
                            "sha256": sha256,
                            "source_image_id": raw_id,
                            "primary_evidence_id": owner,
                        }
                    )
                )

        state["photo_assets"] = [*photos, *recovered]
        try:
            runtime_photo_ids(
                evidence,
                state["photo_assets"],
            )
        except ValueError as exc:
            raise AgentWorkflowError(
                "same-run recovery could not reconstruct every source-table photo "
                "from the frozen XLSX inputs"
            ) from exc
        state["photo_evidence_adjacency"] = {
            "schema_version": 1,
            "photo_to_evidence": {
                photo.id: [
                    item.id for item in evidence if photo.id in item.photo_refs
                ]
                for photo in state["photo_assets"]
            },
            "evidence_to_photo": {
                item.id: list(item.photo_refs) for item in evidence
            },
            "primary_evidence": {
                photo.id: photo.primary_evidence_id
                for photo in state["photo_assets"]
            },
        }
        repair_ref = f"Work/runs/{run_id}/recovery/photo-projection.json"
        self.service.store.write_json(
            repair_ref,
            {
                "schema_version": 1,
                "run_id": run_id,
                "source": "frozen-xlsx-photo-refs",
                "recovered_assets": [
                    asset.model_dump(mode="json") for asset in recovered
                ],
            },
        )
        state["recovery_photo_projection_ref"] = repair_ref

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _preparation_refs(
        self,
        run_id: str,
        *,
        include_special_topics: bool = False,
        include_taxonomy: bool = True,
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
        if include_taxonomy:
            refs["report_taxonomy"] = f"{root}/report-taxonomy.json"
        if include_special_topics:
            refs["special_topic_plan"] = f"{root}/special-topic-plan.json"
        return refs

    def _persist_preparation_snapshot(self, state: dict) -> None:
        if "report_taxonomy" not in state:
            raise AgentWorkflowError("cannot persist preparation without report taxonomy")
        runtime_photo_ids(
            state.get("evidence_items", []),
            state.get("photo_assets", []),
        )
        refs = self._preparation_refs(
            state["run_id"],
            include_special_topics="special_topic_plan" in state,
            include_taxonomy=True,
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
            self.service.store.write_json(
                stage_ref("report_taxonomy"),
                state["report_taxonomy"],
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

    def _restore_preparation_snapshot(self, state: dict) -> Token:
        checkpoint_path = (
            self.service.workspace / f"Work/runs/{state['run_id']}/workflow-state.json"
        )
        checkpoint = FullReportCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
        )
        refs = self._preparation_refs(
            state["run_id"],
            include_special_topics="special_topic_plan" in checkpoint.preparation_refs,
            include_taxonomy="report_taxonomy" in checkpoint.preparation_refs,
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
        if "report_taxonomy" not in refs:
            raise AgentWorkflowError("preparation completion is missing report taxonomy")
        state["report_taxonomy"] = json.loads(
            (self.service.workspace / refs["report_taxonomy"]).read_text(
                encoding="utf-8"
            )
        )
        taxonomy_token = activate_report_taxonomy(state["report_taxonomy"])
        if "special_topic_plan" in refs:
            special_topic_path = self.service.workspace / refs["special_topic_plan"]
            state["special_topic_plan"] = SpecialTopicPlan.model_validate_json(
                special_topic_path.read_text(encoding="utf-8")
            )
        state["preparation_refs"] = refs
        state["preparation_sha256"] = actual_hashes
        state["preparation_completion_ref"] = completion_ref
        return taxonomy_token

    def _build_module_dispatch(
        self, state: dict, module_ids: tuple[str, ...]
    ) -> ModuleDispatchPlan:
        """Build Main's deterministic fixed-module dispatch."""

        request = state["request"]
        knowledge = KnowledgeContextBuilder(
            self.service.workspace,
            state["run_id"],
            global_root=getattr(self.service, "global_root", None),
        )
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
                    "叶子任务内联当前叶子的项目/全局 Knowledge 增量与核心写作方法；整模块 Knowledge 和完整写作 Skill 以共享引用提供，仅在增量不足时按需读取",
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
                    "module specialists/auditors, chief editor, and final auditor"
                ),
                "input": (
                    "hash-verified Work/report-template-role-skills files selected by exact "
                    "module/role identity and embedded whole in task inline_context"
                ),
                "output": (
                    "complete identity-scoped reusable Skill guidance with fact-free examples; "
                    "Cross intentionally receives no template Skill"
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
                    "structural checks never substitute for the module auditor's semantic judgment",
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
                    "renderer reports completed for the current run",
                    "delivery completion reports delivered for the current run",
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

    def _validate_module_review_completion_binding(
        self,
        *,
        run_id: str,
        module_id: str,
        subject_ref: str,
        review_ref: str,
    ) -> ReviewCompletionRecord:
        """Require a module completion to approve this exact persisted revision."""

        revision_match = re.fullmatch(
            rf"Work/runs/{re.escape(run_id)}/modules/"
            rf"{re.escape(module_id)}-r([0-9]+)\.json",
            subject_ref,
        )
        if revision_match is None:
            raise AgentWorkflowError(
                f"module lane subject ref is non-canonical: {module_id}"
            )
        expected_suffix = (
            f"/{module_id}/completion-r{revision_match.group(1)}.json"
        )
        if not review_ref.startswith(
            f"Work/runs/{run_id}/reviews/module/"
        ) or not review_ref.endswith(expected_suffix):
            raise AgentWorkflowError(
                f"module lane review completion path does not bind subject revision: {module_id}"
            )
        try:
            completion, _artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=review_ref,
                lifecycle="module",
                reviewer_agent_id="evidence-auditor",
                reviewer_session_key=f"module-auditor-{module_id}",
                subject_refs=[subject_ref],
            )
        except (OSError, ValueError) as exc:
            raise AgentWorkflowError(
                f"module lane review completion does not bind current subject: {module_id}"
            ) from exc
        return completion

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
        self._validate_module_review_completion_binding(
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=completion.subject.ref,
            review_ref=completion.review_completion.ref,
        )
        lane_state = deepcopy(state)
        lane_state.setdefault("module_submissions", {})[module_id] = submission
        lane_state.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state.setdefault("module_review_completion_refs", {})[module_id] = (
            completion.review_completion.ref
        )
        return submission, completion_ref, completion, lane_state

    def _load_recovery_module_lane(
        self,
        module_id: str,
        state: dict,
        lane_state: Any,
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict] | None:
        """Load a completed module lane from RecoveryStateStore business state.

        The recovery record points at the typed module subject.  Review
        completion is discovered by lifecycle path and is validated as a
        typed record only; no digest/CAS/trusted-handle comparison participates
        in this decision.
        """

        result_ref = getattr(lane_state, "result_ref", None)
        if not result_ref:
            return None
        path = (self.service.workspace / result_ref).resolve()
        if not path.is_relative_to(self.service.workspace) or not path.is_file():
            return None
        try:
            submission = ModuleSubmission.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if submission.module_id != module_id:
            return None
        subject_ref = path.relative_to(self.service.workspace).as_posix()
        review_ref = state.get("module_review_completion_refs", {}).get(module_id)
        if not review_ref:
            review_ref = (
                f"Work/runs/{state['run_id']}/reviews/module/initial/{module_id}/"
                f"completion-r{submission.revision}.json"
            )
            if not (self.service.workspace / review_ref).is_file():
                return None
        if not review_ref:
            return None
        try:
            review_record = self._validate_module_review_completion_binding(
                run_id=state["run_id"],
                module_id=module_id,
                subject_ref=subject_ref,
                review_ref=review_ref,
            )
        except AgentWorkflowError:
            return None
        completion = LaneCompletion(
            lane_id=f"module-{module_id}",
            run_id=state["run_id"],
            stage="module",
            module_id=module_id,
            revision=submission.revision,
            status="completed",
            result_ref=subject_ref,
            subject=subject_ref,
            review_completion=review_ref,
            author_task_attempt_id=f"recovered-module-{module_id}",
            reviewer_session_id=review_record.reviewer_session_key,
            lease_epoch=1,
        )
        lane_state_copy = deepcopy(state)
        lane_state_copy.setdefault("module_submissions", {})[module_id] = submission
        lane_state_copy.setdefault("specialist_submissions", {})[module_id] = submission
        lane_state_copy.setdefault("module_review_completion_refs", {})[module_id] = review_ref
        # The reducer still needs a typed lane completion ref.  Reconstruct
        # the deterministic business record when an older run only persisted
        # the subject ref in RecoveryStateStore.
        completion_ref = (
            f"Work/runs/{state['run_id']}/lanes/module-{module_id}/"
            f"completion-r{submission.revision}.json"
        )
        completion_path = self.service.workspace / completion_ref
        if not completion_path.is_file():
            self.service.store.write_json(
                completion_ref,
                completion.model_dump(mode="json"),
            )
        return submission, completion_ref, completion, lane_state_copy

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
        review_record = self._validate_module_review_completion_binding(
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=subject_ref,
            review_ref=review_ref,
        )
        reviewer_identity_key = review_record.reviewer_session_key
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

    def _start_module_lane_attempt(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        defer_main_exceptions: bool,
        lane_state_override: dict | None,
    ) -> _ModuleLaneAttemptContext:
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
        return _ModuleLaneAttemptContext(
            module_id=module_id,
            state=state,
            lane_state=lane_state,
            workflow_id=workflow_id,
            spec=spec,
            spec_ref=spec_ref,
            lane_attempt_id=lane_attempt_id,
            started_at_ns=started_at_ns,
            event_store=event_store,
            attempt_ref=attempt_ref,
        )

    def _fail_module_lane_attempt(
        self,
        context: _ModuleLaneAttemptContext,
        exc: BaseException,
    ) -> None:
        if isinstance(exc, DeferredMainDecision):
            # Keep the exact in-memory lane state available to the cohort
            # drain.  The state is also represented by durable progress
            # and candidate artifacts; this attribute is only a
            # same-process continuation hint.
            try:
                setattr(exc, "lane_state", context.lane_state)
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
            context.attempt_ref,
            LaneAttemptRecord(
                lane_id=context.spec.lane_id,
                task_attempt_id=context.lane_attempt_id,
                lease_epoch=1,
                started_at_ns=context.started_at_ns,
                finished_at_ns=time.time_ns(),
                status="failed",
                error=str(exc),
            ).model_dump(mode="json"),
        )
        candidate_ref = (
            f"Work/runs/{context.state['run_id']}/lanes/module-{context.module_id}/"
            f"exceptions/{context.lane_attempt_id}.json"
        )
        self.service.store.write_json(
            candidate_ref,
            LaneExceptionCandidate(
                lane_id=context.spec.lane_id,
                run_id=context.state["run_id"],
                module_id=context.module_id,
                disposition=disposition,
                reason=str(exc),
                task_attempt_id=context.lane_attempt_id,
            ).model_dump(mode="json"),
        )
        context.event_store.append(
            "TaskFailed",
            stage_id="module-work",
            task_id=context.spec.lane_id,
            task_attempt_id=context.lane_attempt_id,
            lease_epoch=1,
            correlation_id=context.workflow_id,
            artifact_refs=[self._artifact_ref(candidate_ref)],
            payload={
                "module_id": context.module_id,
                "error": str(exc),
                "exception_candidate_ref": candidate_ref,
            },
        )

    def _complete_module_lane_attempt(
        self,
        context: _ModuleLaneAttemptContext,
        submission: ModuleSubmission,
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict]:
        completion_ref, completion = self._build_lane_completion(
            context.lane_state,
            context.module_id,
            submission,
            context.spec,
        )
        self.service.store.write_json(
            context.attempt_ref,
            LaneAttemptRecord(
                lane_id=context.spec.lane_id,
                task_attempt_id=context.lane_attempt_id,
                lease_epoch=completion.lease_epoch,
                started_at_ns=context.started_at_ns,
                finished_at_ns=time.time_ns(),
                status="completed",
            ).model_dump(mode="json"),
        )
        context.event_store.append(
            "TypedResultAccepted",
            stage_id="module-work",
            task_id=context.spec.lane_id,
            task_attempt_id=context.lane_attempt_id,
            lease_epoch=completion.lease_epoch,
            artifact_refs=[self._artifact_ref(completion_ref)],
            correlation_id=context.workflow_id,
            payload={"module_id": context.module_id},
        )
        return submission, completion_ref, completion, context.lane_state

    async def _execute_module_lane(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        *,
        defer_main_exceptions: bool = True,
        lane_state_override: dict | None = None,
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict]:
        context = self._start_module_lane_attempt(
            module_id,
            state,
            workflow_id,
            defer_main_exceptions,
            lane_state_override,
        )
        try:
            submission = await self._module_pipeline(
                module_id,
                context.lane_state,
                workflow_id,
                review=True,
                checkpoint=False,
            )
            completed = self._complete_module_lane_attempt(context, submission)
        except BaseException as exc:
            self._fail_module_lane_attempt(context, exc)
            raise
        return completed

    def _finalize_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
        results: dict[
            str, tuple[ModuleSubmission, str, LaneCompletion, dict]
        ],
        failures: list[BaseException],
        failures_by_module: dict[str, BaseException],
    ) -> None:
        """Integrate module results and reduce the cohort at its barrier."""

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
                # The lane has no promotable typed completion. Preserve successful siblings
                # and record this terminal state in the cohort barrier below.
                continue
            self._record_recovery_lane(
                state,
                stage="module",
                lane_id=module_id,
                status="completed",
                result_ref=(
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module_id}-r{submission.revision}.json"
                ),
                revision=submission.revision,
            )
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
            for module_id, failure in failures_by_module.items():
                self._record_recovery_lane(
                    state,
                    stage="module",
                    lane_id=module_id,
                    status="failed",
                    error=str(failure),
                )
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
        self._record_recovery_aggregate(
            state,
            stage="module",
            lane_ids=list(requested_modules),
            result_ref=state["module_lane_barrier_ref"],
        )
        LocalEventStore(self.service.workspace, state["run_id"]).append(
            "StageCompleted",
            stage_id="module-work",
            artifact_refs=[self._artifact_ref(state["module_lane_barrier_ref"])],
            correlation_id=workflow_id,
            payload={"target_modules": list(requested_modules)},
        )
        self._checkpoint(state, "module-work", "in_progress")

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
        recovery = self._recovery_store(state)
        recovered_business = recovery.load_completed_lanes(
            self._recovery_stage_name("module"),
            list(requested_modules),
        )
        for module_id, lane_state in recovered_business.items():
            if module_id not in requested_modules:
                continue
            recovered = self._load_recovery_module_lane(module_id, state, lane_state)
            if recovered is not None:
                results[module_id] = recovered
                state.setdefault("module_submissions", {})[module_id] = recovered[0]
                state.setdefault("specialist_submissions", {})[module_id] = recovered[0]
                state.setdefault("module_review_completion_refs", {})[module_id] = (
                    recovered[3]["module_review_completion_refs"][module_id]
                )
        for module_id in requested_modules:
            if module_id in results or module_id in state.get("module_submissions", {}):
                continue
            # Legacy lane artifacts are intentionally not consulted for active
            # recovery.  They remain forensic evidence; a missing business
            # record is an explicit new lane dispatch.
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

        self._finalize_module_lanes(
            requested_modules,
            state,
            workflow_id,
            results,
            failures,
            failures_by_module,
        )

    def _prepare_module_authoring(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        *,
        review: bool,
        checkpoint: bool,
    ) -> _ModuleAuthoringPreparationContext:
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
        return _ModuleAuthoringPreparationContext(
            module_id=module_id,
            state=state,
            workflow_id=workflow_id,
            specialist_id=specialist_id,
            envelope=envelope,
            resumed_payload=resumed_payload,
            revision=revision,
            review=review,
            checkpoint=checkpoint,
        )

    async def _resume_module_authoring(
        self,
        context: _ModuleAuthoringPreparationContext,
    ) -> ModuleSubmission | None:
        if context.resumed_payload is None:
            return None
        payload = context.resumed_payload
        await self.service._notice(
            f"已恢复模块 {context.module_id} 的 specialist 提交；继续原 run 的独立模块审计。"
        )
        return payload

    def _accept_module_authoring(
        self,
        context: _ModuleAuthoringPreparationContext,
        payload: Any,
    ) -> ModuleSubmission:
        if not isinstance(payload, ModuleSubmission) or payload.module_id != context.module_id:
            raise AgentWorkflowError(
                f"{context.specialist_id} returned the wrong module payload"
            )
        payload = ModuleSubmission.model_validate(payload.model_dump(mode="python"))
        self.service.store.write_json(
            f"Work/runs/{context.state['run_id']}/modules/"
            f"{context.module_id}-r{context.revision}.json",
            payload.model_dump(mode="json"),
        )
        context.state.setdefault("specialist_submissions", {})[
            context.module_id
        ] = payload
        if context.review and context.checkpoint:
            self._checkpoint(context.state, "module-work", "in_progress")
        return payload

    async def _module_pipeline(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
        *,
        review: bool = True,
        checkpoint: bool = True,
    ) -> ModuleSubmission:
        context = self._prepare_module_authoring(
            module_id,
            state,
            workflow_id,
            review=review,
            checkpoint=checkpoint,
        )
        payload = await self._resume_module_authoring(context)
        if payload is None:
            payload = await self._agent(
                context.specialist_id,
                context.envelope,
                context.envelope.input_refs,
                workflow_id,
                session_key=f"specialist-{module_id}",
            )
            payload = self._accept_module_authoring(context, payload)

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

    async def _prepare_module_initial_review(
        self,
        module_id: str,
        payload: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
    ) -> ModuleInitialReviewPreparation:
        return await prepare_module_initial_review(
            self,
            module_id=module_id,
            payload=payload,
            state=state,
            workflow_id=workflow_id,
            initial_scope=initial_scope,
            lifecycle_id="initial",
        )

    async def _prepare_module_initial_review_step(
        self,
        module_id: str,
        payload: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
        preflight_progress: ModuleReviewPreflightProgress | None,
    ) -> ModuleInitialReviewPreparation:
        return await prepare_module_initial_review_step(
            self,
            module_id=module_id,
            payload=payload,
            state=state,
            workflow_id=workflow_id,
            initial_scope=initial_scope,
            lifecycle_id="initial",
            preflight_progress=preflight_progress,
        )

    async def _prepare_module_initial_review_preflight_revision(
        self,
        preparation: ModuleInitialReviewPreparation,
        state: dict,
    ) -> ModuleRevisionPreparation:
        return await prepare_module_revision(
            self,
            state=state,
            workflow_id=preparation.workflow_id,
            subject=preparation.current,
            module_findings=[],
            validation_ref=preparation.validation_ref,
            validation_target_submodule_ids=set(
                preparation.validation_target_submodule_ids
            ),
        )

    def _accept_module_initial_review_preflight_revision(
        self,
        preparation: ModuleInitialReviewPreparation,
        revision_preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
        state: dict,
    ) -> tuple[ModuleSubmission, str]:
        return accept_module_initial_review_preflight_revision(
            self,
            state=state,
            preparation=preparation,
            revision_preparation=revision_preparation,
            result=result,
        )

    def _accept_module_initial_review(
        self,
        preparation: ModuleInitialReviewPreparation,
        result: ModuleReviewFindingSubmission,
        state: dict,
    ) -> ModuleInitialReviewAcceptance:
        return accept_module_initial_review(
            self,
            preparation=preparation,
            result=result,
            state=state,
        )

    def _resume_module_initial_review(
        self,
        preparation: ModuleInitialReviewPreparation,
        state: dict,
    ) -> ModuleInitialReviewAcceptance:
        return resume_module_initial_review(
            preparation=preparation,
            state=state,
        )

    async def _prepare_module_revision(
        self,
        subject: ModuleSubmission,
        state: dict,
        workflow_id: str,
        module_findings: list[ModuleReviewFinding],
    ) -> ModuleRevisionPreparation:
        return await prepare_module_revision(
            self,
            state=state,
            workflow_id=workflow_id,
            subject=subject,
            module_findings=module_findings,
        )

    def _accept_module_revision(
        self,
        preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
    ) -> tuple[ModuleSubmission, str]:
        return accept_module_revision(
            self,
            preparation=preparation,
            result=result,
        )

    async def _prepare_module_recheck(
        self,
        module_id: str,
        current: ModuleSubmission,
        state: dict,
        workflow_id: str,
        *,
        initial_scope: set[str],
        lifecycle_id: str = "initial",
        preflight_progress: ModuleReviewPreflightProgress | None = None,
        author_exception_acceptance: MainExceptionDecisionAcceptance | None = None,
    ) -> ModuleRecheckPreparation:
        return await prepare_module_recheck(
            self,
            module_id=module_id,
            current=current,
            state=state,
            workflow_id=workflow_id,
            initial_scope=initial_scope,
            lifecycle_id=lifecycle_id,
            preflight_progress=preflight_progress,
            author_exception_acceptance=author_exception_acceptance,
        )

    async def _prepare_module_recheck_preflight_revision(
        self,
        preparation: ModuleRecheckPreparation,
        state: dict,
    ) -> ModuleRevisionPreparation:
        return await prepare_module_revision(
            self,
            state=state,
            workflow_id=preparation.workflow_id,
            subject=preparation.current,
            module_findings=preparation.pending,
            validation_ref=preparation.validation_ref,
            validation_target_submodule_ids=set(
                preparation.validation_target_submodule_ids
            ),
        )

    def _accept_module_recheck_preflight_revision(
        self,
        preparation: ModuleRecheckPreparation,
        revision_preparation: ModuleRevisionPreparation,
        result: ModuleRevisionSubmission,
        state: dict,
    ) -> tuple[ModuleSubmission, str]:
        return accept_module_recheck_preflight_revision(
            self,
            state=state,
            preparation=preparation,
            revision_preparation=revision_preparation,
            result=result,
        )

    async def _accept_module_recheck(
        self,
        preparation: ModuleRecheckPreparation,
        result: ModuleReviewVerdictSubmission,
        state: dict,
    ) -> ModuleRecheckAcceptance:
        return await accept_module_recheck(
            self,
            preparation=preparation,
            result=result,
            state=state,
        )

    def _resume_module_recheck(
        self,
        preparation: ModuleRecheckPreparation,
        state: dict,
    ) -> ModuleRecheckAcceptance:
        return resume_module_recheck(
            preparation=preparation,
            state=state,
        )

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        self._verify_module_lane_barrier(state)
        await run_cross_review(self, state, workflow_id)

    def _verify_module_lane_barrier(self, state: dict) -> CohortBarrier:
        """Require five completed business lanes and one module aggregate.

        The recovery store is authoritative.  The historical barrier remains
        a useful projection, but its digest/CAS fields are deliberately not
        consulted: a valid typed module subject remains reusable when an
        unrelated legacy barrier byte changes.
        """

        run_id = str(state["run_id"])
        expected_modules = set(REPORT_MODULE_IDS)
        store = self._recovery_store(state)
        completed = store.load_completed_lanes("module", list(REPORT_MODULE_IDS))
        aggregate = store.load_aggregate("module")
        if set(completed) != expected_modules:
            missing = sorted(expected_modules - set(completed), key=float)
            raise AgentWorkflowError(
                "Cross requires all five completed module lanes; "
                f"missing={missing}"
            )
        if aggregate is None or aggregate.status != "completed":
            raise AgentWorkflowError("Cross requires the completed module aggregate")
        if set(aggregate.lane_ids) != expected_modules:
            raise AgentWorkflowError("module aggregate does not cover all five modules")

        # Prefer the legacy projection when it is readable so callers that
        # inspect the returned model keep the established shape.  If it is
        # absent/corrupt, synthesize a status-only barrier from business refs.
        expected_ref = f"Work/runs/{run_id}/lanes/module-barrier.json"
        barrier: CohortBarrier | None = None
        barrier_path = self.service.workspace / expected_ref
        if barrier_path.is_file():
            try:
                candidate = CohortBarrier.model_validate_json(
                    barrier_path.read_text(encoding="utf-8")
                )
                if (
                    candidate.run_id == run_id
                    and candidate.scope == "full"
                    and candidate.status == "committed"
                    and set(candidate.target_modules) == expected_modules
                ):
                    barrier = candidate
            except (OSError, ValueError):
                barrier = None
        completion_refs = {
            module_id: str(getattr(completed[module_id], "result_ref", ""))
            for module_id in REPORT_MODULE_IDS
        }
        if barrier is None:
            barrier = CohortBarrier(
                run_id=run_id,
                target_modules=list(REPORT_MODULE_IDS),
                completion_refs=completion_refs,
                scope="full",
                status="committed",
            )
        state["module_lane_barrier_ref"] = expected_ref
        return barrier

    @staticmethod
    def _chapter_lane_ids(state: dict) -> tuple[str, ...]:
        """Return the active Chief/Final chapter lanes in deterministic order."""

        return ("1", "3", "4") if state.get("special_topic_plan") is not None else ("1", "3")

    def _chief_chapter_source_projection(
        self,
        state: dict,
        chapter_id: str,
    ) -> tuple[dict[str, str], list[str]]:
        """Build bounded, chapter-local Chief context without copying the report."""

        run_id = str(state["run_id"])
        cross_ref = state.get("cross_review_completion_ref")
        source_refs = [str(cross_ref)] if cross_ref else []
        modules = state.get("module_submissions", {})
        if chapter_id == "1":
            source_context = {
                f"module-{module_id}": json.dumps(
                    {
                        "module_id": module_id,
                        "revision": getattr(module, "revision", 0),
                        "submodule_ids": sorted(getattr(module, "submodule_narratives", {})),
                        "unresolved_questions": list(getattr(module, "unresolved_questions", [])),
                        "claim_ids": [claim.id for claim in getattr(module, "claims", [])],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                )[:2400]
                for module_id, module in sorted(modules.items(), key=lambda item: float(item[0]))
            }
            source_context["approved_markers"] = ",".join(
                f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
            )
        elif chapter_id == "3":
            source_context = {
                "cross_synthesis": json.dumps(
                    [
                        item.model_dump(mode="json")
                        if hasattr(item, "model_dump")
                        else item
                        for item in state.get("cross_synthesis_inputs", [])
                    ],
                    ensure_ascii=False,
                    sort_keys=True,
                )[:6000],
                "module_boundaries": ",".join(
                    f"{module_id}:{','.join(sorted(getattr(module, 'submodule_narratives', {})))}"
                    for module_id, module in sorted(modules.items(), key=lambda item: float(item[0]))
                ),
            }
        else:
            plan = state.get("special_topic_plan")
            source_context = {
                "special_topic_plan": json.dumps(
                    plan.model_dump(mode="json") if hasattr(plan, "model_dump") else plan,
                    ensure_ascii=False,
                    sort_keys=True,
                )[:12_000],
            }
            knowledge_ref = state.get("special_topic_knowledge_ref")
            if knowledge_ref:
                source_refs.append(str(knowledge_ref))
        # Every lane can locate the run's immutable evidence ledger, but no
        # lane receives the complete module bodies as inline context.
        evidence_ref = state.get("preparation_refs", {}).get("evidence")
        if evidence_ref:
            source_refs.append(str(evidence_ref))
        source_refs = list(dict.fromkeys(ref for ref in source_refs if ref))
        if not source_context and not source_refs:
            source_context = {"scope": f"chapter-{chapter_id}"}
        return source_context, source_refs

    def _read_chief_chapter_parts(
        self,
        state: dict,
        chapter_id: str,
        submission: ChiefChapterLaneSubmission | ChiefChapterLaneRevisionSubmission,
        task_id: str,
    ) -> dict[str, str]:
        """Materialize only the active task's lane-local result parts."""

        run_root = (self.service.workspace / f"Work/runs/{state['run_id']}").resolve()
        task_root = (
            run_root
            / "drafts"
            / task_id
            / f"r{getattr(submission, 'revision', 0)}"
        ).resolve()
        if not task_root.is_relative_to(run_root):
            raise AgentWorkflowError("Chief chapter task root escapes the current run")
        section_ids = list(submission.section_ids)
        part_to_sections: dict[str, list[str]] = {}
        if chapter_id == "4":
            part_to_sections["special_topic_analysis"] = section_ids
        else:
            for section_id in section_ids:
                part_to_sections.setdefault(CHIEF_SECTION_RESULT_PART_IDS[section_id], []).append(section_id)
        bodies: dict[str, str] = {}
        for part_id, ref in submission.part_refs.items():
            path = (self.service.workspace / ref).resolve()
            if not path.is_relative_to(task_root) or path.suffix != ".md" or not path.is_file():
                raise AgentWorkflowError(
                    f"Chief chapter {chapter_id} returned a part outside its lane task: {ref}"
                )
            content = path.read_text(encoding="utf-8")
            if not content.strip():
                raise AgentWorkflowError(f"Chief chapter {chapter_id} returned a blank part: {part_id}")
            if chapter_id == "4" and part_id == "special_topic_analysis":
                # Chapter 4 has one durable result part, but its input/output
                # lane scope is still one body per planned subsection.  Split
                # the markdown headings before handing content to the reducer;
                # never duplicate the complete chapter into every 4.x field.
                chapter_plan = state.get("special_topic_plan")
                bodies.update(
                    self._split_special_topic_analysis(
                        content,
                        chapter_plan,
                        allow_single_body=len(section_ids) == 1,
                    )
                )
            else:
                numbered_headings = numbered_markdown_headings(content)
                if numbered_headings:
                    raise AgentWorkflowError(
                        f"Chief chapter {chapter_id} part {part_id} must contain section "
                        "body only, without numbered Markdown headings: "
                        f"{list(numbered_headings)}"
                    )
                for section_id in part_to_sections.get(part_id, []):
                    bodies[section_id] = content
        if set(bodies) != set(section_ids):
            raise AgentWorkflowError(
                f"Chief chapter {chapter_id} did not return every assigned section body"
            )
        return bodies

    @staticmethod
    def _split_special_topic_analysis(
        markdown: str,
        plan: SpecialTopicPlan | None,
        *,
        allow_single_body: bool = False,
    ) -> dict[str, str]:
        """Split one Chapter 4 result part into planned subsection bodies.

        ``special_topic_analysis`` remains one typed submission part for
        compatibility, while Chief/Final lane contracts expose each planned
        4.x subsection independently.  Headings are runtime-owned boundaries;
        the reducer adds them back when assembling EditedReportSubmission.
        """

        if plan is None:
            raise AgentWorkflowError("Chapter 4 analysis requires an active special topic plan")
        text = markdown.strip()
        try:
            plan.validate_analysis(text, allow_chapter_heading=True)
        except ValueError as exc:
            raise AgentWorkflowError(str(exc)) from exc
        expected = [section.section_id for section in plan.sections]
        heading_re = re.compile(r"^\s*#{1,6}\s+(4\.\d+)\s+(.+?)\s*$")
        matches = [
            (index, match.group(1), match.group(2).strip())
            for index, line in enumerate(text.splitlines())
            if (match := heading_re.match(line))
        ]
        if not matches:
            if allow_single_body and len(expected) == 1:
                return {expected[0]: text}
            raise AgentWorkflowError(
                "Chapter 4 result must contain one heading for every planned subsection"
            )
        by_id: dict[str, str] = {}
        lines = text.splitlines()
        for position, (start, section_id, _title) in enumerate(matches):
            if section_id not in expected or section_id in by_id:
                raise AgentWorkflowError(
                    f"Chapter 4 result contains an unexpected or duplicate heading: {section_id}"
                )
            end = matches[position + 1][0] if position + 1 < len(matches) else len(lines)
            body = "\n".join(lines[start + 1 : end]).strip()
            if not body:
                raise AgentWorkflowError(f"Chapter 4 subsection {section_id} has a blank body")
            by_id[section_id] = body
        if set(by_id) != set(expected):
            raise AgentWorkflowError(
                "Chapter 4 result headings must exactly match the active special-topic plan"
            )
        return by_id

    @staticmethod
    def _render_special_topic_analysis(
        section_bodies: dict[str, str],
        plan: SpecialTopicPlan | None,
    ) -> str | None:
        if plan is None:
            return None
        expected = [section.section_id for section in plan.sections]
        if set(section_bodies) != set(expected):
            raise AgentWorkflowError("Chapter 4 reducer received an incomplete subsection set")
        return "\n\n".join(
            f"### {section.section_id} {section.title}\n{section_bodies[section.section_id].strip()}"
            for section in plan.sections
        )

    async def _chief_edit_chapter_lanes(self, state: dict, workflow_id: str) -> None:
        """Run Chief Chapter 1/3/(4) lanes concurrently, then reduce once."""

        run_id = str(state["run_id"])
        active_chapters = self._chapter_lane_ids(state)
        plan = state.get("special_topic_plan")
        chapter_sections = {
            "1": tuple(CHAPTER1_SECTION_IDS),
            "3": tuple(CHAPTER3_SECTION_IDS),
            **({"4": chapter_section_ids("4", plan)} if plan is not None else {}),
        }
        recovery = self._recovery_store(state)
        def reusable_chief_lane(payload: object, lane_state: object) -> bool:
            if not isinstance(payload, dict):
                return False
            try:
                recovered = ChiefChapterLaneSubmission.model_validate(payload)
                chapter_id = recovered.chapter_id
                if (
                    recovered.run_id != run_id
                    or chapter_id not in chapter_sections
                    or set(recovered.section_ids) != set(chapter_sections[chapter_id])
                ):
                    return False
                self._read_chief_chapter_parts(
                    state,
                    chapter_id,
                    recovered,
                    f"chief-chapter-{chapter_id}",
                )
            except (OSError, ValueError, AgentWorkflowError):
                return False
            return True

        recovered_lanes = recovery.load_completed_lanes(
            "chief",
            list(active_chapters),
            business_gate=reusable_chief_lane,
        )
        baseline_ref = str(
            state.get("cross_review_completion_ref")
            or f"Work/runs/{run_id}/reviews/cross-completion.json"
        )
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        approved_module_text = {
            module_id: self._approved_module_text(state["module_submissions"][module_id])
            for module_id in REPORT_MODULE_IDS
        }
        # A Chief aggregate has no independent baseline field.  Resume from
        # exact chapter lane + input pairs and deterministically reduce them;
        # an aggregate marker alone cannot prove which Cross subject it used.
        recovered_submissions: dict[str, tuple[ChiefChapterLaneSubmission, str]] = {}
        for chapter_id, lane_state in recovered_lanes.items():
            result_ref = getattr(lane_state, "result_ref", None)
            if not result_ref:
                continue
            try:
                recovered_payload = ChiefChapterLaneSubmission.model_validate_json(
                    (self.service.workspace / result_ref).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if (
                recovered_payload.run_id == run_id
                and recovered_payload.chapter_id == chapter_id
                and set(recovered_payload.section_ids) == set(chapter_sections[chapter_id])
                and recovered_payload.revision == 0
                and result_ref
                == f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r0.json"
            ):
                recovered_submissions[chapter_id] = (recovered_payload, result_ref)
        lane_inputs: dict[str, tuple[ChiefChapterLaneInput, str]] = {}
        for chapter_id in active_chapters:
            source_context, source_refs = self._chief_chapter_source_projection(state, chapter_id)
            contract = ChiefChapterLaneInput(
                phase="initial",
                run_id=run_id,
                subject_ref=baseline_ref,
                chapter_id=chapter_id,
                section_ids=list(chapter_sections[chapter_id]),
                section_bodies={},
                source_context=source_context,
                source_refs=source_refs,
                assigned_findings=[],
                special_topic_plan=plan,
                revision=0,
            )
            input_ref = (
                f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input.json"
            )
            existing_input_matches = False
            input_path = self.service.workspace / input_ref
            if input_path.is_file():
                try:
                    existing_contract = ChiefChapterLaneInput.model_validate_json(
                        input_path.read_text(encoding="utf-8")
                    )
                    existing_input_matches = (
                        existing_contract.model_dump(mode="json")
                        == contract.model_dump(mode="json")
                    )
                except (OSError, ValueError):
                    existing_input_matches = False
            if chapter_id in recovered_submissions and not existing_input_matches:
                recovered_submissions.pop(chapter_id)
            self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
            lane_inputs[chapter_id] = (contract, input_ref)

        async def run_lane(chapter_id: str) -> tuple[str, ChiefChapterLaneSubmission, str]:
            if chapter_id in recovered_submissions:
                submission, output_ref = recovered_submissions[chapter_id]
                return chapter_id, submission, output_ref
            contract, input_ref = lane_inputs[chapter_id]
            task_id = f"chief-chapter-{chapter_id}"
            envelope = TaskEnvelope(
                task_id=task_id,
                run_id=run_id,
                agent_id="chief-editor",
                objective=f"仅完成报告第{chapter_id}章的总编正文分段；不得输出其他章节。",
                input_refs=[input_ref],
                constraints=[
                    f"只处理 Chapter {chapter_id} 的 section_ids={','.join(contract.section_ids)}",
                    "source_context/source_refs 是本 lane 唯一事实边界；不得内联或复述其他章节正文",
                    "每个分段必须先用 write_result_part 持久化，再提交 part_refs",
                    (
                        "Chapter 1/3 的每个 part 只含对应 section body；禁止任何编号 Markdown 标题，运行时负责装配标题"
                        if chapter_id != "4"
                        else "Chapter 4 必须按计划保留全部且仅保留 ### 4.n 顶层小节；允许在匹配父节内使用 #### 4.n.m 等从属小标题"
                    ),
                    "submit_result 只提交 chief_chapter_lane_submission，不得提交完整 EditedReportSubmission",
                ],
                allowed_outputs=["chief_chapter_lane_submission"],
                allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
                revision=0,
                target_submodule_ids=[],
                input_contract_kind="chief_chapter_lane_input",
                input_contract_ref=input_ref,
                artifact_delivery_modes={input_ref: "inline"},
                inline_context=self._chief_template_skill_context(state, (chapter_id,)),
            )
            payload = await self._agent(
                "chief-editor",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"chief-chapter-{chapter_id}",
            )
            if not isinstance(payload, ChiefChapterLaneSubmission):
                raise AgentWorkflowError(
                    f"chief chapter {chapter_id} returned the wrong payload type"
                )
            if (
                payload.run_id != run_id
                or payload.chapter_id != chapter_id
                or set(payload.section_ids) != set(contract.section_ids)
                or payload.revision != 0
            ):
                raise AgentWorkflowError(f"chief chapter {chapter_id} returned an out-of-scope submission")
            output_ref = f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r0.json"
            self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
            return chapter_id, payload, output_ref

        outcomes = await asyncio.gather(
            *(run_lane(chapter_id) for chapter_id in active_chapters),
            return_exceptions=True,
        )
        successes: dict[str, tuple[ChiefChapterLaneSubmission, str]] = {}
        failures: dict[str, BaseException] = {}
        for chapter_id, outcome in zip(active_chapters, outcomes, strict=True):
            if isinstance(outcome, BaseException):
                failures[chapter_id] = outcome
                self._record_recovery_lane(
                    state,
                    stage="chief",
                    lane_id=chapter_id,
                    status="failed",
                    error=str(outcome),
                )
            else:
                _chapter_id, submission, output_ref = outcome
                successes[chapter_id] = (submission, output_ref)
                if chapter_id not in recovered_submissions:
                    self._record_recovery_lane(
                        state,
                        stage="chief",
                        lane_id=chapter_id,
                        status="completed",
                        result_ref=output_ref,
                        revision=submission.revision,
                    )
        if failures:
            raise sorted(failures.items(), key=lambda item: item[0])[0][1]
        section_bodies: dict[str, str] = {}
        lane_refs: dict[str, str] = {}
        for chapter_id in active_chapters:
            submission, output_ref = successes[chapter_id]
            section_bodies.update(
                self._read_chief_chapter_parts(
                    state,
                    chapter_id,
                    submission,
                    f"chief-chapter-{chapter_id}",
                )
            )
            lane_refs[chapter_id] = output_ref
        special_topic_body = self._render_special_topic_analysis(
            {
                section_id: section_bodies[section_id]
                for section_id in chapter_sections.get("4", ())
            },
            plan,
        )
        edited = EditedReportSubmission(
            title="配电安全专家咨询报告",
            assessment_background=section_bodies["1.1"],
            findings_overview=section_bodies["1.2"],
            regional_executive_summary=section_bodies["1.3"],
            module_narratives=approved_module_text,
            risk_panorama=section_bodies["3.1.1"],
            dimension_risk_analysis=section_bodies["3.1.2"],
            data_gap_analysis=section_bodies["3.1.3"],
            improvement_action_plan=section_bodies["3.2"],
            special_topic_plan=plan,
            special_topic_analysis=special_topic_body,
            protected_claim_ids=sorted(claim.id for claim in claims),
            tables=[],
            photo_ids=runtime_photo_ids(
                state.get("evidence_items", []), state.get("photo_assets", [])
            ),
            unresolved_editorial_issues=[],
            revision_responses=[],
        )
        candidate_ref = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        self.service.store.write_json(candidate_ref, edited.model_dump(mode="json"))
        state["edited_report"] = edited
        state["approved_module_text"] = approved_module_text
        state["chief_candidate_ref"] = candidate_ref
        state["chief_chapter_lane_refs"] = lane_refs
        state["chief_editor_session_key"] = "chief-editor"
        state["chief_editor_completion_ref"] = candidate_ref
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "chief": candidate_ref,
        }
        self._record_recovery_aggregate(
            state,
            stage="chief",
            lane_ids=list(active_chapters),
            result_ref=candidate_ref,
            revision=0,
        )

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        # Chief is a true chapter wave: each lane receives only its chapter
        # contract and the reducer assembles the full EditedReportSubmission.
        await self._chief_edit_chapter_lanes(state, workflow_id)

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

    def _final_chapter_section_bodies(
        self,
        edited: EditedReportSubmission,
        chapter_id: str,
    ) -> dict[str, str]:
        values = {
            "1.1": edited.assessment_background,
            "1.2": edited.findings_overview,
            "1.3": edited.regional_executive_summary,
            "3.1.1": edited.risk_panorama,
            "3.1.2": edited.dimension_risk_analysis,
            "3.1.3": edited.data_gap_analysis,
            "3.2": edited.improvement_action_plan,
        }
        if chapter_id == "4":
            plan = edited.special_topic_plan
            if plan is None or edited.special_topic_analysis is None:
                raise AgentWorkflowError("Final Chapter 4 lane requires an active special topic")
            return self._split_special_topic_analysis(
                edited.special_topic_analysis,
                plan,
                allow_single_body=len(plan.sections) == 1,
            )
        sections = CHAPTER1_SECTION_IDS if chapter_id == "1" else CHAPTER3_SECTION_IDS
        return {section_id: values[section_id] for section_id in sections}

    @staticmethod
    def _final_recheck_section_projection(
        current_bodies: dict[str, str],
        findings: list[ChapterScopedFinalReviewFinding],
    ) -> tuple[dict[str, str], dict[str, str]]:
        """Project the existing changed-body and unchanged-digest recheck contract."""

        changed_ids = {
            section_id
            for finding in findings
            for section_id in finding.target_section_ids
        }
        return (
            {
                section_id: body
                for section_id, body in current_bodies.items()
                if section_id in changed_ids
            },
            {
                section_id: hashlib.sha256(body.encode("utf-8")).hexdigest()
                for section_id, body in current_bodies.items()
                if section_id not in changed_ids
            },
        )

    def _restore_final_review_completion(self, state: dict) -> bool:
        """Restore the existing completed Final aggregate without Provider replay."""

        run_id = str(state["run_id"])
        recovered_aggregate = self._recovery_store(state).load_aggregate("final")
        if recovered_aggregate is None or recovered_aggregate.status != "completed":
            return False
        aggregate_ref = recovered_aggregate.result_ref
        if not aggregate_ref:
            return False
        try:
            completion = ReviewCompletionRecord.model_validate_json(
                (self.service.workspace / aggregate_ref).read_text(encoding="utf-8")
            )
            restored_ref = completion.subject_refs[0]
            if len(completion.subject_refs) != 1:
                raise ValueError("final completion must bind exactly one subject")
            self._load_current_review_completion(
                run_id=run_id,
                completion_ref=aggregate_ref,
                lifecycle="final",
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key="final-chapter-wave",
                subject_refs=[restored_ref],
            )
            restored = EditedReportSubmission.model_validate_json(
                (self.service.workspace / restored_ref).read_text(encoding="utf-8")
            )
        except (OSError, ValueError, IndexError):
            return False
        state["edited_report"] = restored
        state["chief_candidate_ref"] = restored_ref
        state["final_review_completion_ref"] = aggregate_ref
        state["final_audit_snapshot_ref"] = (
            f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        )
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "final": aggregate_ref,
        }
        self._validated_final_audit_subject(state)
        return True

    async def _run_final_chapter_lanes(
        self,
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run Final initial/recheck chapter lanes with one final reducer."""

        run_id = str(state["run_id"])
        active_chapters = self._chapter_lane_ids(state)
        plan = state.get("special_topic_plan")
        chapter_sections = {
            "1": tuple(CHAPTER1_SECTION_IDS),
            "3": tuple(CHAPTER3_SECTION_IDS),
            **({"4": chapter_section_ids("4", plan)} if plan is not None else {}),
        }
        current = state["edited_report"]
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in getattr(
                state.get("module_submissions", {}).get(module_id),
                "claims",
                [],
            )
        ]
        subject_ref = str(
            state.get("chief_candidate_ref")
            or f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        )
        recovery = self._recovery_store(state)
        if self._restore_final_review_completion(state):
            return
        # Initial findings and later verdicts are separate recovery stages.
        # Never let a terminal verdict overwrite an initial finding lane.
        recovered_lanes = recovery.load_completed_lanes(
            "final-initial", list(active_chapters)
        )
        recovered_initial: dict[str, tuple[FinalChapterLaneFindingSubmission, str]] = {}
        for chapter_id, lane_state in recovered_lanes.items():
            result_ref = getattr(lane_state, "result_ref", None)
            if not result_ref:
                continue
            try:
                recovered_payload = FinalChapterLaneFindingSubmission.model_validate_json(
                    (self.service.workspace / result_ref).read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            if (
                recovered_payload.run_id == run_id
                and recovered_payload.chapter_id == chapter_id
                and set(recovered_payload.checked_section_ids) == set(chapter_sections[chapter_id])
                and result_ref
                == f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
            ):
                recovered_initial[chapter_id] = (recovered_payload, result_ref)
        initial_inputs: dict[str, tuple[FinalChapterLaneInput, str]] = {}
        for chapter_id in active_chapters:
            contract = FinalChapterLaneInput(
                phase="initial",
                run_id=run_id,
                subject_ref=subject_ref,
                chapter_id=chapter_id,
                review_focus=list(final_lane_specialization(chapter_id).review_focus),
                section_ids=list(chapter_sections[chapter_id]),
                section_bodies=self._final_chapter_section_bodies(current, chapter_id),
                required_findings=[],
                revision_responses=[],
                special_topic_plan=plan,
                revision=0,
            )
            input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r0.json"
            existing_input_matches = False
            input_path = self.service.workspace / input_ref
            if input_path.is_file():
                try:
                    existing_contract = FinalChapterLaneInput.model_validate_json(
                        input_path.read_text(encoding="utf-8")
                    )
                    existing_input_matches = (
                        existing_contract.model_dump(mode="json")
                        == contract.model_dump(mode="json")
                    )
                except (OSError, ValueError):
                    existing_input_matches = False
            if chapter_id in recovered_initial and not existing_input_matches:
                recovered_initial.pop(chapter_id)
            self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
            initial_inputs[chapter_id] = (contract, input_ref)

        async def dispatch_final_initial(chapter_id: str):
            if chapter_id in recovered_initial:
                payload, output_ref = recovered_initial[chapter_id]
                return chapter_id, payload, output_ref
            contract, input_ref = initial_inputs[chapter_id]
            task_id = f"final-chapter-{chapter_id}-r0"
            envelope = TaskEnvelope(
                task_id=task_id,
                run_id=run_id,
                agent_id="chief-editor-auditor",
                objective=f"只审查报告第{chapter_id}章指定小节并提交 lane-local findings。",
                input_refs=[input_ref],
                constraints=[
                    f"只覆盖 Chapter {chapter_id} section_ids={','.join(contract.section_ids)}",
                    "不得复制其他章节正文、全局 EditedReport 或跨章节 finding",
                    "提交 final_chapter_lane_finding_submission，findings target_section_ids 必须留在本 lane",
                ],
                allowed_outputs=["final_chapter_lane_finding_submission"],
                allowed_tools=["submit_result"],
                revision=0,
                input_contract_kind="final_chapter_lane_input",
                input_contract_ref=input_ref,
                artifact_delivery_modes={input_ref: "inline"},
                inline_context=self._final_template_skill_context(state, chapter_id),
            )
            payload = await self._agent(
                "chief-editor-auditor",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"final-chapter-{chapter_id}",
            )
            if not isinstance(payload, FinalChapterLaneFindingSubmission):
                raise AgentWorkflowError(f"final chapter {chapter_id} returned the wrong finding type")
            if (
                payload.run_id != run_id
                or payload.chapter_id != chapter_id
                or set(payload.checked_section_ids) != set(contract.section_ids)
            ):
                raise AgentWorkflowError(f"final chapter {chapter_id} returned an out-of-scope finding lane")
            output_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r0.json"
            self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
            return chapter_id, payload, output_ref

        initial_results = await asyncio.gather(
            *(dispatch_final_initial(chapter_id) for chapter_id in active_chapters),
            return_exceptions=True,
        )
        initial_successes: dict[str, tuple[FinalChapterLaneFindingSubmission, str]] = {}
        initial_failures: dict[str, BaseException] = {}
        for chapter_id, outcome in zip(active_chapters, initial_results, strict=True):
            if isinstance(outcome, BaseException):
                initial_failures[chapter_id] = outcome
                self._record_recovery_lane(
                    state,
                    stage="final-initial",
                    lane_id=chapter_id,
                    status="failed",
                    error=str(outcome),
                )
            else:
                _chapter_id, payload, output_ref = outcome
                initial_successes[chapter_id] = (payload, output_ref)
                if chapter_id not in recovered_initial:
                    self._record_recovery_lane(
                        state,
                        stage="final-initial",
                        lane_id=chapter_id,
                        status="completed",
                        result_ref=output_ref,
                        revision=0,
                    )
        if initial_failures:
            raise sorted(initial_failures.items(), key=lambda item: item[0])[0][1]

        initial_projection_ref = (
            f"Work/runs/{run_id}/reviews/final-initial-aggregate.json"
        )
        self.service.store.write_json(
            initial_projection_ref,
            {
                "run_id": run_id,
                "stage": "final-initial",
                "status": "completed",
                "lane_ids": list(active_chapters),
                "result_refs": {
                    chapter_id: output_ref
                    for chapter_id, (_payload, output_ref) in initial_successes.items()
                },
            },
        )
        self._record_recovery_aggregate(
            state,
            stage="final-initial",
            lane_ids=list(active_chapters),
            result_ref=initial_projection_ref,
            revision=0,
        )

        findings_by_chapter = {
            chapter_id: list(payload.findings)
            for chapter_id, (payload, _ref) in initial_successes.items()
        }
        revision_responses: dict[str, list[RevisionResponse]] = {
            chapter_id: [] for chapter_id in active_chapters
        }
        verdict_payloads: dict[str, tuple[FinalChapterLaneVerdictSubmission, str]] = {}
        verdict_history: list[tuple[FinalChapterLaneVerdictSubmission, str]] = []
        pending_by_chapter: dict[str, list] = {}
        revision_number = 0
        if any(findings_by_chapter.values()):
            revision_number = 1
            affected_chapters = tuple(
                chapter_id for chapter_id in active_chapters if findings_by_chapter.get(chapter_id)
            )
            recovered_chief_revision: dict[str, tuple[ChiefChapterLaneRevisionSubmission, str]] = {}
            recovered_chief_lanes = recovery.load_completed_lanes(
                f"chief-revision-r{revision_number}", list(affected_chapters)
            )
            # Lane outputs, not the aggregate marker, are the authority for a
            # resumed chapter revision.  Re-reduce the exact bound lane parts
            # below so a stale aggregate cannot replace the active base subject.
            for chapter_id, lane_state in recovered_chief_lanes.items():
                result_ref = getattr(lane_state, "result_ref", None)
                if not result_ref:
                    continue
                try:
                    recovered_payload = ChiefChapterLaneRevisionSubmission.model_validate_json(
                        (self.service.workspace / result_ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if (
                    recovered_payload.run_id == run_id
                    and recovered_payload.base_subject_ref == subject_ref
                    and recovered_payload.chapter_id == chapter_id
                    and recovered_payload.revision == revision_number
                    and {
                        response.finding_id
                        for response in recovered_payload.revision_responses
                    }
                    == {finding.id for finding in findings_by_chapter[chapter_id]}
                    and result_ref
                    == (
                        f"Work/runs/{run_id}/reviews/chief-chapter-lane-"
                        f"{chapter_id}-r{revision_number}.json"
                    )
                ):
                    recovered_chief_revision[chapter_id] = (recovered_payload, result_ref)
            recovered_recheck: dict[str, tuple[FinalChapterLaneVerdictSubmission, str]] = {}
            recovered_recheck_lanes = recovery.load_completed_lanes(
                f"final-recheck-r{revision_number}", list(affected_chapters)
            )
            for chapter_id, lane_state in recovered_recheck_lanes.items():
                result_ref = getattr(lane_state, "result_ref", None)
                if not result_ref:
                    continue
                try:
                    recovered_payload = FinalChapterLaneVerdictSubmission.model_validate_json(
                        (self.service.workspace / result_ref).read_text(encoding="utf-8")
                    )
                except (OSError, ValueError):
                    continue
                if (
                    recovered_payload.run_id == run_id
                    and recovered_payload.chapter_id == chapter_id
                    and set(recovered_payload.checked_section_ids) == set(chapter_sections[chapter_id])
                    and {
                        verdict.finding_id for verdict in recovered_payload.verdicts
                    }
                    == {finding.id for finding in findings_by_chapter[chapter_id]}
                    and result_ref
                    == (
                        f"Work/runs/{run_id}/reviews/final-chapter-lane-"
                        f"{chapter_id}-r{revision_number}.json"
                    )
                ):
                    recovered_recheck[chapter_id] = (recovered_payload, result_ref)
            revised_parts: dict[str, dict[str, str]] = {}
            chief_revision_results: dict[str, tuple[ChiefChapterLaneRevisionSubmission, str]] = {}

            async def dispatch_chief_revision(chapter_id: str):
                findings = findings_by_chapter[chapter_id]
                if not findings:
                    return chapter_id, None, None
                if chapter_id in recovered_chief_revision:
                    recovered_payload, recovered_ref = recovered_chief_revision[chapter_id]
                    parts = self._read_chief_chapter_parts(
                        state,
                        chapter_id,
                        recovered_payload,
                        f"chief-chapter-{chapter_id}-r{revision_number}",
                    )
                    return chapter_id, recovered_payload, recovered_ref, parts
                section_bodies = self._final_chapter_section_bodies(current, chapter_id)
                source_context, source_refs = self._chief_chapter_source_projection(state, chapter_id)
                contract = ChiefChapterLaneInput(
                    phase="revision",
                    run_id=run_id,
                    subject_ref=subject_ref,
                    chapter_id=chapter_id,
                    section_ids=list(chapter_sections[chapter_id]),
                    section_bodies=section_bodies,
                    source_context=source_context,
                    source_refs=source_refs,
                    assigned_findings=findings,
                    special_topic_plan=plan,
                    revision=revision_number,
                )
                input_ref = f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input-r{revision_number}.json"
                self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
                task_id = f"chief-chapter-{chapter_id}-r{revision_number}"
                envelope = TaskEnvelope(
                    task_id=task_id,
                    run_id=run_id,
                    agent_id="chief-editor",
                    objective=f"只修订 Chapter {chapter_id} 被 Final 指定的 finding 小节。",
                    input_refs=[input_ref],
                    constraints=[
                        "只提交 chief_chapter_lane_revision_submission，禁止提交完整报告",
                        "part_refs 只能覆盖本章；revision_responses 必须对应本章 findings",
                        (
                            "Chapter 1/3 的每个 part 只含对应 section body；禁止任何编号 Markdown 标题，运行时负责装配标题"
                            if chapter_id != "4"
                            else "Chapter 4 必须按计划保留全部且仅保留 ### 4.n 顶层小节；允许在匹配父节内使用 #### 4.n.m 等从属小标题"
                        ),
                    ],
                    allowed_outputs=["chief_chapter_lane_revision_submission"],
                    allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
                    revision=revision_number,
                    prior_result_ref=subject_ref,
                    input_contract_kind="chief_chapter_lane_input",
                    input_contract_ref=input_ref,
                    artifact_delivery_modes={input_ref: "inline"},
                    inline_context=self._chief_template_skill_context(
                        state, (chapter_id,)
                    ),
                )
                payload = await self._agent(
                    "chief-editor",
                    envelope,
                    envelope.input_refs,
                    workflow_id,
                    session_key=f"chief-chapter-{chapter_id}",
                )
                if not isinstance(payload, ChiefChapterLaneRevisionSubmission):
                    raise AgentWorkflowError(f"chief chapter {chapter_id} returned the wrong revision type")
                if (
                    payload.run_id != run_id
                    or payload.base_subject_ref != subject_ref
                    or payload.chapter_id != chapter_id
                    or payload.revision != revision_number
                    or {
                        response.finding_id
                        for response in payload.revision_responses
                    }
                    != {finding.id for finding in findings}
                ):
                    raise AgentWorkflowError(f"chief chapter {chapter_id} revision identity mismatch")
                parts = self._read_chief_chapter_parts(
                    state,
                    chapter_id,
                    payload,
                    task_id,
                )
                output_ref = f"Work/runs/{run_id}/reviews/chief-chapter-lane-{chapter_id}-r{revision_number}.json"
                self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
                return chapter_id, payload, output_ref, parts

            chief_results = await asyncio.gather(
                *(dispatch_chief_revision(chapter_id) for chapter_id in active_chapters),
                return_exceptions=True,
            )
            chief_failures: dict[str, BaseException] = {}
            for chapter_id, outcome in zip(active_chapters, chief_results, strict=True):
                if isinstance(outcome, BaseException):
                    chief_failures[chapter_id] = outcome
                    continue
                if outcome[1] is None:
                    continue
                _chapter_id, payload, output_ref, parts = outcome
                chief_revision_results[chapter_id] = (payload, output_ref)
                revised_parts[chapter_id] = parts
                revision_responses[chapter_id] = list(payload.revision_responses)
                if chapter_id not in recovered_chief_revision:
                    self._record_recovery_lane(
                        state,
                        stage=f"chief-revision-r{revision_number}",
                        lane_id=chapter_id,
                        status="completed",
                        result_ref=output_ref,
                        revision=revision_number,
                    )
            if chief_failures:
                raise sorted(chief_failures.items(), key=lambda item: item[0])[0][1]

            updates: dict[str, str] = {}
            for chapter_id, parts in revised_parts.items():
                updates.update(parts)
            if updates:
                field_for_section = {
                    "1.1": "assessment_background",
                    "1.2": "findings_overview",
                    "1.3": "regional_executive_summary",
                    "3.1.1": "risk_panorama",
                    "3.1.2": "dimension_risk_analysis",
                    "3.1.3": "data_gap_analysis",
                    "3.2": "improvement_action_plan",
                }
                current = current.model_copy(
                    update={
                        **{
                            field_for_section[section_id]: body
                            for section_id, body in updates.items()
                            if section_id in field_for_section
                        },
                        **(
                            {
                                "special_topic_analysis": self._render_special_topic_analysis(
                                    revised_parts["4"],
                                    plan,
                                )
                            }
                            if "4" in revised_parts
                            else {}
                        ),
                    }
                )
                subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r{revision_number}.json"
                self.service.store.write_json(subject_ref, current.model_dump(mode="json"))
                state["edited_report"] = current
                state["chief_candidate_ref"] = subject_ref
                self._record_recovery_aggregate(
                    state,
                    stage=f"chief-revision-r{revision_number}",
                    lane_ids=[
                        chapter_id
                        for chapter_id in active_chapters
                        if findings_by_chapter.get(chapter_id)
                    ],
                    result_ref=subject_ref,
                    revision=revision_number,
                )

            async def dispatch_final_recheck(chapter_id: str):
                findings = findings_by_chapter[chapter_id]
                if not findings:
                    return chapter_id, None, None
                current_bodies = self._final_chapter_section_bodies(current, chapter_id)
                changed_bodies, unchanged_digests = (
                    self._final_recheck_section_projection(current_bodies, findings)
                )
                contract = FinalChapterLaneInput(
                    phase="recheck",
                    run_id=run_id,
                    subject_ref=subject_ref,
                    chapter_id=chapter_id,
                    review_focus=list(final_lane_specialization(chapter_id).review_focus),
                    section_ids=list(chapter_sections[chapter_id]),
                    section_bodies=changed_bodies,
                    unchanged_section_sha256=unchanged_digests,
                    required_findings=findings,
                    revision_responses=revision_responses[chapter_id],
                    special_topic_plan=plan,
                    revision=revision_number,
                )
                input_ref = f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-r{revision_number}.json"
                if chapter_id in recovered_recheck:
                    try:
                        persisted_contract = FinalChapterLaneInput.model_validate_json(
                            (self.service.workspace / input_ref).read_text(encoding="utf-8")
                        )
                    except (OSError, ValueError):
                        persisted_contract = None
                    if (
                        persisted_contract is not None
                        and persisted_contract.model_dump(mode="json")
                        == contract.model_dump(mode="json")
                    ):
                        recovered_payload, recovered_ref = recovered_recheck[chapter_id]
                        return chapter_id, recovered_payload, recovered_ref
                self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
                task_id = f"final-chapter-{chapter_id}-r{revision_number}"
                envelope = TaskEnvelope(
                    task_id=task_id,
                    run_id=run_id,
                    agent_id="chief-editor-auditor",
                    objective=f"只复核 Chapter {chapter_id} 的 assigned findings 并提交 verdicts。",
                    input_refs=[input_ref],
                    constraints=[
                        "只提交 final_chapter_lane_verdict_submission",
                        "verdicts 必须覆盖该章全部 required_findings，new_findings 只能留在该章",
                    ],
                    allowed_outputs=["final_chapter_lane_verdict_submission"],
                    allowed_tools=["submit_result"],
                    revision=revision_number,
                    prior_result_ref=subject_ref,
                    input_contract_kind="final_chapter_lane_input",
                    input_contract_ref=input_ref,
                    artifact_delivery_modes={input_ref: "inline"},
                    inline_context=self._final_template_skill_context(state, chapter_id),
                )
                payload = await self._agent(
                    "chief-editor-auditor",
                    envelope,
                    envelope.input_refs,
                    workflow_id,
                    session_key=f"final-chapter-{chapter_id}",
                )
                if not isinstance(payload, FinalChapterLaneVerdictSubmission):
                    raise AgentWorkflowError(f"final chapter {chapter_id} returned the wrong verdict type")
                expected_ids = {finding.id for finding in findings}
                actual_ids = {verdict.finding_id for verdict in payload.verdicts}
                if (
                    payload.run_id != run_id
                    or payload.chapter_id != chapter_id
                    or set(payload.checked_section_ids) != set(contract.section_ids)
                    or actual_ids != expected_ids
                ):
                    raise AgentWorkflowError(f"final chapter {chapter_id} verdict does not close its lane findings")
                output_ref = f"Work/runs/{run_id}/reviews/final-chapter-lane-{chapter_id}-r{revision_number}.json"
                self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
                return chapter_id, payload, output_ref

            verdict_results = await asyncio.gather(
                *(dispatch_final_recheck(chapter_id) for chapter_id in active_chapters),
                return_exceptions=True,
            )
            verdict_failures: dict[str, BaseException] = {}
            for chapter_id, outcome in zip(active_chapters, verdict_results, strict=True):
                if isinstance(outcome, BaseException):
                    verdict_failures[chapter_id] = outcome
                    continue
                if outcome[1] is not None:
                    _chapter_id, payload, output_ref = outcome
                    verdict_payloads[chapter_id] = (payload, output_ref)
                    verdict_history.append((payload, output_ref))
                    if chapter_id not in recovered_recheck:
                        self._record_recovery_lane(
                            state,
                            stage=f"final-recheck-r{revision_number}",
                            lane_id=chapter_id,
                            status="completed",
                            result_ref=output_ref,
                            revision=revision_number,
                        )
            if verdict_failures:
                raise sorted(verdict_failures.items(), key=lambda item: item[0])[0][1]

            # Keep open/escalated findings and genuine lane-local regressions
            # for the next bounded revision wave.  A resolved verdict is the
            # only terminal finding disposition.
            for chapter_id, findings in findings_by_chapter.items():
                payload = verdict_payloads.get(chapter_id, (None, None))[0]
                if payload is None:
                    continue
                verdict_by_id = {verdict.finding_id: verdict for verdict in payload.verdicts}
                pending = [
                    finding
                    for finding in findings
                    if verdict_by_id[finding.id].verdict != "resolved"
                ]
                pending.extend(payload.new_findings)
                if pending:
                    pending_by_chapter[chapter_id] = pending
            recheck_projection_ref = (
                f"Work/runs/{run_id}/reviews/final-recheck-r{revision_number}-aggregate.json"
            )
            self.service.store.write_json(
                recheck_projection_ref,
                {
                    "run_id": run_id,
                    "stage": f"final-recheck-r{revision_number}",
                    "status": "completed",
                    "lane_ids": list(affected_chapters),
                    "result_ref": subject_ref,
                },
            )
            self._record_recovery_aggregate(
                state,
                stage=f"final-recheck-r{revision_number}",
                lane_ids=list(affected_chapters),
                result_ref=recheck_projection_ref,
                revision=revision_number,
            )

        # Genuine regressions and open/escalated findings trigger another
        # affected-only parallel wave.  The bound prevents an endless provider
        # loop; callers can route a repeated failure through the existing Main
        # exception path instead of replaying arbitrary provider turns.
        max_rounds = max(1, int(state.get("max_final_review_rounds", 3)))
        while pending_by_chapter:
            if revision_number >= max_rounds:
                raise AgentWorkflowError(
                    "final chapter review exceeded the maximum revision rounds"
                )
            revision_number += 1
            (
                current,
                subject_ref,
                pending_by_chapter,
                followup_verdicts,
            ) = await self._run_final_followup_round(
                state,
                workflow_id,
                current=current,
                subject_ref=subject_ref,
                pending_by_chapter=pending_by_chapter,
                revision_number=revision_number,
                chapter_sections=chapter_sections,
                active_chapters=active_chapters,
                plan=plan,
            )
            verdict_payloads.update(followup_verdicts)
            verdict_history.extend(followup_verdicts.values())

        state["edited_report"] = current
        state["final_review_restart_round"] = revision_number or 1
        completion = ReviewCompletionRecord(
            lifecycle="final",
            run_id=run_id,
            reviewer_agent_id="chief-editor-auditor",
            reviewer_session_key="final-chapter-wave",
            subject_refs=[subject_ref],
            finding_refs=[ref for _payload, ref in initial_successes.values()],
            verdict_refs=[ref for _payload, ref in verdict_history],
            resolved_finding_ids=sorted(
                {
                    finding.id
                    for findings in findings_by_chapter.values()
                    for finding in findings
                }
                | {
                    finding.id
                    for payload, _ref in verdict_history
                    for finding in payload.new_findings
                }
            ),
        )
        completion_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
        self.service.store.write_json(completion_ref, completion.model_dump(mode="json"))
        # Keep a typed business snapshot for delivery and inspection.
        canonical_ref = f"Work/runs/{run_id}/validation/report-chief-candidate-r{revision_number}.md"
        try:
            _, canonical = self._delivery_projection(state, current, claims)
            self._validate_final_report_structure(
                state,
                canonical,
                f"chief-candidate-r{revision_number}",
            )
        except AgentWorkflowError:
            raise
        validation_ref = f"Work/runs/{run_id}/reviews/report-integrity-chief-candidate-r{revision_number}.json"
        snapshot_ref = f"Work/runs/{run_id}/reviews/final-audit-snapshot.json"
        snapshot = FinalAuditSnapshot(
            run_id=run_id,
            subject_ref=subject_ref,
            subject_revision=revision_number,
            canonical_markdown_ref=canonical_ref,
            validation_report_ref=validation_ref,
            completion_ref=completion_ref,
        )
        self.service.store.write_json(snapshot_ref, snapshot.model_dump(mode="json"))
        state["final_review_completion_ref"] = completion_ref
        state["final_audit_snapshot_ref"] = snapshot_ref
        state["final_residual_risks"] = [
            risk
            for payload, _ref in initial_successes.values()
            for risk in payload.residual_risks
        ]
        state["final_chapter_lane_refs"] = {
            chapter_id: ref for chapter_id, (_payload, ref) in initial_successes.items()
        }
        state["aggregate_refs"] = {
            **dict(state.get("aggregate_refs", {})),
            "final": completion_ref,
        }
        # Final aggregate consumes one terminal lane projection per chapter;
        # phase-specific initial/recheck records above remain immutable history.
        for chapter_id in active_chapters:
            terminal_ref = (
                verdict_payloads.get(chapter_id, (None, None))[1]
                or initial_successes.get(chapter_id, (None, None))[1]
            )
            if terminal_ref:
                self._record_recovery_lane(
                    state,
                    stage="final",
                    lane_id=chapter_id,
                    status="completed",
                    result_ref=terminal_ref,
                    revision=revision_number,
                )
        self._record_recovery_aggregate(
            state,
            stage="final",
            lane_ids=list(active_chapters),
            result_ref=completion_ref,
            revision=revision_number,
        )

    async def _run_final_followup_round(
        self,
        state: dict,
        workflow_id: str,
        *,
        current: EditedReportSubmission,
        subject_ref: str,
        pending_by_chapter: dict[str, list],
        revision_number: int,
        chapter_sections: dict[str, tuple[str, ...]],
        active_chapters: tuple[str, ...],
        plan: SpecialTopicPlan | None,
    ) -> tuple[
        EditedReportSubmission,
        str,
        dict[str, list],
        dict[str, tuple[FinalChapterLaneVerdictSubmission, str]],
    ]:
        """Run one affected-only Chief revision + Final recheck wave.

        This helper is deliberately phase-specific in RecoveryStateStore: a
        retry or crash in round N never makes the initial finding lane appear
        complete, and unaffected chapters are not dispatched again.
        """

        run_id = str(state["run_id"])
        affected = tuple(chapter_id for chapter_id in active_chapters if pending_by_chapter.get(chapter_id))
        revised_parts: dict[str, dict[str, str]] = {}
        revision_responses: dict[str, list[RevisionResponse]] = {
            chapter_id: [] for chapter_id in affected
        }

        async def dispatch_chief(chapter_id: str):
            findings = pending_by_chapter[chapter_id]
            section_bodies = self._final_chapter_section_bodies(current, chapter_id)
            source_context, source_refs = self._chief_chapter_source_projection(state, chapter_id)
            contract = ChiefChapterLaneInput(
                phase="revision",
                run_id=run_id,
                subject_ref=subject_ref,
                chapter_id=chapter_id,
                section_ids=list(chapter_sections[chapter_id]),
                section_bodies=section_bodies,
                source_context=source_context,
                source_refs=source_refs,
                assigned_findings=findings,
                special_topic_plan=plan,
                revision=revision_number,
            )
            input_ref = (
                f"Work/runs/{run_id}/context/chief-chapter-{chapter_id}-input-"
                f"r{revision_number}.json"
            )
            self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
            task_id = f"chief-chapter-{chapter_id}-r{revision_number}"
            envelope = TaskEnvelope(
                task_id=task_id,
                run_id=run_id,
                agent_id="chief-editor",
                objective=f"只修订 Chapter {chapter_id} 被 Final 指定的 finding 小节。",
                input_refs=[input_ref],
                constraints=[
                    "只提交 chief_chapter_lane_revision_submission，禁止提交完整报告",
                    "part_refs 只能覆盖本章；revision_responses 必须对应本章 findings",
                    (
                        "Chapter 1/3 的每个 part 只含对应 section body；禁止任何编号 Markdown 标题，运行时负责装配标题"
                        if chapter_id != "4"
                        else "Chapter 4 必须按计划保留全部且仅保留 ### 4.n 顶层小节；允许在匹配父节内使用 #### 4.n.m 等从属小标题"
                    ),
                ],
                allowed_outputs=["chief_chapter_lane_revision_submission"],
                allowed_tools=["write_result_part", "list_result_parts", "submit_result"],
                revision=revision_number,
                prior_result_ref=subject_ref,
                input_contract_kind="chief_chapter_lane_input",
                input_contract_ref=input_ref,
                artifact_delivery_modes={input_ref: "inline"},
                inline_context=self._chief_template_skill_context(
                    state, (chapter_id,)
                ),
            )
            payload = await self._agent(
                "chief-editor",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"chief-chapter-{chapter_id}",
            )
            if not isinstance(payload, ChiefChapterLaneRevisionSubmission):
                raise AgentWorkflowError(f"chief chapter {chapter_id} returned the wrong revision type")
            if (
                payload.run_id != run_id
                or payload.base_subject_ref != subject_ref
                or payload.chapter_id != chapter_id
                or payload.revision != revision_number
            ):
                raise AgentWorkflowError(f"chief chapter {chapter_id} revision identity mismatch")
            parts = self._read_chief_chapter_parts(state, chapter_id, payload, task_id)
            output_ref = (
                f"Work/runs/{run_id}/reviews/chief-chapter-lane-"
                f"{chapter_id}-r{revision_number}.json"
            )
            self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
            return chapter_id, payload, output_ref, parts

        chief_results = await asyncio.gather(
            *(dispatch_chief(chapter_id) for chapter_id in affected),
            return_exceptions=True,
        )
        chief_failures = [result for result in chief_results if isinstance(result, BaseException)]
        if chief_failures:
            raise sorted(chief_failures, key=str)[0]
        for chapter_id, payload, output_ref, parts in chief_results:
            revised_parts[chapter_id] = parts
            revision_responses[chapter_id] = list(payload.revision_responses)
            self._record_recovery_lane(
                state,
                stage=f"chief-revision-r{revision_number}",
                lane_id=chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=revision_number,
            )

        updates = {
            section_id: body
            for chapter_parts in revised_parts.values()
            for section_id, body in chapter_parts.items()
        }
        field_for_section = {
            "1.1": "assessment_background",
            "1.2": "findings_overview",
            "1.3": "regional_executive_summary",
            "3.1.1": "risk_panorama",
            "3.1.2": "dimension_risk_analysis",
            "3.1.3": "data_gap_analysis",
            "3.2": "improvement_action_plan",
        }
        current = current.model_copy(
            update={
                **{
                    field_for_section[section_id]: body
                    for section_id, body in updates.items()
                    if section_id in field_for_section
                },
                **(
                    {"special_topic_analysis": self._render_special_topic_analysis(revised_parts["4"], plan)}
                    if "4" in revised_parts
                    else {}
                ),
            }
        )
        subject_ref = f"Work/runs/{run_id}/edited-revisions/chief-r{revision_number}.json"
        self.service.store.write_json(subject_ref, current.model_dump(mode="json"))
        state["edited_report"] = current
        state["chief_candidate_ref"] = subject_ref
        chief_aggregate_ref = (
            f"Work/runs/{run_id}/reviews/chief-revision-r{revision_number}-aggregate.json"
        )
        self.service.store.write_json(
            chief_aggregate_ref,
            {
                "run_id": run_id,
                "stage": f"chief-revision-r{revision_number}",
                "status": "completed",
                "lane_ids": list(affected),
                "result_ref": subject_ref,
            },
        )
        self._record_recovery_aggregate(
            state,
            stage=f"chief-revision-r{revision_number}",
            lane_ids=list(affected),
            result_ref=subject_ref,
            revision=revision_number,
        )

        async def dispatch_recheck(chapter_id: str):
            findings = pending_by_chapter[chapter_id]
            current_bodies = self._final_chapter_section_bodies(current, chapter_id)
            changed_bodies, unchanged_digests = self._final_recheck_section_projection(
                current_bodies,
                findings,
            )
            contract = FinalChapterLaneInput(
                phase="recheck",
                run_id=run_id,
                subject_ref=subject_ref,
                chapter_id=chapter_id,
                review_focus=list(final_lane_specialization(chapter_id).review_focus),
                section_ids=list(chapter_sections[chapter_id]),
                section_bodies=changed_bodies,
                unchanged_section_sha256=unchanged_digests,
                required_findings=findings,
                revision_responses=revision_responses[chapter_id],
                special_topic_plan=plan,
                revision=revision_number,
            )
            input_ref = (
                f"Work/runs/{run_id}/context/final-chapter-{chapter_id}-input-"
                f"r{revision_number}.json"
            )
            self.service.store.write_json(input_ref, contract.model_dump(mode="json"))
            task_id = f"final-chapter-{chapter_id}-r{revision_number}"
            envelope = TaskEnvelope(
                task_id=task_id,
                run_id=run_id,
                agent_id="chief-editor-auditor",
                objective=f"只复核 Chapter {chapter_id} 的 assigned findings 并提交 verdicts。",
                input_refs=[input_ref],
                constraints=[
                    "只提交 final_chapter_lane_verdict_submission",
                    "verdicts 必须覆盖该章全部 required_findings，new_findings 只能留在该章",
                ],
                allowed_outputs=["final_chapter_lane_verdict_submission"],
                allowed_tools=["submit_result"],
                revision=revision_number,
                prior_result_ref=subject_ref,
                input_contract_kind="final_chapter_lane_input",
                input_contract_ref=input_ref,
                artifact_delivery_modes={input_ref: "inline"},
                inline_context=self._final_template_skill_context(state, chapter_id),
            )
            payload = await self._agent(
                "chief-editor-auditor",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"final-chapter-{chapter_id}",
            )
            if not isinstance(payload, FinalChapterLaneVerdictSubmission):
                raise AgentWorkflowError(f"final chapter {chapter_id} returned the wrong verdict type")
            expected_ids = {finding.id for finding in findings}
            actual_ids = {verdict.finding_id for verdict in payload.verdicts}
            if (
                payload.run_id != run_id
                or payload.chapter_id != chapter_id
                or set(payload.checked_section_ids) != set(contract.section_ids)
                or actual_ids != expected_ids
            ):
                raise AgentWorkflowError(f"final chapter {chapter_id} verdict does not cover its lane findings")
            output_ref = (
                f"Work/runs/{run_id}/reviews/final-chapter-lane-"
                f"{chapter_id}-r{revision_number}.json"
            )
            self.service.store.write_json(output_ref, payload.model_dump(mode="json"))
            return chapter_id, payload, output_ref

        verdict_results = await asyncio.gather(
            *(dispatch_recheck(chapter_id) for chapter_id in affected),
            return_exceptions=True,
        )
        failures = [result for result in verdict_results if isinstance(result, BaseException)]
        if failures:
            raise sorted(failures, key=str)[0]
        next_pending: dict[str, list] = {}
        verdict_payloads: dict[str, tuple[FinalChapterLaneVerdictSubmission, str]] = {}
        for chapter_id, payload, output_ref in verdict_results:
            verdict_payloads[chapter_id] = (payload, output_ref)
            self._record_recovery_lane(
                state,
                stage=f"final-recheck-r{revision_number}",
                lane_id=chapter_id,
                status="completed",
                result_ref=output_ref,
                revision=revision_number,
            )
            verdict_by_id = {verdict.finding_id: verdict for verdict in payload.verdicts}
            pending = [
                finding
                for finding in pending_by_chapter[chapter_id]
                if verdict_by_id[finding.id].verdict != "resolved"
            ]
            pending.extend(payload.new_findings)
            if pending:
                next_pending[chapter_id] = pending
        recheck_aggregate_ref = (
            f"Work/runs/{run_id}/reviews/final-recheck-r{revision_number}-aggregate.json"
        )
        self.service.store.write_json(
            recheck_aggregate_ref,
            {
                "run_id": run_id,
                "stage": f"final-recheck-r{revision_number}",
                "status": "completed",
                "lane_ids": list(affected),
                "result_ref": subject_ref,
            },
        )
        self._record_recovery_aggregate(
            state,
            stage=f"final-recheck-r{revision_number}",
            lane_ids=list(affected),
            result_ref=recheck_aggregate_ref,
            revision=revision_number,
        )
        return current, subject_ref, next_pending, verdict_payloads

    async def _final_review_loop(
        self,
        state: dict,
        workflow_id: str,
        *,
        chief_envelope: TaskEnvelope | None,
        chief_session_key: str,
        approved_module_text: dict[str, str],
        claims: list,
        aggregate_mode: bool = False,
    ) -> None:
        await self._run_final_chapter_lanes(state, workflow_id)

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
        report = ValidationReport(
            validation_protocol_version=2,
            run_id=state["run_id"],
            subject_ref=subject_ref,
            subject_revision=module.revision,
            validator="module-structure/v2",
            check_ids=["module.canonical_markdown"],
            failures=[failure] if failure else [],
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
            validate_final_report_markdown(markdown, plan)
        except ValueError as exc:
            self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    validation_protocol_version=2,
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    subject_revision=subject_revision,
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
                validator="final-report-structure/v2",
                check_ids=check_ids,
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
            reviewer_session_key=FINAL_REVIEW_COMPLETION_SESSION_KEYS,
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
            self.service.store.write_text(canonical_ref, canonical)
            self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    validation_protocol_version=2,
                    run_id=run_id,
                    subject_ref=canonical_ref,
                    subject_revision=subject_revision,
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
                    canonical_markdown_ref=canonical_ref,
                    validation_report_ref=validation_ref,
                    completion_ref=completion_ref,
                ).model_dump(mode="json"),
            )
        snapshot = FinalAuditSnapshot.model_validate_json(
            snapshot_path.read_text(encoding="utf-8")
        )
        run_root = (self.service.workspace / f"Work/runs/{run_id}").resolve()
        for ref in (
            snapshot.subject_ref,
            snapshot.canonical_markdown_ref,
            snapshot.validation_report_ref,
            snapshot.completion_ref,
        ):
            path = (self.service.workspace / ref).resolve()
            if not path.is_relative_to(run_root) or not path.is_file():
                raise AgentWorkflowError(
                    f"final audit snapshot ref is outside the current run: {ref}"
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
            or validation.run_id != run_id
            or validation.subject_ref != snapshot.canonical_markdown_ref
            or validation.subject_revision != snapshot.subject_revision
        ):
            raise AgentWorkflowError("final audit snapshot validation identity is stale")
        _, audited_canonical = self._delivery_projection(state, audited)
        validate_final_report_markdown(
            audited_canonical,
            audited.special_topic_plan,
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
        context = self._prepare_and_render_delivery(state)
        context = self._publish_and_materialize_delivery(context)
        self._complete_delivery(context)

    def _prepare_and_render_delivery(self, state: dict) -> DeliveryContext:
        preparation = _DeliveryPreparationDependencies(
            validated_final_audit_subject=self._validated_final_audit_subject,
            write_handoff_contracts=self._write_handoff_contracts,
            delivery_projection=self._delivery_projection,
            validate_final_report_structure=self._validate_final_report_structure,
            resolve_report_template=self.service.resolve_report_template,
        )
        state = DeliveryTools(
            self.service.workspace,
            self.service.store,
            preparation=preparation,
        ).prepare(state)
        return DeliveryContext.model_validate(
            {
                "state": state,
                **state["_declarative_delivery_context"],
            }
        )

    def _publish_and_materialize_delivery(
        self,
        context: DeliveryContext,
    ) -> DeliveryContext:
        return DeliveryTools(
            self.service.workspace,
            self.service.store,
        ).publish_context(context)

    def _complete_delivery(self, context: DeliveryContext) -> None:
        DeliveryTools(
            self.service.workspace,
            self.service.store,
        ).complete_context(context)
        context.state.pop("_declarative_delivery_context", None)

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
