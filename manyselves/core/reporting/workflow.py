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
from .agent_runner import ReportingAgentRunner
from .agentic_models import (
    FINAL_REPORT_SECTION_IDS,
    AgentResult,
    AgentRunStatus,
    CrossSynthesisInput,
    EditedReportSubmission,
    ModuleDispatchPlan,
    ModuleSubmission,
    SubmoduleDraftSubmission,
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
    FinalAuditSnapshot,
    ModuleAuthoringInput,
    RequestedModuleChange,
    ReviewCompletionRecord,
    SubmoduleAuthoringInput,
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
from .module_collaboration import (
    MODULE_IDS,
    ModuleCollaborationBundle,
    ModuleDiscoverySubmission,
    ModuleInterfaceCoverage,
    ModuleInterfaceResponseSubmission,
    ModuleSubmoduleDiscoveryBarrier,
    SubmoduleCollaborationBundle,
    SubmoduleDiscoveryBatchSubmission,
    SubmoduleDiscoverySubmission,
    SubmoduleInterfaceResponseSubmission,
    build_collaboration_bundles,
    build_interface_inboxes,
    build_submodule_collaboration_bundles,
    build_submodule_interface_inboxes,
    reduce_submodule_discoveries,
)
from .module_skills import ModuleSkillLibrary
from .parallel_runtime import (
    ArtifactRef,
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
    collaboration_barrier1_ref: str | None = None
    collaboration_barrier2_ref: str | None = None
    collaboration_bundle_refs: dict[str, str] = Field(default_factory=dict)
    submodule_discovery_barrier_refs: dict[str, str] = Field(default_factory=dict)
    submodule_collaboration_bundle_refs: dict[str, str] = Field(default_factory=dict)
    submodule_authoring_barrier_refs: dict[str, str] = Field(default_factory=dict)
    module_lane_barrier_ref: str | None = None
    cross_owner_barrier_ref: str | None = None
    quality_context_ref: str | None = None
    module_review_completion_refs: dict[str, str] = Field(default_factory=dict)
    cross_review_completion_ref: str | None = None
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

    @staticmethod
    def _leaf_knowledge_excerpt(text: str, submodule_id: str) -> str:
        """Return the shared header plus exactly one taxonomy leaf Knowledge block."""

        heading = re.compile(r"(?m)^##\s+(\d+(?:\.\d+)+)\b.*$")
        matches = list(heading.finditer(text))
        header = text[: matches[0].start()].strip() if matches else ""
        leaf = ""
        for index, match in enumerate(matches):
            if match.group(1) != submodule_id:
                continue
            end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
            leaf = text[match.start() : end].strip()
            break
        if not leaf:
            leaf = (
                f"## {submodule_id}\n"
                "未找到该叶子的确定性 Knowledge 小节；需要时打开共享 Knowledge 引用，"
                "且不得把通用知识写成客户事实。"
            )
        return "\n\n".join(item for item in (header, leaf) if item)

    @staticmethod
    def _artifact_sha256(workspace: Path, ref: str | None) -> str | None:
        if not ref:
            return None
        path = (workspace / ref).resolve()
        if not path.is_relative_to(workspace) or not path.is_file():
            return None
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _shared_module_context_ref(
        self,
        state: dict,
        module_id: str,
        *,
        purpose: str,
        include_author_skill: bool = False,
    ) -> str:
        """Persist a small sibling-shared directory; large sources remain references."""

        workspace = self.service.workspace
        preparation = state.get("preparation_refs", {})
        source_refs: dict[str, str] = {
            "knowledge": state["module_knowledge_refs"][module_id],
        }
        if preparation.get("manifest"):
            source_refs["manifest"] = preparation["manifest"]
        if include_author_skill:
            for key in ("core", "analysis", "visual", "rubric"):
                ref = state.get("template_skill_refs", {}).get(key)
                if ref:
                    source_refs[f"template_skill_{key}"] = ref
        body = {
            "kind": "shared_module_context",
            "version": 1,
            "purpose": purpose,
            "run_id": state["run_id"],
            "module_id": module_id,
            "module_title": REPORT_TAXONOMY[module_id].title,
            "peer_target_taxonomy": {
                peer_id: {
                    "title": definition.title,
                    "leaves": [
                        {"id": item.id, "title": item.title}
                        for item in definition.submodules.values()
                    ],
                }
                for peer_id, definition in REPORT_TAXONOMY.items()
                if peer_id != module_id
            },
            "source_refs": source_refs,
            "source_sha256": {
                name: self._artifact_sha256(workspace, ref)
                for name, ref in source_refs.items()
            },
        }
        canonical = json.dumps(
            body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        body["content_sha256"] = digest
        path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/context/shared/"
                f"{purpose}-module-{module_id}-{digest[:12]}.json"
            ),
            body,
        )
        return path.relative_to(workspace).as_posix()

    def _module_author_leaf_inline_context(
        self,
        state: dict,
        module_id: str,
        submodule_id: str,
        shared_ref: str,
    ) -> str:
        knowledge_ref = state["module_knowledge_refs"][module_id]
        knowledge_path = self.service.workspace / knowledge_ref
        knowledge_text = (
            knowledge_path.read_text(encoding="utf-8")
            if knowledge_path.is_file()
            else ""
        )
        leaf_knowledge = self._leaf_knowledge_excerpt(knowledge_text, submodule_id)
        core_method = state.get("template_skill_text", {}).get("core", "").strip()
        parts = [
            (
                f'<shared_module_context ref="{shared_ref}" delivery_mode="reference">'
                "完整模块 Knowledge、manifest 与写作 Skill 只在需要补充当前叶子增量时按需读取。"
                "</shared_module_context>"
            ),
            (
                f'<leaf_knowledge_delta submodule_id="{submodule_id}" '
                f'provenance_ref="{knowledge_ref}" project_fact_authority="false">\n'
                f"{leaf_knowledge}\n</leaf_knowledge_delta>"
            ),
        ]
        if core_method:
            parts.append(
                '<module_author_core_method delivery_mode="inline">\n'
                + core_method
                + "\n</module_author_core_method>"
            )
        return "\n\n".join(parts)

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
    ) -> tuple[bool, bool, bool]:
        """Prepare leaf work for every writing path; only review overlap is optional."""

        full_scope = set(requested_modules) == set(REPORT_MODULE_IDS)
        bounded_lanes = (
            getattr(
                state["request"],
                "execution_mode",
                "current_serial_review",
            )
            == "bounded_module_lanes"
        ) and len(requested_modules) > 1
        await self.service._notice(
            (
                "五个专业模块的固定叶子进入独立调度：先并行发现接口问题，经两次"
                "确定性跨模块屏障形成叶子协作包，再独立写作、归并和审查。"
                if full_scope
                else "所请求模块将拆成固定叶子的独立、可恢复任务；叶子归并后再进入模块审查。"
            )
        )
        if full_scope:
            await self._module_collaboration(
                requested_modules,
                state,
                workflow_id,
            )
        else:
            await self._module_local_submodule_preparation(
                requested_modules,
                state,
                workflow_id,
            )
        await self._run_submodule_authoring_stage(
            requested_modules,
            state,
            workflow_id,
        )
        if bounded_lanes:
            await self._run_bounded_module_lanes(
                requested_modules,
                state,
                workflow_id,
                concurrency=state["request"].module_lane_concurrency,
            )
        return True, bounded_lanes, full_scope

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
            (
                bounded_leaf_mode,
                bounded_lanes,
                full_scope,
            ) = await self._prepare_module_authoring_mode(
                requested_modules,
                state,
                workflow_id,
            )
            pending_author_modules = tuple(
                module_id
                for module_id in requested_modules
                if not bounded_lanes
                if module_id
                not in (
                    state.get("specialist_submissions", {})
                    if bounded_leaf_mode or full_scope
                    else state.get("module_submissions", {})
                )
            )
            if pending_author_modules and (bounded_leaf_mode or full_scope):
                author_results = await asyncio.gather(
                    *(
                        self._module_pipeline(
                            module_id,
                            state,
                            workflow_id,
                            review=False,
                        )
                        for module_id in pending_author_modules
                    ),
                    return_exceptions=True,
                )
                author_failures = [
                    result
                    for result in author_results
                    if isinstance(result, BaseException)
                ]
                if author_failures:
                    self._checkpoint(
                        state,
                        activity,
                        "failed",
                        "; ".join(str(error) for error in author_failures),
                    )
                    raise author_failures[0]
                await self._checkpoint_then_cost_boundary(
                    state,
                    activity,
                    "in_progress",
                    "module-authoring",
                    f"module-review-{requested_modules[0]}",
                )
            elif pending_author_modules:
                for module_index, module_id in enumerate(pending_author_modules):
                    try:
                        submission = await self._module_pipeline(
                            module_id,
                            state,
                            workflow_id,
                        )
                    except BaseException:
                        self._checkpoint(state, activity, "failed")
                        raise
                    state.setdefault("module_submissions", {})[
                        submission.module_id
                    ] = submission
                    remaining = pending_author_modules[module_index + 1 :]
                    if remaining:
                        await self._checkpoint_then_cost_boundary(
                            state,
                            activity,
                            "in_progress",
                            f"module-{module_id}",
                            f"module-{remaining[0]}",
                        )
                    else:
                        self._checkpoint(state, activity, "in_progress")
            state["module_submissions"] = dict(
                state.get("module_submissions", {})
            )
            pending_review_modules = tuple(
                module_id
                for module_id in requested_modules
                if (bounded_leaf_mode or full_scope)
                if module_id not in state["module_submissions"]
            )
            for module_index, module_id in enumerate(pending_review_modules):
                specialist_payload = state.get("specialist_submissions", {}).get(
                    module_id
                )
                if specialist_payload is None:
                    raise AgentWorkflowError(
                        f"module {module_id} has no specialist submission after Wave 3"
                    )
                try:
                    submission = await self._module_review_loop(
                        module_id,
                        specialist_payload,
                        state,
                        workflow_id,
                        initial_scope=set(REPORT_TAXONOMY[module_id].submodules),
                    )
                except BaseException:
                    self._checkpoint(state, activity, "failed")
                    raise
                state["module_submissions"][submission.module_id] = submission
                self._bind_reviewed_module_to_authoring_context(
                    state,
                    module_id,
                    submission,
                    provenance="module_review",
                )
                remaining = pending_review_modules[module_index + 1 :]
                if remaining:
                    await self._checkpoint_then_cost_boundary(
                        state,
                        activity,
                        "in_progress",
                        f"module-review-{module_id}",
                        f"module-review-{remaining[0]}",
                    )
                else:
                    self._checkpoint(state, activity, "in_progress")
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
                for module_id, submission in state["module_submissions"].items():
                    self._bind_reviewed_module_to_authoring_context(
                        state,
                        module_id,
                        submission,
                        provenance="cross_review",
                    )
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
            collaboration_barrier1_ref=state.get("collaboration_barrier1_ref"),
            collaboration_barrier2_ref=state.get("collaboration_barrier2_ref"),
            collaboration_bundle_refs=dict(
                state.get("collaboration_bundle_refs", {})
            ),
            submodule_discovery_barrier_refs=dict(
                state.get("submodule_discovery_barrier_refs", {})
            ),
            submodule_collaboration_bundle_refs=dict(
                state.get("submodule_collaboration_bundle_refs", {})
            ),
            submodule_authoring_barrier_refs=dict(
                state.get("submodule_authoring_barrier_refs", {})
            ),
            module_lane_barrier_ref=state.get("module_lane_barrier_ref"),
            cross_owner_barrier_ref=state.get("cross_owner_barrier_ref"),
            quality_context_ref=state.get("quality_context_ref"),
            report_state_ref=("Work/report-state.json" if "edited_report" in state else None),
            module_review_completion_refs=dict(state.get("module_review_completion_refs", {})),
            cross_review_completed="cross_review_completion_ref" in state,
            cross_review_completion_ref=state.get("cross_review_completion_ref"),
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

    def _current_chief_editor_input(self, state: dict) -> ChiefEditorInput:
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
            cross_review_completion_ref=state[
                "cross_review_completion_ref"
            ],
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
        preparation = state.get("preparation_refs", {})
        missing = [
            name
            for name in ("evidence", "photo_manifest")
            if not preparation.get(name)
        ]
        if missing:
            raise AgentWorkflowError(
                "Chief completion cannot reconstruct preparation refs: "
                f"{missing}"
            )
        special_ref, _ = self._chief_special_topic_context(state)
        return [
            f"Work/runs/{state['run_id']}/context/chief-editor-input.json",
            str(preparation["evidence"]),
            str(preparation["photo_manifest"]),
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
        """Fingerprint every durable input that makes a Wave 3 draft reusable."""

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
            "version": 2,
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
            "collaboration_bundle": ref_record(
                state.get("collaboration_bundle_refs", {}).get(module_id)
            ),
            "collaboration_barrier_2": ref_record(
                state.get("collaboration_barrier2_ref")
            ),
            "submodule_discoveries": [
                ref_record(
                    f"Work/runs/{state['run_id']}/collaboration/"
                    f"wave-1/submodules/{submodule_id}.json"
                )
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            ],
            "submodule_collaboration_bundles": [
                ref_record(
                    state.get("submodule_collaboration_bundle_refs", {}).get(
                        submodule_id
                    )
                )
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            ],
        }
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def _module_authoring_completion_is_current(
        self,
        state: dict,
        module_id: str,
        submission: ModuleSubmission,
    ) -> bool:
        """Require a Wave 3 subject to prove which current bundle it consumed."""

        completion_ref = (
            f"Work/runs/{state['run_id']}/collaboration/wave-3/"
            f"module-{module_id}-r{submission.revision}.json"
        )
        completion_path = self.service.workspace / completion_ref
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module_id}-r{submission.revision}.json"
        )
        subject_path = self.service.workspace / subject_ref
        if not completion_path.is_file() or not subject_path.is_file():
            return False
        try:
            completion = json.loads(
                completion_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError):
            return False
        common_valid = (
            completion.get("kind") == "module_authoring_completion"
            and completion.get("run_id") == state["run_id"]
            and completion.get("module_id") == module_id
            and completion.get("subject_ref") == subject_ref
            and completion.get("subject_sha256")
            == self._sha256(subject_path)
            and completion.get("authoring_context_sha256")
            == self._module_authoring_context_sha256(state, module_id)
        )
        if not common_valid:
            return False
        if completion.get("source_mode") != "submodule_reducer":
            return True
        barrier_ref = state.get("submodule_authoring_barrier_refs", {}).get(
            module_id
        )
        if not barrier_ref or completion.get("submodule_barrier_ref") != barrier_ref:
            return False
        barrier_path = self.service.workspace / barrier_ref
        if not barrier_path.is_file():
            return False
        return completion.get("submodule_barrier_sha256") == self._sha256(
            barrier_path
        )

    def _write_module_authoring_completion(
        self,
        state: dict,
        module_id: str,
        submission: ModuleSubmission,
        *,
        module_input_ref: str,
        envelope: TaskEnvelope,
    ) -> str:
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module_id}-r{submission.revision}.json"
        )
        subject_path = self.service.workspace / subject_ref
        module_input_path = self.service.workspace / module_input_ref
        completion_path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/collaboration/wave-3/"
                f"module-{module_id}-r{submission.revision}.json"
            ),
            {
                "kind": "module_authoring_completion",
                "version": 1,
                "run_id": state["run_id"],
                "module_id": module_id,
                "revision": submission.revision,
                "subject_ref": subject_ref,
                "subject_sha256": self._sha256(subject_path),
                "module_input_ref": module_input_ref,
                "module_input_sha256": self._sha256(module_input_path),
                "authoring_context_sha256": (
                    self._module_authoring_context_sha256(state, module_id)
                ),
                "envelope_sha256": hashlib.sha256(
                    json.dumps(
                        envelope.model_dump(mode="json"),
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest(),
            },
        )
        return completion_path.relative_to(
            self.service.workspace
        ).as_posix()

    def _bind_reviewed_module_to_authoring_context(
        self,
        state: dict,
        module_id: str,
        submission: ModuleSubmission,
        *,
        provenance: str,
    ) -> str | None:
        """Carry the current Wave 3 context binding across reviewed revisions."""

        if module_id not in state.get("collaboration_bundle_refs", {}):
            return None
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module_id}-r{submission.revision}.json"
        )
        subject_path = self.service.workspace / subject_ref
        if not subject_path.is_file():
            raise AgentWorkflowError(
                f"reviewed module subject is missing: {subject_ref}"
            )
        completion_path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/collaboration/wave-3/"
                f"module-{module_id}-r{submission.revision}.json"
            ),
            {
                "kind": "module_authoring_completion",
                "version": 1,
                "run_id": state["run_id"],
                "module_id": module_id,
                "revision": submission.revision,
                "subject_ref": subject_ref,
                "subject_sha256": self._sha256(subject_path),
                "authoring_context_sha256": (
                    self._module_authoring_context_sha256(state, module_id)
                ),
                "provenance": provenance,
            },
        )
        return completion_path.relative_to(
            self.service.workspace
        ).as_posix()

    def _restore_resume_state(self, state: dict, checkpoint: dict | None = None) -> None:
        run_id = state["run_id"]
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
        collaboration_refs_present = bool(
            typed_checkpoint.collaboration_barrier1_ref
            or typed_checkpoint.collaboration_barrier2_ref
            or typed_checkpoint.collaboration_bundle_refs
        )
        if collaboration_refs_present:
            if typed_checkpoint.collaboration_barrier1_ref is None:
                raise AgentWorkflowError(
                    "checkpoint has collaboration output without Barrier 1"
                )
            state["collaboration_barrier1_ref"] = require_run_ref(
                typed_checkpoint.collaboration_barrier1_ref,
                label="collaboration Barrier 1",
            )
            if typed_checkpoint.collaboration_barrier2_ref is not None:
                state["collaboration_barrier2_ref"] = require_run_ref(
                    typed_checkpoint.collaboration_barrier2_ref,
                    label="collaboration Barrier 2",
                )
            target_modules = set(state.get("request").target_modules)
            unexpected_bundles = sorted(
                set(typed_checkpoint.collaboration_bundle_refs) - target_modules
            )
            if unexpected_bundles:
                raise AgentWorkflowError(
                    "checkpoint contains collaboration bundles outside the request: "
                    f"{unexpected_bundles}"
                )
            state["collaboration_bundle_refs"] = {
                module_id: require_run_ref(
                    ref,
                    label=f"module {module_id} collaboration bundle",
                )
                for module_id, ref in (
                    typed_checkpoint.collaboration_bundle_refs.items()
                )
            }
            if typed_checkpoint.collaboration_barrier2_ref is not None:
                missing_bundles = sorted(
                    target_modules - set(state["collaboration_bundle_refs"])
                )
                if missing_bundles:
                    raise AgentWorkflowError(
                        "Barrier 2 checkpoint lacks module bundles: "
                        f"{missing_bundles}"
                    )
        submodule_refs_present = bool(
            typed_checkpoint.submodule_discovery_barrier_refs
            or typed_checkpoint.submodule_collaboration_bundle_refs
            or typed_checkpoint.submodule_authoring_barrier_refs
        )
        requested_modules = set(
            getattr(state.get("request"), "target_modules", ())
        )
        if submodule_refs_present and not requested_modules:
            raise AgentWorkflowError(
                "checkpoint has submodule orchestration refs without a current request"
            )
        requested_submodules = {
            submodule_id
            for module_id in requested_modules
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        }
        if typed_checkpoint.submodule_discovery_barrier_refs:
            unexpected = sorted(
                set(typed_checkpoint.submodule_discovery_barrier_refs)
                - requested_modules
            )
            if unexpected:
                raise AgentWorkflowError(
                    "checkpoint contains submodule discovery barriers outside request: "
                    f"{unexpected}"
                )
            state["submodule_discovery_barrier_refs"] = {
                module_id: require_run_ref(
                    ref,
                    label=f"module {module_id} submodule discovery barrier",
                )
                for module_id, ref in (
                    typed_checkpoint.submodule_discovery_barrier_refs.items()
                )
            }
        if typed_checkpoint.submodule_collaboration_bundle_refs:
            unexpected = sorted(
                set(typed_checkpoint.submodule_collaboration_bundle_refs)
                - requested_submodules
            )
            if unexpected:
                raise AgentWorkflowError(
                    "checkpoint contains collaboration bundles outside leaf scope: "
                    f"{unexpected}"
                )
            state["submodule_collaboration_bundle_refs"] = {
                submodule_id: require_run_ref(
                    ref,
                    label=f"submodule {submodule_id} collaboration bundle",
                )
                for submodule_id, ref in (
                    typed_checkpoint.submodule_collaboration_bundle_refs.items()
                )
            }
        if typed_checkpoint.submodule_authoring_barrier_refs:
            unexpected = sorted(
                set(typed_checkpoint.submodule_authoring_barrier_refs)
                - requested_modules
            )
            if unexpected:
                raise AgentWorkflowError(
                    "checkpoint contains submodule authoring barriers outside request: "
                    f"{unexpected}"
                )
            state["submodule_authoring_barrier_refs"] = {
                module_id: require_run_ref(
                    ref,
                    label=f"module {module_id} submodule authoring barrier",
                )
                for module_id, ref in (
                    typed_checkpoint.submodule_authoring_barrier_refs.items()
                )
            }
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
        invalidated_authoring_revisions: dict[str, int] = {}
        modules_root = self.service.workspace / f"Work/runs/{run_id}/modules"
        for module_id in REPORT_MODULE_IDS:
            valid: list[ModuleSubmission] = []
            context_mismatch_revisions: list[int] = []
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
                    if (
                        typed_checkpoint.collaboration_barrier2_ref is not None
                        and not self._module_authoring_completion_is_current(
                            state,
                            module_id,
                            submission,
                        )
                    ):
                        raise ValueError(
                            "Wave 3 subject is not bound to the current "
                            "collaboration bundle and authoring context"
                        )
                except (OSError, ValueError) as exc:
                    rejected[module_id] = str(exc)
                    if (
                        "not bound to the current collaboration bundle"
                        in str(exc)
                    ):
                        match = re.search(r"-r([0-9]+)\.json$", path.name)
                        if match:
                            context_mismatch_revisions.append(
                                int(match.group(1))
                            )
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
            elif context_mismatch_revisions:
                invalidated_authoring_revisions[module_id] = (
                    max(context_mismatch_revisions) + 1
                )
        state["specialist_submissions"] = restored_subjects
        state["module_submissions"] = approved_subjects
        state["rejected_specialist_submissions"] = rejected
        state["invalidated_authoring_revisions"] = (
            invalidated_authoring_revisions
        )
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
        try:
            _, cross_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=cross_ref,
                lifecycle="cross",
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key="cross-module-reviewer",
                subject_refs=module_refs,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay Cross review because its current-run "
                f"completion is invalid: {cross_ref}: {exc}"
            ) from exc
        state["cross_review_completion_ref"] = cross_ref
        state["cross_synthesis_inputs"] = self._latest_cross_synthesis(cross_artifacts)

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
        run_id = state["run_id"]
        completion_ref = f"Work/runs/{run_id}/delivery-completion.json"
        completion_path = self.service.workspace / completion_ref
        if not completion_path.is_file():
            return
        payload = json.loads(completion_path.read_text(encoding="utf-8"))
        if payload.get("run_id") != run_id or payload.get("status") != "completed":
            raise AgentWorkflowError("delivery completion identity/status is invalid")
        receipt_ref = str(payload.get("delivery_receipt_ref", ""))
        run_prefix = f"Work/runs/{run_id}/"
        if not receipt_ref.startswith(run_prefix):
            raise AgentWorkflowError("delivery receipt is outside the current run")
        receipt_path = self.service.workspace / receipt_ref
        if not receipt_path.is_file():
            raise AgentWorkflowError("delivery completion references a missing receipt")
        receipt = DeliveryReceipt.model_validate_json(receipt_path.read_text(encoding="utf-8"))
        delivery_root = self._delivery_root(self.service.workspace, run_id).resolve()
        if not receipt.manifest_path.resolve().is_relative_to(delivery_root):
            return
        required_paths = {
            "final_docx": receipt.final_docx,
            "report_state": receipt.report_state,
            "manifest": receipt.manifest_path,
            **{f"module:{module_id}": path for module_id, path in receipt.module_files.items()},
        }
        for key, path in required_paths.items():
            resolved = Path(path).resolve()
            if (
                not resolved.is_relative_to(self.service.workspace)
                or not resolved.is_file()
                or self._sha256(resolved) != receipt.artifact_sha256.get(key)
            ):
                raise AgentWorkflowError(
                    f"delivery completion artifact failed hash validation: {key}"
                )
        version = ReportVersionStore(self.service.workspace).load(run_id)
        if version.run_id != run_id:
            raise AgentWorkflowError("delivery completion report version belongs to another run")
        artifacts = [
            OutputArtifact.model_validate(item) for item in payload.get("output_artifacts", [])
        ]
        if not artifacts:
            raise AgentWorkflowError("delivery completion lacks output artifacts")
        expected_artifacts = self._delivery_output_artifacts(
            final_review_ref=state["final_review_completion_ref"],
            delivery_manifest_ref=receipt.manifest_path.resolve().relative_to(
                self.service.workspace
            ),
        )
        if artifacts != expected_artifacts:
            # The persisted completion does not implement the current delivery
            # contract. Leave delivery unrestored so this same run deterministically
            # regenerates and republishes its outputs.
            return
        state["report_version"] = version
        state["output_artifacts"] = artifacts
        state["delivery_completion_ref"] = completion_ref
        # The completion record and receipt hashes above prove these artifacts
        # belong to this exact run.  A later resume may therefore reuse them
        # without pretending they were regenerated during the new invocation.
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
        try:
            _, cross_artifacts = self._load_current_review_completion(
                run_id=run_id,
                completion_ref=cross_ref,
                lifecycle="cross",
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key="cross-module-reviewer",
                subject_refs=module_refs,
            )
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay Cross revision because its current-run "
                f"completion is invalid: {cross_ref}: {exc}"
            ) from exc
        state["cross_review_completion_ref"] = cross_ref
        state["cross_synthesis_inputs"] = self._latest_cross_synthesis(cross_artifacts)
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

    def _load_collaboration_submission(
        self,
        *,
        run_id: str,
        artifact_ref: str,
        task_id: str,
        module_id: str,
        expected_type: type[Any],
        submodule_id: str | None = None,
        wave: str | None = None,
        expected_context_sha256: str | None = None,
        require_payload_submodule_identity: bool = True,
    ) -> Any | None:
        """Recover a typed wave candidate without promoting it before its Barrier."""

        candidate_wave = wave or (
            "wave-1" if "discovery" in task_id else "wave-2"
        )

        artifact_path = self.service.workspace / artifact_ref
        completion_ref = (
            f"Work/runs/{run_id}/collaboration/completions/"
            f"{candidate_wave}/{task_id}.json"
        )
        completion_path = self.service.workspace / completion_ref
        if expected_context_sha256 is not None:
            if not artifact_path.is_file() or not completion_path.is_file():
                return None
            try:
                completion = json.loads(
                    completion_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                return None
            if (
                completion.get("kind") != "submodule_task_completion"
                or completion.get("run_id") != run_id
                or completion.get("task_id") != task_id
                or completion.get("module_id") != module_id
                or completion.get("submodule_id") != submodule_id
                or completion.get("wave") != candidate_wave
                or completion.get("artifact_ref") != artifact_ref
                or completion.get("context_sha256")
                != expected_context_sha256
                or completion.get("artifact_sha256")
                != self._sha256(artifact_path)
            ):
                self._reject_collaboration_candidates(
                    run_id=run_id,
                    wave=candidate_wave,
                    artifact_refs=[artifact_ref, completion_ref],
                    task_ids=[task_id],
                    reason="submodule completion no longer matches its exact task context",
                )
                return None
        if artifact_path.is_file():
            run_root = (
                self.service.workspace / f"Work/runs/{run_id}"
            ).resolve()
            if (
                artifact_path.is_symlink()
                or not artifact_path.resolve().is_relative_to(run_root)
            ):
                self._reject_collaboration_candidates(
                    run_id=run_id,
                    wave=candidate_wave,
                    artifact_refs=[artifact_ref],
                    task_ids=[task_id],
                    reason="collaboration artifact is not a regular current-run file",
                )
                return None
            try:
                payload = expected_type.model_validate_json(
                    artifact_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                self._reject_collaboration_candidates(
                    run_id=run_id,
                    wave=candidate_wave,
                    artifact_refs=[artifact_ref],
                    task_ids=[task_id],
                    reason=f"invalid persisted collaboration artifact: {exc}",
                )
                return None
        else:
            result_path = (
                self.service.workspace
                / f"Work/runs/{run_id}/results/{task_id}.json"
            )
            if not result_path.is_file():
                return None
            try:
                result = AgentResult.model_validate_json(
                    result_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                self._reject_collaboration_candidates(
                    run_id=run_id,
                    wave=candidate_wave,
                    artifact_refs=[],
                    task_ids=[task_id],
                    reason=f"invalid persisted collaboration task result: {exc}",
                )
                return None
            if (
                result.run_id != run_id
                or result.task_id != task_id
                or result.agent_id
                != f"module-{module_id}-specialist"
            ):
                self._reject_collaboration_candidates(
                    run_id=run_id,
                    wave=candidate_wave,
                    artifact_refs=[],
                    task_ids=[task_id],
                    reason=(
                        "collaboration result identity does not match its "
                        "current-run task and specialist"
                    ),
                )
                return None
            if (
                result.status is not AgentRunStatus.COMPLETED
                or not isinstance(result.payload, expected_type)
            ):
                return None
            payload = result.payload
        if payload.module_id != module_id:
            self._reject_collaboration_candidates(
                run_id=run_id,
                wave=candidate_wave,
                artifact_refs=[artifact_ref],
                task_ids=[task_id],
                reason=(
                    f"collaboration payload belongs to module {payload.module_id}, "
                    f"expected {module_id}"
                ),
            )
            return None
        if (
            submodule_id is not None
            and require_payload_submodule_identity
            and getattr(payload, "submodule_id", None) != submodule_id
        ):
            self._reject_collaboration_candidates(
                run_id=run_id,
                wave=candidate_wave,
                artifact_refs=[artifact_ref],
                task_ids=[task_id],
                reason=(
                    "collaboration payload belongs to submodule "
                    f"{getattr(payload, 'submodule_id', None)}, expected {submodule_id}"
                ),
            )
            return None
        return payload

    def _submodule_task_context_sha256(
        self,
        state: dict,
        *,
        task_kind: str,
        submodule_id: str,
        input_refs: list[str],
    ) -> str:
        """Fingerprint the exact durable inputs and plan for one leaf task."""

        module_id = resolve_submodule(submodule_id).module_id
        planned = next(
            (
                item.model_dump(mode="json")
                for item in state["module_dispatch"].module_tasks
                if item.agent_id == f"module-{module_id}-specialist"
            ),
            None,
        )
        request = state.get("request")
        ref_records = []
        for ref in input_refs:
            path = (self.service.workspace / ref).resolve()
            ref_records.append(
                {
                    "ref": ref,
                    "sha256": (
                        self._sha256(path)
                        if path.is_relative_to(self.service.workspace)
                        and path.is_file()
                        else None
                    ),
                }
            )
        payload = {
            "version": 1,
            "run_id": state["run_id"],
            "task_kind": task_kind,
            "module_id": module_id,
            "submodule_id": submodule_id,
            "planned_task": planned,
            "input_refs": ref_records,
            "execution_requirements": list(
                getattr(request, "execution_requirements", [])
            ),
            "missing_evidence_policy": getattr(
                request, "missing_evidence_policy", None
            ),
            "supplement_constraints": (
                self._user_supplement_constraints(
                    state,
                    stage=task_kind,
                    target_ids={module_id, submodule_id},
                )
                if request is not None
                and hasattr(request, "user_supplements")
                else []
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

    def _persist_submodule_task_completion(
        self,
        *,
        state: dict,
        task_kind: str,
        wave: str,
        submodule_id: str,
        artifact_ref: str,
        context_sha256: str,
        payload: Any,
    ) -> None:
        """Persist a typed leaf result and its exact-context recovery proof."""

        module_id = resolve_submodule(submodule_id).module_id
        artifact_path = self.service.store.write_json(
            artifact_ref, payload.model_dump(mode="json")
        )
        task_prefix = {
            "submodule_authoring": "submodule-author",
        }.get(task_kind, task_kind.replace("_", "-"))
        task_id = f"{task_prefix}-{submodule_id}"
        self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/collaboration/completions/"
                f"{wave}/{task_id}.json"
            ),
            {
                "kind": "submodule_task_completion",
                "version": 1,
                "run_id": state["run_id"],
                "wave": wave,
                "task_id": task_id,
                "module_id": module_id,
                "submodule_id": submodule_id,
                "artifact_ref": artifact_ref,
                "artifact_sha256": self._sha256(artifact_path),
                "context_sha256": context_sha256,
            },
        )

    def _reject_collaboration_candidates(
        self,
        *,
        run_id: str,
        wave: str,
        artifact_refs: list[str],
        task_ids: list[str],
        reason: str,
    ) -> str:
        """Move Barrier-invalid candidates aside so explicit resume can retry."""

        rejection_id = uuid4().hex
        run_root = (
            self.service.workspace / f"Work/runs/{run_id}"
        ).resolve()
        rejected_root = (
            run_root / "collaboration" / "rejected" / wave / rejection_id
        )
        moved: dict[str, str] = {}
        candidates = [
            *(
                ("artifact", ref, self.service.workspace / ref)
                for ref in artifact_refs
            ),
            *(
                (
                    "result",
                    f"Work/runs/{run_id}/results/{task_id}.json",
                    run_root / "results" / f"{task_id}.json",
                )
                for task_id in task_ids
            ),
        ]
        for category, ref, source in candidates:
            lexical = source.absolute()
            if (
                not lexical.is_relative_to(run_root)
                or not (source.exists() or source.is_symlink())
            ):
                continue
            target = rejected_root / category / source.name
            target.parent.mkdir(parents=True, exist_ok=True)
            source.replace(target)
            moved[ref] = target.relative_to(
                self.service.workspace
            ).as_posix()
        manifest_ref = (
            rejected_root / "rejection.json"
        ).relative_to(self.service.workspace).as_posix()
        self.service.store.write_json(
            manifest_ref,
            {
                "kind": "module_collaboration_rejection",
                "run_id": run_id,
                "wave": wave,
                "reason": reason,
                "moved": moved,
            },
        )
        return manifest_ref

    async def _run_scheduled_submodule_stage(
        self,
        submodule_ids: tuple[str, ...],
        *,
        run_id: str,
        workflow_id: str,
        task_kind: str,
        concurrency: int,
        execute,
        persist=None,
    ) -> dict[str, Any]:
        """Run one bounded leaf cohort with stable history-informed dispatch."""

        ordered = tuple(dict.fromkeys(submodule_ids))
        history = TaskTimingHistory(self.service.workspace)
        scheduler = AdaptiveTaskScheduler(
            [
                SchedulingCandidate(
                    task_id=f"{task_kind}:{submodule_id}",
                    owner_key=resolve_submodule(submodule_id).module_id,
                    task_kind=task_kind,
                    ordinal=index,
                    expected_duration_ms=history.estimate_ms(
                        task_kind,
                        submodule_id,
                        default=60_000,
                    ),
                )
                for index, submodule_id in enumerate(ordered)
            ]
        )
        event_store = LocalEventStore(self.service.workspace, run_id)
        stage_id = task_kind.replace("_", "-")
        event_store.append(
            "StageReady",
            stage_id=stage_id,
            correlation_id=workflow_id,
            payload={
                "submodule_ids": list(ordered),
                "concurrency": min(max(1, concurrency), max(1, len(ordered))),
            },
        )
        results: dict[str, Any] = {}
        failures: list[BaseException] = []
        stop_dispatch = asyncio.Event()

        async def worker() -> None:
            while not stop_dispatch.is_set():
                candidate = await scheduler.next()
                if candidate is None:
                    return
                submodule_id = candidate.task_id.split(":", 1)[1]
                event_store.append(
                    "TaskDispatched",
                    stage_id=stage_id,
                    task_id=candidate.task_id,
                    correlation_id=workflow_id,
                    payload={"submodule_id": submodule_id},
                )
                started = time.perf_counter_ns()
                try:
                    payload = await execute(submodule_id)
                    if persist is not None:
                        persist(submodule_id, payload)
                    results[submodule_id] = payload
                except BaseException as exc:
                    failures.append(exc)
                    stop_dispatch.set()
                    history.record(
                        run_id=run_id,
                        task_id=candidate.task_id,
                        task_kind=task_kind,
                        owner_key=submodule_id,
                        duration_ms=max(
                            0, (time.perf_counter_ns() - started) // 1_000_000
                        ),
                        status="failed",
                    )
                    event_store.append(
                        "TaskFailed",
                        stage_id=stage_id,
                        task_id=candidate.task_id,
                        correlation_id=workflow_id,
                        payload={"error": str(exc)},
                    )
                    return
                duration_ms = max(
                    0, (time.perf_counter_ns() - started) // 1_000_000
                )
                history.record(
                    run_id=run_id,
                    task_id=candidate.task_id,
                    task_kind=task_kind,
                    owner_key=submodule_id,
                    duration_ms=duration_ms,
                    status="completed",
                )
                event_store.append(
                    "TypedResultAccepted",
                    stage_id=stage_id,
                    task_id=candidate.task_id,
                    correlation_id=workflow_id,
                    payload={
                        "submodule_id": submodule_id,
                        "duration_ms": duration_ms,
                    },
                )

        worker_count = min(max(1, concurrency), max(1, len(ordered)))
        await asyncio.gather(*(worker() for _ in range(worker_count)))
        self.service.store.write_json(
            f"Work/runs/{run_id}/scheduling/{stage_id}.json",
            {
                "kind": "submodule_scheduling_decisions",
                "run_id": run_id,
                "stage_id": stage_id,
                "policy": "longest_critical_path_first_v1",
                "concurrency": worker_count,
                "decisions": [
                    item.model_dump(mode="json") for item in scheduler.decisions
                ],
            },
        )
        if failures:
            raise failures[0]
        if set(results) != set(ordered):
            raise AgentWorkflowError(
                f"{task_kind} ended without exact leaf completion; "
                f"missing={sorted(set(ordered) - set(results))}"
            )
        event_store.append(
            "StageCompleted",
            stage_id=stage_id,
            correlation_id=workflow_id,
            payload={"submodule_ids": list(ordered)},
        )
        return results

    @staticmethod
    def _jsonable_context_item(item: object) -> dict[str, object]:
        """Serialize runtime evidence without requiring test doubles to be Pydantic."""

        if hasattr(item, "model_dump"):
            return item.model_dump(mode="json")
        if isinstance(item, dict):
            return dict(item)
        return {
            key: value
            for key, value in vars(item).items()
            if not key.startswith("_")
        }

    def _leaf_collaboration_context_packet(
        self,
        submodule_ids: tuple[str, ...],
        state: dict,
        *,
        purpose: str,
    ) -> tuple[str, str, str]:
        """Persist one leaf delta plus one sibling-shared context directory."""

        ordered = tuple(dict.fromkeys(submodule_ids))
        if not ordered:
            raise AgentWorkflowError("leaf collaboration context requires at least one leaf")
        module_ids = {resolve_submodule(item).module_id for item in ordered}
        if len(module_ids) != 1:
            raise AgentWorkflowError("leaf collaboration context may not cross module owners")
        module_id = next(iter(module_ids))
        workspace = self.service.workspace

        def read_json_ref(ref: str | None) -> object:
            if not ref:
                return {}
            path = workspace / ref
            if not path.is_file():
                return {}
            return json.loads(path.read_text(encoding="utf-8"))

        preparation = state.get("preparation_refs", {})
        coverage_payload = read_json_ref(preparation.get("coverage"))
        coverage_entry = (
            coverage_payload.get("entries", {}).get(module_id, {})
            if isinstance(coverage_payload, dict)
            else {}
        )
        scoped_coverage = dict(coverage_entry) if isinstance(coverage_entry, dict) else {}
        submodule_coverage = scoped_coverage.get("submodules", {})
        scoped_coverage["submodules"] = {
            submodule_id: submodule_coverage.get(submodule_id, {})
            for submodule_id in ordered
        }
        evidence_ids = {
            evidence_id
            for submodule_id in ordered
            for evidence_id in scoped_coverage["submodules"]
            .get(submodule_id, {})
            .get("evidence_ids", [])
        }

        evidence_items: list[dict[str, object]] = []
        evidence_path = workspace / preparation.get("evidence", "")
        if evidence_path.is_file():
            for line in evidence_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                if item.get("id") in evidence_ids:
                    evidence_items.append(item)
        else:
            candidates = [
                self._jsonable_context_item(item)
                for item in state.get("evidence_items", [])
            ]
            evidence_items = [
                item
                for item in candidates
                if not evidence_ids or item.get("id") in evidence_ids
            ]

        knowledge_ref = state.get("module_knowledge_refs", {}).get(module_id)
        knowledge_path = workspace / knowledge_ref if knowledge_ref else None
        knowledge_text = (
            knowledge_path.read_text(encoding="utf-8")
            if knowledge_path is not None and knowledge_path.is_file()
            else ""
        )
        shared_ref = self._shared_module_context_ref(
            state,
            module_id,
            purpose="collaboration",
        )
        packet_body = {
            "kind": "leaf_context_delta",
            "version": 2,
            "purpose": purpose,
            "run_id": state["run_id"],
            "shared_context_ref": shared_ref,
            "module": {
                "id": module_id,
                "title": REPORT_TAXONOMY[module_id].title,
                "target_leaves": [
                    {
                        "id": submodule_id,
                        "title": REPORT_TAXONOMY[module_id]
                        .submodules[submodule_id]
                        .title,
                    }
                    for submodule_id in ordered
                ],
            },
            "coverage": scoped_coverage,
            "evidence_items": evidence_items,
            "leaf_knowledge": {
                submodule_id: self._leaf_knowledge_excerpt(
                    knowledge_text, submodule_id
                )
                for submodule_id in ordered
            },
            "source_refs": {
                "coverage": preparation.get("coverage"),
                "evidence": preparation.get("evidence"),
                "manifest": preparation.get("manifest"),
                "knowledge": knowledge_ref,
                "shared_context": shared_ref,
            },
        }
        canonical = json.dumps(
            packet_body,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        if len(canonical) > 90_000:
            raise AgentWorkflowError(
                f"leaf collaboration context for module {module_id} exceeds 90000 chars; "
                "refine deterministic evidence scoping instead of starting an unbounded "
                "search conversation"
            )
        digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        packet_body["content_sha256"] = digest
        path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/context/collaboration/"
                f"{purpose}-module-{module_id}-{digest[:12]}.json"
            ),
            packet_body,
        )
        ref = path.relative_to(workspace).as_posix()
        inline = (
            f'<leaf_context_delta ref="{ref}" shared_ref="{shared_ref}" sha256="{digest}">\n'
            + canonical
            + "\n</leaf_context_delta>"
        )
        return ref, inline, shared_ref

    async def _submodule_discovery(
        self,
        submodule_id: str,
        state: dict,
        workflow_id: str,
        *,
        allow_cross_module_interfaces: bool = True,
    ) -> SubmoduleDiscoverySubmission:
        """Run Wave 1A for one exact fixed leaf submodule."""

        module_id = resolve_submodule(submodule_id).module_id
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        context_ref, context_inline, shared_ref = self._leaf_collaboration_context_packet(
            (submodule_id,),
            state,
            purpose=("wave-1a" if allow_cross_module_interfaces else "module-local"),
        )
        knowledge_ref = state["module_knowledge_refs"][module_id]
        manifest_ref = state.get("preparation_refs", {}).get("manifest")
        shared_input_refs = [shared_ref, knowledge_ref]
        if manifest_ref:
            shared_input_refs.append(manifest_ref)
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": (
                        f"submodule-discovery-{submodule_id}"
                        if allow_cross_module_interfaces
                        else f"submodule-discovery-local-{submodule_id}"
                    ),
                    "objective": (
                        f"独立完成固定子模块 {submodule_id} 的 Wave 1A 证据发现；"
                        + (
                            "只提交该叶子范围的事实、缺口、初步发现和精确跨模块依赖。"
                            if allow_cross_module_interfaces
                            else "只提交该叶子范围的事实、缺口和初步发现；本路径不激活跨模块接口。"
                        )
                    ),
                    "input_refs": [context_ref, *shared_input_refs],
                    "constraints": [
                        f"唯一工作范围是叶子子模块 {submodule_id}",
                        "不得用模块级摘要替代本子模块发现，也不得写其他子模块正文",
                        "保留全部相关 E-*、证据缺口和初步发现；本阶段不形成 reviewer verdict",
                        *(
                            [
                                "跨模块依赖必须指向一个固定 target_submodule_id",
                                "不得发明 request_id；运行时 reducer 将为 request/conflict 信号分配稳定 id",
                            ]
                            if allow_cross_module_interfaces
                            else [
                                "module_report 不激活跨模块协作；interface_signals 必须为空",
                            ]
                        ),
                        "不得实时 query_peer；只提交类型化 interface_signals",
                        "当前叶子的证据与 Knowledge 增量已内联；只有准备提交非空 interface_signals 时，才先用 open_artifact 读取 shared_context_ref 的 peer_target_taxonomy 并选择合法目标叶子",
                        "没有跨模块接口时不要打开整模块共享资料；仅在确需补充当前叶子增量、计算或记录缺口时使用辅助工具",
                        *self._evidence_policy_constraints(
                            state["request"].missing_evidence_policy
                        ),
                        *state["request"].execution_requirements,
                    ],
                    "allowed_outputs": ["submodule_discovery_submission"],
                    "allowed_tools": [
                        "open_artifact",
                        "search_text",
                        "calculate",
                        "report_gap",
                        "report_blocked",
                        "submit_result",
                    ],
                    "target_submodule_ids": [submodule_id],
                    "revision": 0,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        "<submodule_wave_1a>只完成一个固定叶子子模块的独立发现；"
                        "不得压缩、代写或推断其他子模块。"
                        + (
                            "</submodule_wave_1a>"
                            if allow_cross_module_interfaces
                            else "本任务不请求、不回答跨模块接口。</submodule_wave_1a>"
                        )
                        + "\n"
                        + context_inline
                    ),
                    "input_contract_kind": None,
                    "input_contract_ref": None,
                    "artifact_delivery_modes": {
                        context_ref: "hash_retained",
                        **{ref: "reference" for ref in shared_input_refs},
                    },
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"submodule-{submodule_id}",
        )
        if (
            not isinstance(payload, SubmoduleDiscoverySubmission)
            or payload.module_id != module_id
            or payload.submodule_id != submodule_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 1A submodule discovery"
            )
        if not allow_cross_module_interfaces and payload.interface_signals:
            raise AgentWorkflowError(
                f"module-local leaf discovery activated a cross-module interface: {submodule_id}"
            )
        return payload

    async def _legacy_submodule_discovery_batch(
        self,
        submodule_ids: tuple[str, ...],
        state: dict,
        workflow_id: str,
        *,
        allow_cross_module_interfaces: bool = True,
    ) -> dict[str, SubmoduleDiscoverySubmission]:
        """Legacy compatibility helper; active workflows never batch leaf calls."""

        ordered = tuple(dict.fromkeys(submodule_ids))
        if not ordered:
            return {}
        module_ids = {resolve_submodule(item).module_id for item in ordered}
        if len(module_ids) != 1:
            raise AgentWorkflowError("one discovery microbatch may not cross module ownership")
        module_id = next(iter(module_ids))
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        context_ref, context_inline, shared_ref = self._leaf_collaboration_context_packet(
            ordered,
            state,
            purpose=("wave-1a" if allow_cross_module_interfaces else "module-local"),
        )
        knowledge_ref = state["module_knowledge_refs"][module_id]
        manifest_ref = state.get("preparation_refs", {}).get("manifest")
        shared_input_refs = [shared_ref, knowledge_ref]
        if manifest_ref:
            shared_input_refs.append(manifest_ref)
        batch_label = "-".join(ordered)
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": (
                        f"submodule-discovery-batch-{batch_label}"
                        if allow_cross_module_interfaces
                        else f"submodule-discovery-local-batch-{batch_label}"
                    ),
                    "objective": (
                        f"在一次模块 {module_id} 共享上下文会话中，分别完成 "
                        f"{', '.join(ordered)} 的 Wave 1A 发现；每个叶子必须独立提交。"
                    ),
                    "input_refs": [context_ref, *shared_input_refs],
                    "constraints": [
                        f"本批次仅包含固定叶子 {', '.join(ordered)}",
                        "discoveries 必须对每个 target_submodule_id 恰好返回一次，禁止遗漏、重复或越界",
                        "各叶子的 summary、E-*、缺口、初步发现和接口信号必须分别保留，不得用一个模块摘要复制替代",
                        *(
                            [
                                "跨模块依赖必须指向固定 target_submodule_id",
                                "不得发明 request_id；运行时 reducer 为 request/conflict 信号分配稳定 id",
                            ]
                            if allow_cross_module_interfaces
                            else ["module_report 不激活跨模块协作；所有 interface_signals 必须为空"]
                        ),
                        "共享检索结果可在本批次复用，但 evidence_ids 必须按叶子实际适用范围声明",
                        "各叶子的证据与 Knowledge 增量已内联；只有准备提交非空 interface_signals 时，才先用 open_artifact 读取 shared_context_ref 的 peer_target_taxonomy 并选择合法目标叶子",
                        "没有跨模块接口时不要打开整模块共享资料；仅在确需补充叶子增量、计算或记录缺口时使用辅助工具",
                        *self._evidence_policy_constraints(
                            state["request"].missing_evidence_policy
                        ),
                        *state["request"].execution_requirements,
                    ],
                    "allowed_outputs": ["submodule_discovery_batch_submission"],
                    "allowed_tools": [
                        "open_artifact",
                        "search_text",
                        "calculate",
                        "report_gap",
                        "report_blocked",
                        "submit_result",
                    ],
                    "target_submodule_ids": list(ordered),
                    "revision": 0,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        "<submodule_wave_1a_batch>共享模块级不变输入；输出仍是可单独验收和恢复的叶子结果。"
                        "</submodule_wave_1a_batch>\n"
                        + context_inline
                    ),
                    "input_contract_kind": None,
                    "input_contract_ref": None,
                    "artifact_delivery_modes": {
                        context_ref: "hash_retained",
                        **{ref: "reference" for ref in shared_input_refs},
                    },
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"specialist-{module_id}",
        )
        if (
            not isinstance(payload, SubmoduleDiscoveryBatchSubmission)
            or payload.module_id != module_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 1A discovery batch"
            )
        discoveries = {item.submodule_id: item for item in payload.discoveries}
        if set(discoveries) != set(ordered):
            raise AgentWorkflowError(
                "Wave 1A discovery batch did not return the exact requested leaves; "
                f"missing={sorted(set(ordered) - set(discoveries))}; "
                f"extra={sorted(set(discoveries) - set(ordered))}"
            )
        if not allow_cross_module_interfaces:
            activated = sorted(
                item.submodule_id for item in payload.discoveries if item.interface_signals
            )
            if activated:
                raise AgentWorkflowError(
                    "module-local discovery batch activated cross-module interfaces: "
                    + ", ".join(activated)
                )
        return discoveries

    async def _legacy_run_batched_submodule_discovery_stage(
        self,
        submodule_ids: tuple[str, ...],
        *,
        state: dict,
        workflow_id: str,
        task_kind: str,
        wave: str,
        artifact_refs: dict[str, str],
        context_sha256: dict[str, str],
        allow_cross_module_interfaces: bool,
    ) -> dict[str, SubmoduleDiscoverySubmission]:
        """Legacy compatibility helper; active workflows use the leaf scheduler."""

        ordered = tuple(dict.fromkeys(submodule_ids))
        batch_size = int(getattr(state["request"], "submodule_batch_size", 14))
        by_module: dict[str, list[str]] = {}
        for submodule_id in ordered:
            by_module.setdefault(resolve_submodule(submodule_id).module_id, []).append(
                submodule_id
            )
        batches = [
            tuple(leaves[index : index + batch_size])
            for module_id in MODULE_IDS
            for leaves in [by_module.get(module_id, [])]
            for index in range(0, len(leaves), batch_size)
        ]
        results: dict[str, SubmoduleDiscoverySubmission] = {}
        result_lock = asyncio.Lock()

        async def run_module(module_id: str, leaves: list[str]) -> None:
            for index in range(0, len(leaves), batch_size):
                batch = tuple(leaves[index : index + batch_size])
                payloads = await self._legacy_submodule_discovery_batch(
                    batch,
                    state,
                    workflow_id,
                    allow_cross_module_interfaces=allow_cross_module_interfaces,
                )
                for submodule_id, payload in payloads.items():
                    self._persist_submodule_task_completion(
                        state=state,
                        task_kind=task_kind,
                        wave=wave,
                        submodule_id=submodule_id,
                        artifact_ref=artifact_refs[submodule_id],
                        context_sha256=context_sha256[submodule_id],
                        payload=payload,
                    )
                async with result_lock:
                    results.update(payloads)

        outcomes = await asyncio.gather(
            *(run_module(module_id, leaves) for module_id, leaves in by_module.items()),
            return_exceptions=True,
        )
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/scheduling/{task_kind.replace('_', '-')}.json",
            {
                "kind": "submodule_microbatch_scheduling_decisions",
                "version": 1,
                "run_id": state["run_id"],
                "stage_id": task_kind.replace("_", "-"),
                "policy": "module_shared_context_microbatch_v1",
                "logical_task_count": len(ordered),
                "agent_dispatch_count": len(batches),
                "avoided_independent_agent_dispatches": len(ordered) - len(batches),
                "provider_attempt_count_source": "UsageLedger",
                "batch_size_limit": batch_size,
                "batches": [list(batch) for batch in batches],
            },
        )
        failures = [item for item in outcomes if isinstance(item, BaseException)]
        if failures:
            raise failures[0]
        if set(results) != set(ordered):
            raise AgentWorkflowError(
                f"{task_kind} ended without exact leaf completion; "
                f"missing={sorted(set(ordered) - set(results))}"
            )
        return results

    async def _submodule_interface_response(
        self,
        submodule_id: str,
        *,
        inbox_ref: str,
        discovery_ref: str,
        state: dict,
        workflow_id: str,
    ) -> SubmoduleInterfaceResponseSubmission:
        """Answer one exact sparse Wave 2 leaf inbox."""

        module_id = resolve_submodule(submodule_id).module_id
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        inbox_text = (self.service.workspace / inbox_ref).read_text(encoding="utf-8")
        knowledge_ref = state["module_knowledge_refs"][module_id]
        knowledge_path = self.service.workspace / knowledge_ref
        knowledge_text = (
            knowledge_path.read_text(encoding="utf-8")
            if knowledge_path.is_file()
            else ""
        )
        shared_ref = self._shared_module_context_ref(
            state,
            module_id,
            purpose="collaboration",
        )
        evidence_ref = state["preparation_refs"]["evidence"]
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"submodule-interface-response-{submodule_id}",
                    "objective": (
                        f"回答目标为叶子子模块 {submodule_id} 的全部 Wave 2 请求；"
                        "无法回答时逐项提交明确 unresolved 边界。"
                    ),
                    "input_refs": [
                        inbox_ref,
                        discovery_ref,
                        evidence_ref,
                        knowledge_ref,
                        shared_ref,
                    ],
                    "constraints": [
                        f"只回答 target_submodule_id={submodule_id} 的 inbox",
                        "每个 request_id 必须恰好一个 answered 或 unresolved disposition",
                        "不得回答其他叶子子模块的问题，也不得发明 request_id",
                        "answered 必须包含适用条件；证据不足时明确 unresolved_reason 和 boundary",
                        "不得实时 query_peer",
                        "inbox 与回答所需上下文已完整注入，应优先直接提交；仅在确需计算或记录缺口时使用辅助工具",
                    ],
                    "allowed_outputs": [
                        "submodule_interface_response_submission"
                    ],
                    "allowed_tools": [
                        "search_project_evidence",
                        "open_project_source",
                        "open_artifact",
                        "search_text",
                        "calculate",
                        "report_gap",
                        "report_blocked",
                        "submit_result",
                    ],
                    "target_submodule_ids": [submodule_id],
                    "revision": 0,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        "<submodule_interface_inbox>\n"
                        + inbox_text
                        + "\n</submodule_interface_inbox>\n"
                        + f'<leaf_knowledge_delta submodule_id="{submodule_id}" '
                        + f'provenance_ref="{knowledge_ref}" '
                        + 'project_fact_authority="false">\n'
                        + self._leaf_knowledge_excerpt(knowledge_text, submodule_id)
                        + "\n</leaf_knowledge_delta>"
                    ),
                    "input_contract_kind": None,
                    "input_contract_ref": None,
                    "artifact_delivery_modes": {
                        inbox_ref: "hash_retained",
                        discovery_ref: "hash_retained",
                        evidence_ref: "reference",
                        knowledge_ref: "reference",
                        shared_ref: "reference",
                    },
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"submodule-{submodule_id}",
        )
        if (
            not isinstance(payload, SubmoduleInterfaceResponseSubmission)
            or payload.module_id != module_id
            or payload.submodule_id != submodule_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 2 submodule response"
            )
        return payload

    async def _module_discovery(
        self,
        module_id: str,
        state: dict,
        workflow_id: str,
    ) -> ModuleDiscoverySubmission:
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        preparation = state["preparation_refs"]
        knowledge_ref = state["module_knowledge_refs"][module_id]
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"module-discovery-{module_id}",
                    "objective": (
                        f"完成模块 {module_id} 的 Wave 1 证据与接口发现；"
                        "此阶段不撰写最终报告正文。"
                    ),
                    "input_refs": [
                        preparation["coverage"],
                        preparation["evidence"],
                        preparation["manifest"],
                        knowledge_ref,
                    ],
                    "constraints": [
                        f"只研究固定责任模块 {module_id}，不得提前撰写最终报告正文",
                        "必须对其余四个固定模块各提交一条 interface_coverage，且不得遗漏或重复",
                        "只有 request 或 conflict 状态可创建 InterfaceRequest",
                        (
                            "若请求包含 requester_submodule_id 和 target_submodule_id，"
                            "request_id 必须采用 IF-<requester_submodule_id>-"
                            "<target_submodule_id>-NNN；只有不含叶子身份的历史兼容请求"
                            f"才可采用 IF-{module_id}-<目标模块>-NNN。不得通过删除叶子"
                            "字段绕过已声明的叶子身份，且 id 在当前 discovery 内唯一"
                        ),
                        "所有 evidence_ids 只能使用当前 run 已注册的 E-*",
                        "不得实时 query_peer；问题只通过类型化 InterfaceRequest 进入 Barrier 1",
                        *self._evidence_policy_constraints(
                            state["request"].missing_evidence_policy
                        ),
                        *state["request"].execution_requirements,
                    ],
                    "allowed_outputs": ["module_discovery_submission"],
                    "allowed_tools": [
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
                        "report_blocked",
                        "submit_result",
                    ],
                    "target_submodule_ids": list(
                        REPORT_TAXONOMY[module_id].submodules
                    ),
                    "revision": 0,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        "<collaboration_wave>\n"
                        "Wave 1 只形成简洁 discovery、四模块接口覆盖和必要请求。"
                        "模块 Knowledge 以引用方式提供，仅在确有需要时打开；"
                        "不要输出最终小节正文或调用长正文写入工具。\n"
                        "</collaboration_wave>"
                    ),
                    "input_contract_kind": None,
                    "input_contract_ref": None,
                    "artifact_delivery_modes": {},
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"specialist-{module_id}",
        )
        if (
            not isinstance(payload, ModuleDiscoverySubmission)
            or payload.module_id != module_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 1 discovery"
            )
        return payload

    async def _legacy_module_interface_response(
        self,
        module_id: str,
        *,
        inbox_ref: str,
        discovery_ref: str,
        state: dict,
        workflow_id: str,
        target_submodule_ids: tuple[str, ...] | None = None,
    ) -> ModuleInterfaceResponseSubmission:
        """Legacy module-wide response helper; active Wave 2 is leaf-scoped."""

        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        inbox_text = (
            self.service.workspace / inbox_ref
        ).read_text(encoding="utf-8")
        discovery_text = (
            self.service.workspace / discovery_ref
        ).read_text(encoding="utf-8")
        target_leaves = target_submodule_ids or tuple(
            REPORT_TAXONOMY[module_id].submodules
        )
        context_ref, context_inline, shared_ref = self._leaf_collaboration_context_packet(
            target_leaves,
            state,
            purpose="wave-2",
        )
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"module-interface-response-{module_id}",
                    "objective": (
                        f"批量回答发送到模块 {module_id} 的 Wave 2 接口请求；"
                        "无法回答时提交明确 unresolved 边界。"
                    ),
                    "input_refs": [
                        context_ref,
                        shared_ref,
                        inbox_ref,
                        discovery_ref,
                    ],
                    "constraints": [
                        f"只回答 inbox 中目标为 {module_id} 的 request_id",
                        "每个 inbox 请求必须恰好提交一个 answered 或 unresolved disposition",
                        "不得发明 request_id，也不得回答发给其他模块的问题",
                        "answered 必须给出适用条件；证据不足时用 unresolved_reason 和 boundary 明确边界",
                        "所有 evidence_ids 只能使用当前 run 已注册的 E-*",
                        "不得实时 query_peer；本轮只批量提交接口响应",
                        "inbox 与回答所需上下文已完整注入，应优先直接提交；仅在确需计算或记录缺口时使用辅助工具",
                    ],
                    "allowed_outputs": [
                        "module_interface_response_submission"
                    ],
                    "allowed_tools": [
                        "calculate",
                        "report_gap",
                        "report_blocked",
                        "submit_result",
                    ],
                    "target_submodule_ids": list(target_leaves),
                    "revision": 0,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        "<module_interface_inbox>\n"
                        + inbox_text
                        + "\n</module_interface_inbox>\n"
                        + "<module_discovery>\n"
                        + discovery_text
                        + "\n</module_discovery>\n"
                        + context_inline
                    ),
                    "input_contract_kind": None,
                    "input_contract_ref": None,
                    "artifact_delivery_modes": {
                        context_ref: "hash_retained",
                        shared_ref: "reference",
                        inbox_ref: "hash_retained",
                        discovery_ref: "hash_retained",
                    },
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"specialist-{module_id}",
        )
        if (
            not isinstance(payload, ModuleInterfaceResponseSubmission)
            or payload.module_id != module_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 2 response"
            )
        return payload

    async def _legacy_run_batched_submodule_interface_response_stage(
        self,
        pending_submodule_ids: tuple[str, ...],
        *,
        inboxes: dict[str, tuple],
        discovery_refs: dict[str, str],
        module_discovery_refs: dict[str, str],
        response_refs: dict[str, str],
        response_contexts: dict[str, str],
        state: dict,
        workflow_id: str,
    ) -> dict[str, SubmoduleInterfaceResponseSubmission]:
        """Legacy compatibility helper; active Wave 2 dispatches each leaf inbox."""

        pending = tuple(dict.fromkeys(pending_submodule_ids))
        by_module: dict[str, list[str]] = {}
        for submodule_id in pending:
            by_module.setdefault(resolve_submodule(submodule_id).module_id, []).append(
                submodule_id
            )
        results: dict[str, SubmoduleInterfaceResponseSubmission] = {}

        async def run_module(
            module_id: str, leaves: list[str]
        ) -> dict[str, SubmoduleInterfaceResponseSubmission]:
            requests = [request for leaf in leaves for request in inboxes[leaf]]
            inbox_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/collaboration/inboxes/module-{module_id}.json",
                {
                    "kind": "module_interface_inbox",
                    "version": 2,
                    "run_id": state["run_id"],
                    "module_id": module_id,
                    "target_submodule_ids": leaves,
                    "requests": [
                        request.model_dump(mode="json") for request in requests
                    ],
                },
            )
            inbox_ref = inbox_path.relative_to(self.service.workspace).as_posix()
            response = await self._legacy_module_interface_response(
                module_id,
                inbox_ref=inbox_ref,
                discovery_ref=module_discovery_refs[module_id],
                state=state,
                workflow_id=workflow_id,
                target_submodule_ids=tuple(leaves),
            )
            expected_ids = {request.request_id for request in requests}
            actual_ids = {item.request_id for item in response.dispositions}
            if actual_ids != expected_ids:
                raise AgentWorkflowError(
                    f"Wave 2 module batch {module_id} violated exact inbox; "
                    f"missing={sorted(expected_ids - actual_ids)}; "
                    f"extra={sorted(actual_ids - expected_ids)}"
                )
            target_by_request = {
                request.request_id: request.target_submodule_id for request in requests
            }
            scoped: dict[str, SubmoduleInterfaceResponseSubmission] = {}
            for submodule_id in leaves:
                payload = SubmoduleInterfaceResponseSubmission(
                    module_id=module_id,
                    submodule_id=submodule_id,
                    dispositions=[
                        item.model_copy(deep=True)
                        for item in response.dispositions
                        if target_by_request[item.request_id] == submodule_id
                    ],
                )
                self._persist_submodule_task_completion(
                    state=state,
                    task_kind="submodule_interface_response",
                    wave="wave-2",
                    submodule_id=submodule_id,
                    artifact_ref=response_refs[submodule_id],
                    context_sha256=response_contexts[submodule_id],
                    payload=payload,
                )
                scoped[submodule_id] = payload
            return scoped

        outcomes = await asyncio.gather(
            *(run_module(module_id, leaves) for module_id, leaves in by_module.items()),
            return_exceptions=True,
        )
        failures: list[BaseException] = []
        for outcome in outcomes:
            if isinstance(outcome, BaseException):
                failures.append(outcome)
            else:
                results.update(outcome)
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/scheduling/submodule-interface-response.json",
            {
                "kind": "submodule_microbatch_scheduling_decisions",
                "version": 1,
                "run_id": state["run_id"],
                "stage_id": "submodule-interface-response",
                "policy": "module_shared_inbox_batch_v1",
                "logical_task_count": len(pending),
                "agent_dispatch_count": len(by_module),
                "avoided_independent_agent_dispatches": len(pending) - len(by_module),
                "provider_attempt_count_source": "UsageLedger",
                "batches": [
                    leaves for _module_id, leaves in sorted(by_module.items())
                ],
            },
        )
        if failures:
            raise failures[0]
        return results

    @staticmethod
    def _invalid_discovery_modules(
        discoveries: dict[str, ModuleDiscoverySubmission],
        known_evidence_ids: set[str],
    ) -> set[str]:
        """Locate Barrier 1 candidates that can be retried independently."""

        invalid: set[str] = set()
        request_owners: dict[str, set[str]] = {}
        for module_id, discovery in discoveries.items():
            declared = {
                *discovery.evidence_ids,
                *(
                    evidence_id
                    for request in discovery.requests
                    for evidence_id in request.evidence_ids
                ),
            }
            if declared - known_evidence_ids:
                invalid.add(module_id)
            for request in discovery.requests:
                request_owners.setdefault(
                    request.request_id, set()
                ).add(module_id)
        for owners in request_owners.values():
            if len(owners) > 1:
                invalid.update(owners)
        return invalid

    @staticmethod
    def _invalid_response_modules(
        responses: dict[str, ModuleInterfaceResponseSubmission],
        inboxes: dict[str, tuple],
        known_evidence_ids: set[str],
    ) -> set[str]:
        """Locate Wave 2 responders whose exact inbox contract was violated."""

        invalid: set[str] = set()
        for module_id, response in responses.items():
            expected_ids = {
                request.request_id
                for request in inboxes.get(module_id, ())
            }
            actual_ids = {
                disposition.request_id
                for disposition in response.dispositions
            }
            response_evidence_ids = {
                evidence_id
                for disposition in response.dispositions
                for evidence_id in disposition.evidence_ids
            }
            if (
                actual_ids != expected_ids
                or response_evidence_ids - known_evidence_ids
            ):
                invalid.add(module_id)
        return invalid

    async def _module_local_submodule_preparation(
        self,
        module_ids: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Prepare independent leaf authoring without activating peer workflows."""

        run_id = state["run_id"]
        submodule_ids = tuple(
            submodule_id
            for module_id in module_ids
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        )
        discovery_refs = {
            submodule_id: (
                f"Work/runs/{run_id}/collaboration/wave-1/submodules/"
                f"{submodule_id}.json"
            )
            for submodule_id in submodule_ids
        }
        discovery_contexts = {
            submodule_id: self._submodule_task_context_sha256(
                state,
                task_kind="submodule_discovery_local",
                submodule_id=submodule_id,
                input_refs=[
                    state["preparation_refs"]["coverage"],
                    state["preparation_refs"]["evidence"],
                    state["preparation_refs"]["manifest"],
                    state["module_knowledge_refs"][
                        resolve_submodule(submodule_id).module_id
                    ],
                ],
            )
            for submodule_id in submodule_ids
        }
        discoveries: dict[str, SubmoduleDiscoverySubmission] = {}
        pending: list[str] = []
        for submodule_id in submodule_ids:
            module_id = resolve_submodule(submodule_id).module_id
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=discovery_refs[submodule_id],
                task_id=f"submodule-discovery-local-{submodule_id}",
                module_id=module_id,
                submodule_id=submodule_id,
                expected_type=SubmoduleDiscoverySubmission,
                wave="module-local-discovery",
                expected_context_sha256=discovery_contexts[submodule_id],
            )
            if payload is None:
                pending.append(submodule_id)
            else:
                discoveries[submodule_id] = payload
        if pending:
            discoveries.update(
                await self._run_scheduled_submodule_stage(
                    tuple(pending),
                    run_id=run_id,
                    workflow_id=workflow_id,
                    task_kind="submodule_discovery_local",
                    concurrency=state["request"].submodule_task_concurrency,
                    execute=lambda submodule_id: self._submodule_discovery(
                        submodule_id,
                        state,
                        workflow_id,
                        allow_cross_module_interfaces=False,
                    ),
                    persist=lambda submodule_id, payload: (
                        self._persist_submodule_task_completion(
                            state=state,
                            task_kind="submodule_discovery_local",
                            wave="module-local-discovery",
                            submodule_id=submodule_id,
                            artifact_ref=discovery_refs[submodule_id],
                            context_sha256=discovery_contexts[submodule_id],
                            payload=payload,
                        )
                    ),
                )
            )

        known_evidence_ids = {
            item.id for item in state.get("evidence_items", [])
        } | {
            item.id
            for item in SourceLedger(self.service.workspace, run_id).records
            if item.id.startswith("E-")
        }
        for submodule_id, discovery in discoveries.items():
            unknown = sorted(set(discovery.evidence_ids) - known_evidence_ids)
            if unknown:
                raise AgentWorkflowError(
                    f"module-local discovery {submodule_id} uses unknown evidence: {unknown}"
                )
            if discovery.interface_signals:
                raise AgentWorkflowError(
                    "module_report leaf discovery must not activate cross-module interfaces: "
                    f"{submodule_id}"
                )
            self.service.store.write_json(
                discovery_refs[submodule_id],
                discovery.model_dump(mode="json"),
            )

        module_barrier_refs: dict[str, str] = {}
        for module_id in module_ids:
            scoped_refs = {
                submodule_id: discovery_refs[submodule_id]
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            }
            barrier = ModuleSubmoduleDiscoveryBarrier(
                run_id=run_id,
                module_id=module_id,
                discovery_refs=scoped_refs,
                discovery_sha256={
                    submodule_id: self._sha256(
                        self.service.workspace / scoped_refs[submodule_id]
                    )
                    for submodule_id in scoped_refs
                },
            )
            path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/wave-1/module-barriers/"
                    f"module-{module_id}.json"
                ),
                barrier.model_dump(mode="json"),
            )
            module_barrier_refs[module_id] = path.relative_to(
                self.service.workspace
            ).as_posix()

        bundle_refs: dict[str, str] = {}
        for submodule_id in submodule_ids:
            discovery = discoveries[submodule_id]
            bundle = SubmoduleCollaborationBundle(
                module_id=discovery.module_id,
                submodule_id=submodule_id,
                discovery=discovery,
                requested_interfaces=[],
                responded_interfaces=[],
            )
            path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/bundles/submodules/"
                    f"{submodule_id}.json"
                ),
                bundle.model_dump(mode="json"),
            )
            bundle_refs[submodule_id] = path.relative_to(
                self.service.workspace
            ).as_posix()
        state["submodule_discovery_barrier_refs"] = module_barrier_refs
        state["submodule_collaboration_bundle_refs"] = bundle_refs
        module_bundle_refs: dict[str, str] = {}
        for module_id in module_ids:
            scoped = [
                discoveries[submodule_id]
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            ]
            module_bundle = ModuleCollaborationBundle(
                module_id=module_id,
                discovery_summary="\n\n".join(
                    f"[{item.submodule_id}] {item.discovery_summary}"
                    for item in scoped
                ),
                discovery_evidence_ids=sorted(
                    {
                        evidence_id
                        for item in scoped
                        for evidence_id in item.evidence_ids
                    }
                ),
                peer_coverage=[
                    ModuleInterfaceCoverage(
                        target_module_id=peer_id,
                        status="not_applicable",
                        rationale=(
                            "Partial module_report path intentionally does not activate "
                            "unrequested cross-module workflows."
                        ),
                    )
                    for peer_id in MODULE_IDS
                    if peer_id != module_id
                ],
            )
            module_bundle_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/bundles/"
                    f"module-{module_id}.json"
                ),
                module_bundle.model_dump(mode="json"),
            )
            module_bundle_refs[module_id] = module_bundle_path.relative_to(
                self.service.workspace
            ).as_posix()
        state["collaboration_bundle_refs"] = module_bundle_refs
        await self._checkpoint_then_cost_boundary(
            state,
            "module-local-leaf-preparation",
            "completed",
            "submodule-discovery",
            "submodule-authoring",
        )
        await self.service._notice(
            "指定模块的叶子发现已完成；未激活跨模块问答，正在恢复或调度各叶子写作。"
        )

    async def _module_collaboration(
        self,
        module_ids: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run leaf discovery, exact interface closure, and two global barriers."""

        if set(module_ids) != set(MODULE_IDS):
            raise AgentWorkflowError(
                "submodule collaboration requires the complete fixed module set"
            )
        run_id = state["run_id"]
        submodule_ids = tuple(
            submodule_id
            for module_id in MODULE_IDS
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        )
        known_evidence_ids = {
            item.id for item in state.get("evidence_items", [])
        } | {
            item.id
            for item in SourceLedger(self.service.workspace, run_id).records
            if item.id.startswith("E-")
        }
        discovery_refs = {
            submodule_id: (
                f"Work/runs/{run_id}/collaboration/wave-1/submodules/"
                f"{submodule_id}.json"
            )
            for submodule_id in submodule_ids
        }
        discovery_contexts = {
            submodule_id: self._submodule_task_context_sha256(
                state,
                task_kind="submodule_discovery",
                submodule_id=submodule_id,
                input_refs=[
                    state["preparation_refs"]["coverage"],
                    state["preparation_refs"]["evidence"],
                    state["preparation_refs"]["manifest"],
                    state["module_knowledge_refs"][
                        resolve_submodule(submodule_id).module_id
                    ],
                ],
            )
            for submodule_id in submodule_ids
        }
        discoveries: dict[str, SubmoduleDiscoverySubmission] = {}
        pending_discovery: list[str] = []
        for submodule_id in submodule_ids:
            module_id = resolve_submodule(submodule_id).module_id
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=discovery_refs[submodule_id],
                task_id=f"submodule-discovery-{submodule_id}",
                module_id=module_id,
                submodule_id=submodule_id,
                expected_type=SubmoduleDiscoverySubmission,
                wave="wave-1a",
                expected_context_sha256=discovery_contexts[submodule_id],
            )
            if payload is None:
                pending_discovery.append(submodule_id)
            else:
                discoveries[submodule_id] = payload
        if pending_discovery:
            discoveries.update(
                await self._run_scheduled_submodule_stage(
                    tuple(pending_discovery),
                    run_id=run_id,
                    workflow_id=workflow_id,
                    task_kind="submodule_discovery",
                    concurrency=state["request"].submodule_task_concurrency,
                    execute=lambda submodule_id: self._submodule_discovery(
                        submodule_id,
                        state,
                        workflow_id,
                        allow_cross_module_interfaces=True,
                    ),
                    persist=lambda submodule_id, payload: (
                        self._persist_submodule_task_completion(
                            state=state,
                            task_kind="submodule_discovery",
                            wave="wave-1a",
                            submodule_id=submodule_id,
                            artifact_ref=discovery_refs[submodule_id],
                            context_sha256=discovery_contexts[submodule_id],
                            payload=payload,
                        )
                    ),
                )
            )
        ordered_discoveries = [discoveries[item] for item in submodule_ids]
        try:
            module_discoveries = reduce_submodule_discoveries(
                ordered_discoveries,
                known_evidence_ids=known_evidence_ids,
            )
        except ValueError as exc:
            raise AgentWorkflowError(f"Wave 1A module reduction failed: {exc}") from exc

        for submodule_id, payload in discoveries.items():
            self.service.store.write_json(
                discovery_refs[submodule_id],
                payload.model_dump(mode="json"),
            )
        module_barrier_refs: dict[str, str] = {}
        for module_id in MODULE_IDS:
            scoped_refs = {
                submodule_id: discovery_refs[submodule_id]
                for submodule_id in REPORT_TAXONOMY[module_id].submodules
            }
            barrier = ModuleSubmoduleDiscoveryBarrier(
                run_id=run_id,
                module_id=module_id,
                discovery_refs=scoped_refs,
                discovery_sha256={
                    submodule_id: self._sha256(
                        self.service.workspace / scoped_refs[submodule_id]
                    )
                    for submodule_id in scoped_refs
                },
            )
            barrier_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/wave-1/module-barriers/"
                    f"module-{module_id}.json"
                ),
                barrier.model_dump(mode="json"),
            )
            module_barrier_refs[module_id] = barrier_path.relative_to(
                self.service.workspace
            ).as_posix()
        state["submodule_discovery_barrier_refs"] = module_barrier_refs

        module_discovery_refs: dict[str, str] = {}
        for module_id, discovery in module_discoveries.items():
            path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/wave-1/"
                    f"module-{module_id}.json"
                ),
                discovery.model_dump(mode="json"),
            )
            module_discovery_refs[module_id] = path.relative_to(
                self.service.workspace
            ).as_posix()
        ordered_module_discoveries = [
            module_discoveries[module_id] for module_id in MODULE_IDS
        ]
        try:
            inboxes = build_submodule_interface_inboxes(
                ordered_module_discoveries,
                known_evidence_ids=known_evidence_ids,
            )
        except ValueError as exc:
            raise AgentWorkflowError(f"Barrier 1 failed: {exc}") from exc
        inbox_refs: dict[str, str] = {}
        for submodule_id, requests in inboxes.items():
            module_id = resolve_submodule(submodule_id).module_id
            inbox_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/inboxes/"
                    f"submodule-{submodule_id}.json"
                ),
                {
                    "kind": "submodule_interface_inbox",
                    "run_id": run_id,
                    "module_id": module_id,
                    "submodule_id": submodule_id,
                    "requests": [
                        request.model_dump(mode="json") for request in requests
                    ],
                },
            )
            inbox_refs[submodule_id] = inbox_path.relative_to(
                self.service.workspace
            ).as_posix()
        barrier1_path = self.service.store.write_json(
            f"Work/runs/{run_id}/collaboration/barrier-1.json",
            {
                "kind": "submodule_collaboration_barrier_1",
                "version": 2,
                "run_id": run_id,
                "module_ids": list(MODULE_IDS),
                "submodule_ids": list(submodule_ids),
                "module_discovery_barrier_refs": module_barrier_refs,
                "submodule_discovery_refs": discovery_refs,
                "module_discovery_refs": module_discovery_refs,
                "inbox_refs": inbox_refs,
                "request_index": [
                    {
                        "request_id": request.request_id,
                        "requester_module_id": request.requester_module_id,
                        "requester_submodule_id": request.requester_submodule_id,
                        "target_module_id": request.target_module_id,
                        "target_submodule_id": request.target_submodule_id,
                    }
                    for discovery in ordered_module_discoveries
                    for request in discovery.requests
                ],
            },
        )
        state["collaboration_barrier1_ref"] = barrier1_path.relative_to(
            self.service.workspace
        ).as_posix()
        await self._checkpoint_then_cost_boundary(
            state,
            "collaboration-barrier-1",
            "completed",
            "submodule-discovery",
            "submodule-interface-response",
        )

        response_refs = {
            submodule_id: (
                f"Work/runs/{run_id}/collaboration/wave-2/submodules/"
                f"{submodule_id}.json"
            )
            for submodule_id in inboxes
        }
        response_contexts = {
            submodule_id: self._submodule_task_context_sha256(
                state,
                task_kind="submodule_interface_response",
                submodule_id=submodule_id,
                input_refs=[
                    inbox_refs[submodule_id],
                    discovery_refs[submodule_id],
                    state["preparation_refs"]["evidence"],
                    state["module_knowledge_refs"][
                        resolve_submodule(submodule_id).module_id
                    ],
                ],
            )
            for submodule_id in inboxes
        }
        responses: dict[str, SubmoduleInterfaceResponseSubmission] = {}
        pending_responses: list[str] = []
        for submodule_id in inboxes:
            module_id = resolve_submodule(submodule_id).module_id
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=response_refs[submodule_id],
                task_id=f"submodule-interface-response-{submodule_id}",
                module_id=module_id,
                submodule_id=submodule_id,
                expected_type=SubmoduleInterfaceResponseSubmission,
                wave="wave-2",
                expected_context_sha256=response_contexts[submodule_id],
            )
            if payload is None:
                pending_responses.append(submodule_id)
            else:
                responses[submodule_id] = payload
        if pending_responses:
            responses.update(
                await self._run_scheduled_submodule_stage(
                    tuple(pending_responses),
                    run_id=run_id,
                    workflow_id=workflow_id,
                    task_kind="submodule_interface_response",
                    concurrency=state["request"].submodule_task_concurrency,
                    execute=lambda submodule_id: self._submodule_interface_response(
                        submodule_id,
                        inbox_ref=inbox_refs[submodule_id],
                        discovery_ref=discovery_refs[submodule_id],
                        state=state,
                        workflow_id=workflow_id,
                    ),
                    persist=lambda submodule_id, payload: (
                        self._persist_submodule_task_completion(
                            state=state,
                            task_kind="submodule_interface_response",
                            wave="wave-2",
                            submodule_id=submodule_id,
                            artifact_ref=response_refs[submodule_id],
                            context_sha256=response_contexts[submodule_id],
                            payload=payload,
                        )
                    ),
                )
            )
        ordered_responses = [responses[item] for item in inboxes]
        try:
            submodule_bundles = build_submodule_collaboration_bundles(
                ordered_discoveries,
                ordered_module_discoveries,
                ordered_responses,
                known_evidence_ids=known_evidence_ids,
            )
            grouped_responses = [
                ModuleInterfaceResponseSubmission(
                    module_id=module_id,
                    dispositions=[
                        disposition.model_copy(deep=True)
                        for response in ordered_responses
                        if response.module_id == module_id
                        for disposition in response.dispositions
                    ],
                )
                for module_id in MODULE_IDS
                if any(response.module_id == module_id for response in ordered_responses)
            ]
            module_bundles = build_collaboration_bundles(
                ordered_module_discoveries,
                grouped_responses,
                known_evidence_ids=known_evidence_ids,
            )
        except ValueError as exc:
            raise AgentWorkflowError(f"Barrier 2 failed: {exc}") from exc
        for submodule_id, payload in responses.items():
            self.service.store.write_json(
                response_refs[submodule_id],
                payload.model_dump(mode="json"),
            )
        submodule_bundle_refs: dict[str, str] = {}
        for submodule_id, bundle in submodule_bundles.items():
            path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/bundles/submodules/"
                    f"{submodule_id}.json"
                ),
                bundle.model_dump(mode="json"),
            )
            submodule_bundle_refs[submodule_id] = path.relative_to(
                self.service.workspace
            ).as_posix()
        module_bundle_refs: dict[str, str] = {}
        for module_id, bundle in module_bundles.items():
            path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/bundles/"
                    f"module-{module_id}.json"
                ),
                bundle.model_dump(mode="json"),
            )
            module_bundle_refs[module_id] = path.relative_to(
                self.service.workspace
            ).as_posix()
        unresolved_request_ids = sorted(
            disposition.request_id
            for response in ordered_responses
            for disposition in response.dispositions
            if disposition.status == "unresolved"
        )
        barrier2_path = self.service.store.write_json(
            f"Work/runs/{run_id}/collaboration/barrier-2.json",
            {
                "kind": "submodule_collaboration_barrier_2",
                "version": 2,
                "run_id": run_id,
                "barrier_1_ref": state["collaboration_barrier1_ref"],
                "response_refs": response_refs,
                "submodule_bundle_refs": submodule_bundle_refs,
                "module_bundle_refs": module_bundle_refs,
                "request_count": sum(
                    len(discovery.requests)
                    for discovery in ordered_module_discoveries
                ),
                "unresolved_request_ids": unresolved_request_ids,
            },
        )
        state["collaboration_barrier2_ref"] = barrier2_path.relative_to(
            self.service.workspace
        ).as_posix()
        state["collaboration_bundle_refs"] = module_bundle_refs
        state["submodule_collaboration_bundle_refs"] = submodule_bundle_refs
        await self._checkpoint_then_cost_boundary(
            state,
            "collaboration-barrier-2",
            "completed",
            "submodule-interface-response",
            "submodule-authoring",
        )

    async def _module_collaboration_legacy(
        self,
        module_ids: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run sparse Wave 1/2 tasks and persist two deterministic barriers."""

        if set(module_ids) != set(MODULE_IDS):
            raise AgentWorkflowError(
                "three-wave collaboration requires the complete fixed module set"
            )
        run_id = state["run_id"]
        known_evidence_ids = {
            item.id for item in state.get("evidence_items", [])
        }
        discovery_refs = {
            module_id: (
                f"Work/runs/{run_id}/collaboration/wave-1/"
                f"module-{module_id}.json"
            )
            for module_id in MODULE_IDS
        }
        discoveries: dict[str, ModuleDiscoverySubmission] = {}
        pending_discovery: list[str] = []
        for module_id in MODULE_IDS:
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=discovery_refs[module_id],
                task_id=f"module-discovery-{module_id}",
                module_id=module_id,
                expected_type=ModuleDiscoverySubmission,
            )
            if payload is None:
                pending_discovery.append(module_id)
            else:
                discoveries[module_id] = payload

        if pending_discovery:
            results = await asyncio.gather(
                *(
                    self._module_discovery(
                        module_id,
                        state,
                        workflow_id,
                    )
                    for module_id in pending_discovery
                ),
                return_exceptions=True,
            )
            failures: list[BaseException] = []
            for module_id, result in zip(
                pending_discovery,
                results,
                strict=True,
            ):
                if isinstance(result, BaseException):
                    failures.append(result)
                    continue
                discoveries[module_id] = result
            if failures:
                raise failures[0]

        ordered_discoveries = [
            discoveries[module_id] for module_id in MODULE_IDS
        ]
        try:
            inboxes = build_interface_inboxes(
                ordered_discoveries,
                known_evidence_ids=known_evidence_ids,
            )
        except ValueError as exc:
            invalid_modules = self._invalid_discovery_modules(
                discoveries,
                known_evidence_ids,
            ) or set(MODULE_IDS)
            for module_id in set(MODULE_IDS) - invalid_modules:
                self.service.store.write_json(
                    discovery_refs[module_id],
                    discoveries[module_id].model_dump(mode="json"),
                )
            self._reject_collaboration_candidates(
                run_id=run_id,
                wave="wave-1",
                artifact_refs=[
                    discovery_refs[module_id]
                    for module_id in invalid_modules
                ],
                task_ids=[
                    f"module-discovery-{module_id}"
                    for module_id in invalid_modules
                ],
                reason=str(exc),
            )
            state.pop("collaboration_barrier1_ref", None)
            state.pop("collaboration_barrier2_ref", None)
            state.pop("collaboration_bundle_refs", None)
            raise AgentWorkflowError(f"Barrier 1 failed: {exc}") from exc
        for module_id, payload in discoveries.items():
            self.service.store.write_json(
                discovery_refs[module_id],
                payload.model_dump(mode="json"),
            )
        inbox_refs: dict[str, str] = {}
        for module_id, requests in inboxes.items():
            inbox_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/inboxes/"
                    f"module-{module_id}.json"
                ),
                {
                    "kind": "module_interface_inbox",
                    "run_id": run_id,
                    "module_id": module_id,
                    "requests": [
                        request.model_dump(mode="json")
                        for request in requests
                    ],
                },
            )
            inbox_refs[module_id] = inbox_path.relative_to(
                self.service.workspace
            ).as_posix()
        barrier1_path = self.service.store.write_json(
            f"Work/runs/{run_id}/collaboration/barrier-1.json",
            {
                "kind": "module_collaboration_barrier_1",
                "run_id": run_id,
                "module_ids": list(MODULE_IDS),
                "discovery_refs": discovery_refs,
                "inbox_refs": inbox_refs,
                "request_index": [
                    {
                        "request_id": request.request_id,
                        "requester_module_id": request.requester_module_id,
                        "target_module_id": request.target_module_id,
                        "discovery_ref": discovery_refs[
                            request.requester_module_id
                        ],
                    }
                    for discovery in ordered_discoveries
                    for request in discovery.requests
                ],
            },
        )
        state["collaboration_barrier1_ref"] = barrier1_path.relative_to(
            self.service.workspace
        ).as_posix()
        await self._checkpoint_then_cost_boundary(
            state,
            "collaboration-barrier-1",
            "completed",
            "collaboration-discovery",
            "collaboration-interface-response",
        )

        response_refs = {
            module_id: (
                f"Work/runs/{run_id}/collaboration/wave-2/"
                f"module-{module_id}.json"
            )
            for module_id in inboxes
        }
        responses: dict[str, ModuleInterfaceResponseSubmission] = {}
        pending_responses: list[str] = []
        for module_id in inboxes:
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=response_refs[module_id],
                task_id=f"module-interface-response-{module_id}",
                module_id=module_id,
                expected_type=ModuleInterfaceResponseSubmission,
            )
            if payload is None:
                pending_responses.append(module_id)
            else:
                responses[module_id] = payload
        if pending_responses:
            response_results = await asyncio.gather(
                *(
                    self._legacy_module_interface_response(
                        module_id,
                        inbox_ref=inbox_refs[module_id],
                        discovery_ref=discovery_refs[module_id],
                        state=state,
                        workflow_id=workflow_id,
                    )
                    for module_id in pending_responses
                ),
                return_exceptions=True,
            )
            failures = []
            for module_id, result in zip(
                pending_responses,
                response_results,
                strict=True,
            ):
                if isinstance(result, BaseException):
                    failures.append(result)
                    continue
                responses[module_id] = result
            if failures:
                raise failures[0]

        ordered_responses = [
            responses[module_id] for module_id in inboxes
        ]
        try:
            bundles = build_collaboration_bundles(
                ordered_discoveries,
                ordered_responses,
                known_evidence_ids=known_evidence_ids,
            )
        except ValueError as exc:
            invalid_modules = self._invalid_response_modules(
                responses,
                inboxes,
                known_evidence_ids,
            ) or set(inboxes)
            for module_id in set(inboxes) - invalid_modules:
                self.service.store.write_json(
                    response_refs[module_id],
                    responses[module_id].model_dump(mode="json"),
                )
            self._reject_collaboration_candidates(
                run_id=run_id,
                wave="wave-2",
                artifact_refs=[
                    response_refs[module_id]
                    for module_id in invalid_modules
                ],
                task_ids=[
                    f"module-interface-response-{module_id}"
                    for module_id in invalid_modules
                ],
                reason=str(exc),
            )
            state.pop("collaboration_barrier2_ref", None)
            state.pop("collaboration_bundle_refs", None)
            raise AgentWorkflowError(f"Barrier 2 failed: {exc}") from exc
        for module_id, payload in responses.items():
            self.service.store.write_json(
                response_refs[module_id],
                payload.model_dump(mode="json"),
            )
        bundle_refs: dict[str, str] = {}
        for module_id, bundle in bundles.items():
            bundle_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/bundles/"
                    f"module-{module_id}.json"
                ),
                bundle.model_dump(mode="json"),
            )
            bundle_refs[module_id] = bundle_path.relative_to(
                self.service.workspace
            ).as_posix()
        unresolved_request_ids = sorted(
            item.request_id
            for response in ordered_responses
            for item in response.dispositions
            if item.status == "unresolved"
        )
        barrier2_path = self.service.store.write_json(
            f"Work/runs/{run_id}/collaboration/barrier-2.json",
            {
                "kind": "module_collaboration_barrier_2",
                "run_id": run_id,
                "barrier_1_ref": state["collaboration_barrier1_ref"],
                "response_refs": response_refs,
                "bundle_refs": bundle_refs,
                "request_count": sum(
                    len(discovery.requests)
                    for discovery in ordered_discoveries
                ),
                "unresolved_request_ids": unresolved_request_ids,
            },
        )
        state["collaboration_barrier2_ref"] = barrier2_path.relative_to(
            self.service.workspace
        ).as_posix()
        state["collaboration_bundle_refs"] = bundle_refs
        await self._checkpoint_then_cost_boundary(
            state,
            "collaboration-barrier-2",
            "completed",
            "collaboration-interface-response",
            "module-authoring",
        )

    def _submodule_authoring_input(
        self,
        submodule_id: str,
        state: dict,
    ) -> tuple[SubmoduleAuthoringInput, str, SubmoduleCollaborationBundle]:
        """Write the immutable current input for one Wave 3 leaf task."""

        module_id = resolve_submodule(submodule_id).module_id
        bundle_ref = state.get("submodule_collaboration_bundle_refs", {}).get(
            submodule_id
        )
        if not bundle_ref:
            raise AgentWorkflowError(
                f"submodule {submodule_id} lacks its Barrier 2 bundle"
            )
        bundle_path = (self.service.workspace / bundle_ref).resolve()
        run_root = (
            self.service.workspace / f"Work/runs/{state['run_id']}"
        ).resolve()
        if not bundle_path.is_relative_to(run_root) or not bundle_path.is_file():
            raise AgentWorkflowError(
                f"submodule collaboration bundle is unreadable: {bundle_ref}"
            )
        bundle = SubmoduleCollaborationBundle.model_validate_json(
            bundle_path.read_text(encoding="utf-8")
        )
        if bundle.module_id != module_id or bundle.submodule_id != submodule_id:
            raise AgentWorkflowError(
                f"submodule collaboration bundle identity mismatch: {submodule_id}"
            )
        discovery_ref = (
            f"Work/runs/{state['run_id']}/collaboration/wave-1/submodules/"
            f"{submodule_id}.json"
        )
        contract = SubmoduleAuthoringInput(
            run_id=state["run_id"],
            module_id=module_id,
            submodule_id=submodule_id,
            revision=0,
            coverage_ref=state["preparation_refs"]["coverage"],
            evidence_ref=state["preparation_refs"]["evidence"],
            manifest_ref=state["preparation_refs"]["manifest"],
            knowledge_ref=state["module_knowledge_refs"][module_id],
            collaboration_bundle_ref=bundle_ref,
            discovery_ref=discovery_ref,
        )
        path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/context/"
                f"submodule-authoring-{submodule_id}-r0.json"
            ),
            contract.model_dump(mode="json"),
        )
        return (
            contract,
            path.relative_to(self.service.workspace).as_posix(),
            bundle,
        )

    async def _submodule_authoring(
        self,
        submodule_id: str,
        state: dict,
        workflow_id: str,
    ) -> SubmoduleDraftSubmission:
        """Run one independently fenced and recoverable Wave 3 leaf author."""

        module_id = resolve_submodule(submodule_id).module_id
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item
            for item in state["module_dispatch"].module_tasks
            if item.agent_id == specialist_id
        )
        contract, contract_ref, bundle = self._submodule_authoring_input(
            submodule_id, state
        )
        shared_ref = self._shared_module_context_ref(
            state,
            module_id,
            purpose="module-author",
            include_author_skill=True,
        )
        role_skill_refs = [
            ref
            for key in ("core", "analysis", "visual", "rubric")
            if (ref := state.get("template_skill_refs", {}).get(key))
        ]
        bundle_context = bundle.model_dump_json()
        constraints = list(
            dict.fromkeys(
                [
                    *planned.constraints,
                    f"唯一写作范围是固定叶子子模块 {submodule_id}",
                    "只调用 write_result_part 写入当前 submodule_id；不得写其他 part_id",
                    "必须消费当前子模块 discovery 以及全部 requested/responded interface",
                    "每个 answered 接口都保留答案和适用条件；每个 unresolved 接口都保留边界",
                    "固定标题之外，内部现状、判断、机理、建议和验收标签不得使用数字编号",
                    "不得调用 query_peer/reply_peer；Barrier 2 已冻结接口",
                    *state["request"].execution_requirements,
                    *self._evidence_policy_constraints(
                        state["request"].missing_evidence_policy
                    ),
                    *self._user_supplement_constraints(
                        state,
                        stage="module_authoring",
                        target_ids={module_id, submodule_id},
                    ),
                ]
            )
        )
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"submodule-author-{submodule_id}",
                    "run_id": state["run_id"],
                    "agent_id": specialist_id,
                    "objective": (
                        f"完成叶子子模块 {submodule_id} 的最终专业正文；"
                        "提交前持久化该唯一正文 part。"
                    ),
                    "allowed_outputs": ["submodule_draft_submission"],
                    "allowed_tools": [
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
                    ],
                    "target_submodule_ids": [submodule_id],
                    "constraints": constraints,
                    "revision": 0,
                    "input_refs": [
                        contract_ref,
                        contract.coverage_ref,
                        contract.evidence_ref,
                        contract.manifest_ref,
                        contract.collaboration_bundle_ref,
                        contract.discovery_ref,
                        contract.knowledge_ref,
                        shared_ref,
                        *role_skill_refs,
                    ],
                    "input_contract_kind": "submodule_authoring_input",
                    "input_contract_ref": contract_ref,
                    "prior_result_ref": None,
                    "context_summary_refs": [],
                    "inline_context": (
                        self._module_author_leaf_inline_context(
                            state,
                            module_id,
                            submodule_id,
                            shared_ref,
                        )
                        + "\n\n<submodule_collaboration_bundle>\n"
                        + bundle_context
                        + "\n</submodule_collaboration_bundle>"
                    ),
                    "artifact_delivery_modes": {
                        contract_ref: "inline",
                        contract.coverage_ref: "reference",
                        contract.evidence_ref: "reference",
                        contract.manifest_ref: "reference",
                        contract.collaboration_bundle_ref: "hash_retained",
                        contract.discovery_ref: "hash_retained",
                        contract.knowledge_ref: "reference",
                        shared_ref: "reference",
                        **{ref: "reference" for ref in role_skill_refs},
                    },
                }
            ).model_dump(mode="python")
        )
        payload = await self._agent(
            specialist_id,
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"submodule-{submodule_id}",
        )
        if (
            not isinstance(payload, SubmoduleDraftSubmission)
            or payload.module_id != module_id
            or payload.submodule_id != submodule_id
        ):
            raise AgentWorkflowError(
                f"{specialist_id} returned the wrong Wave 3 submodule draft"
            )
        return payload

    def _write_submodule_module_completion(
        self,
        state: dict,
        module_id: str,
        submission: ModuleSubmission,
        *,
        barrier_ref: str,
    ) -> str:
        """Bind a reduced module subject to all leaf completions and current context."""

        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/"
            f"{module_id}-r{submission.revision}.json"
        )
        subject_path = self.service.workspace / subject_ref
        barrier_path = self.service.workspace / barrier_ref
        completion_path = self.service.store.write_json(
            (
                f"Work/runs/{state['run_id']}/collaboration/wave-3/"
                f"module-{module_id}-r{submission.revision}.json"
            ),
            {
                "kind": "module_authoring_completion",
                "version": 2,
                "source_mode": "submodule_reducer",
                "run_id": state["run_id"],
                "module_id": module_id,
                "revision": submission.revision,
                "subject_ref": subject_ref,
                "subject_sha256": self._sha256(subject_path),
                "submodule_barrier_ref": barrier_ref,
                "submodule_barrier_sha256": self._sha256(barrier_path),
                "authoring_context_sha256": (
                    self._module_authoring_context_sha256(state, module_id)
                ),
            },
        )
        return completion_path.relative_to(self.service.workspace).as_posix()

    async def _run_submodule_authoring_stage(
        self,
        module_ids: tuple[str, ...],
        state: dict,
        workflow_id: str,
    ) -> None:
        """Run 37 leaf authors, then reduce exactly once per module."""

        submodule_ids = tuple(
            submodule_id
            for module_id in module_ids
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        )
        run_id = state["run_id"]
        draft_refs = {
            submodule_id: (
                f"Work/runs/{run_id}/collaboration/wave-3/submodules/"
                f"{submodule_id}-r0.json"
            )
            for submodule_id in submodule_ids
        }
        draft_contexts: dict[str, str] = {}
        for submodule_id in submodule_ids:
            contract, contract_ref, _bundle = self._submodule_authoring_input(
                submodule_id, state
            )
            draft_contexts[submodule_id] = self._submodule_task_context_sha256(
                state,
                task_kind="submodule_authoring",
                submodule_id=submodule_id,
                input_refs=[
                    contract_ref,
                    contract.coverage_ref,
                    contract.evidence_ref,
                    contract.manifest_ref,
                    contract.collaboration_bundle_ref,
                    contract.discovery_ref,
                ],
            )
        drafts: dict[str, SubmoduleDraftSubmission] = {}
        pending: list[str] = []
        for submodule_id in submodule_ids:
            module_id = resolve_submodule(submodule_id).module_id
            payload = self._load_collaboration_submission(
                run_id=run_id,
                artifact_ref=draft_refs[submodule_id],
                task_id=f"submodule-author-{submodule_id}",
                module_id=module_id,
                submodule_id=submodule_id,
                expected_type=SubmoduleDraftSubmission,
                wave="wave-3",
                expected_context_sha256=draft_contexts[submodule_id],
            )
            if payload is None:
                pending.append(submodule_id)
            else:
                drafts[submodule_id] = payload
        if pending:
            drafts.update(
                await self._run_scheduled_submodule_stage(
                    tuple(pending),
                    run_id=run_id,
                    workflow_id=workflow_id,
                    task_kind="submodule_authoring",
                    concurrency=state["request"].submodule_task_concurrency,
                    execute=lambda submodule_id: self._submodule_authoring(
                        submodule_id,
                        state,
                        workflow_id,
                    ),
                    persist=lambda submodule_id, payload: (
                        self._persist_submodule_task_completion(
                            state=state,
                            task_kind="submodule_authoring",
                            wave="wave-3",
                            submodule_id=submodule_id,
                            artifact_ref=draft_refs[submodule_id],
                            context_sha256=draft_contexts[submodule_id],
                            payload=payload,
                        )
                    ),
                )
            )
        for submodule_id, payload in drafts.items():
            self.service.store.write_json(
                draft_refs[submodule_id], payload.model_dump(mode="json")
            )

        source_records = SourceLedger(self.service.workspace, run_id).records
        barrier_refs: dict[str, str] = {}
        for module_id in module_ids:
            expected = tuple(REPORT_TAXONOMY[module_id].submodules)
            scoped = {submodule_id: drafts[submodule_id] for submodule_id in expected}
            if set(scoped) != set(expected):
                raise AgentWorkflowError(
                    f"module {module_id} reducer lacks exact leaf completion"
                )
            claim_ids = [item.claim.id for item in scoped.values()]
            if len(claim_ids) != len(set(claim_ids)):
                raise AgentWorkflowError(
                    f"module {module_id} reducer received duplicate Claim ids"
                )
            submission = ModuleSubmission(
                module_id=module_id,
                submodule_narratives={
                    submodule_id: scoped[submodule_id].narrative
                    for submodule_id in expected
                },
                claims=[scoped[submodule_id].claim for submodule_id in expected],
                source_ids=sorted(
                    {
                        source_id
                        for submodule_id in expected
                        for source_id in scoped[submodule_id].source_ids
                    }
                ),
                unresolved_questions=list(
                    dict.fromkeys(
                        question
                        for submodule_id in expected
                        for question in scoped[submodule_id].unresolved_questions
                    )
                ),
                revision=0,
            )
            ClaimLedger(claims=submission.claims, sources=source_records)
            subject_path = self.service.store.write_json(
                f"Work/runs/{run_id}/modules/{module_id}-r0.json",
                submission.model_dump(mode="json"),
            )
            scoped_refs = {
                submodule_id: draft_refs[submodule_id] for submodule_id in expected
            }
            barrier_path = self.service.store.write_json(
                (
                    f"Work/runs/{run_id}/collaboration/wave-3/module-barriers/"
                    f"module-{module_id}.json"
                ),
                {
                    "kind": "module_submodule_authoring_barrier",
                    "version": 1,
                    "run_id": run_id,
                    "module_id": module_id,
                    "submodule_ids": list(expected),
                    "completion_refs": scoped_refs,
                    "completion_sha256": {
                        submodule_id: self._sha256(
                            self.service.workspace / scoped_refs[submodule_id]
                        )
                        for submodule_id in expected
                    },
                    "subject_ref": subject_path.relative_to(
                        self.service.workspace
                    ).as_posix(),
                    "subject_sha256": self._sha256(subject_path),
                },
            )
            barrier_ref = barrier_path.relative_to(
                self.service.workspace
            ).as_posix()
            barrier_refs[module_id] = barrier_ref
            self._write_submodule_module_completion(
                state,
                module_id,
                submission,
                barrier_ref=barrier_ref,
            )
            state.setdefault("specialist_submissions", {})[module_id] = submission
        state["submodule_authoring_barrier_refs"] = barrier_refs
        await self._checkpoint_then_cost_boundary(
            state,
            "module-work",
            "in_progress",
            "submodule-authoring",
            "module-review",
        )

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
        bundle_ref = state.get("collaboration_bundle_refs", {}).get(module_id)
        bundle_sha256 = None
        if bundle_ref:
            bundle_path = (self.service.workspace / bundle_ref).resolve()
            if not bundle_path.is_file():
                raise AgentWorkflowError(
                    f"module lane bundle is missing: {module_id}: {bundle_ref}"
                )
            bundle_sha256 = self._sha256(bundle_path)
        semantic_key = hashlib.sha256(
            json.dumps(
                {
                    "run_id": state["run_id"],
                    "module_id": module_id,
                    "preparation_sha256": preparation_sha256,
                    "collaboration_bundle_sha256": bundle_sha256,
                    "authoring_context_sha256": self._module_authoring_context_sha256(
                        state, module_id
                    ),
                    "schema_version": "1",
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
            collaboration_bundle_sha256=bundle_sha256,
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
    ) -> tuple[ModuleSubmission, str, LaneCompletion, dict]:
        lane_state = deepcopy(state)
        lane_state["_bounded_module_lane"] = module_id
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

    async def _run_bounded_module_lanes(
        self,
        requested_modules: tuple[str, ...],
        state: dict,
        workflow_id: str,
        *,
        concurrency: int,
    ) -> None:
        """Incrementally admit isolated module lanes and reduce at one barrier."""

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
                "concurrency": min(max(1, concurrency), len(pending) or 1),
                "recovered_modules": sorted(results, key=float),
                "scheduling_policy": "longest_critical_path_first_v1",
                "scheduling_candidates": [
                    candidate.model_dump(mode="json")
                    for candidate in candidates
                ],
            },
        )
        failures: list[BaseException] = []
        deferred_main_modules: set[str] = set()
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
                        module_id, state, workflow_id
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
                except DeferredMainDecision:
                    deferred_main_modules.add(module_id)
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
                    freeze_admission.set()
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

        workers = [
            asyncio.create_task(
                worker(), name=f"module-lane-worker-{index + 1}"
            )
            for index in range(min(max(1, concurrency), len(pending) or 1))
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
        if failures:
            raise failures[0]
        for module_id in sorted(deferred_main_modules, key=float):
            results[module_id] = await self._execute_module_lane(
                module_id,
                state,
                workflow_id,
                defer_main_exceptions=False,
            )

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
                self._bind_reviewed_module_to_authoring_context(
                    state,
                    module_id,
                    submission,
                    provenance="bounded_module_lane",
                )
            else:
                submission = state["module_submissions"][module_id]
                spec = self._lane_task_spec(state, module_id)
                completion_ref, completion = self._build_lane_completion(
                    state, module_id, submission, spec
                )
            completions.append((completion_ref, completion))

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
        collaboration_bundle_ref = state.get(
            "collaboration_bundle_refs", {}
        ).get(module_id)
        resumed_payload = state.get("specialist_submissions", {}).get(module_id)
        forced_fresh_revision = state.get(
            "invalidated_authoring_revisions", {}
        ).get(module_id)
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
                        " assistant turn 内为所有 ready 叶子分别调用 write_result_part，"
                        "再在下一轮提交小型 module commit；不得把多个叶子合并成一个 part"
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
                "协作 bundle、输入或用户补充约束已变化；本 revision 必须从当前上下文完整重写全部固定子模块，禁止复用旧 draft parts"
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

        collaboration_context = ""
        if collaboration_bundle_ref:
            collaboration_path = (
                self.service.workspace / collaboration_bundle_ref
            ).resolve()
            run_root = (
                self.service.workspace / f"Work/runs/{state['run_id']}"
            ).resolve()
            if (
                not collaboration_path.is_relative_to(run_root)
                or not collaboration_path.is_file()
            ):
                raise AgentWorkflowError(
                    "module collaboration bundle is not a readable current-run "
                    f"artifact: {collaboration_bundle_ref}"
                )
            try:
                collaboration_bundle = (
                    ModuleCollaborationBundle.model_validate_json(
                        collaboration_path.read_text(encoding="utf-8")
                    )
                )
            except (OSError, ValueError) as exc:
                raise AgentWorkflowError(
                    "module collaboration bundle is invalid: "
                    f"{collaboration_bundle_ref}: {exc}"
                ) from exc
            if collaboration_bundle.module_id != module_id:
                raise AgentWorkflowError(
                    "module collaboration bundle ownership mismatch: "
                    f"{collaboration_bundle.module_id} != {module_id}"
                )
            base_constraints.extend(
                [
                    (
                        "这是 Wave 3 最终写作；必须先消费内联的 "
                        "module_collaboration_bundle，并复用其中 Wave 1 "
                        "discovery，禁止重复已经完成的宽泛检索"
                    ),
                    (
                        "只对 bundle 尚未覆盖的具体证据缺口追加检索；"
                        "把 requested/responded interface 的答案、适用条件和"
                        " unresolved 边界写入相关小节"
                    ),
                    "不得调用 query_peer/reply_peer；两道 Barrier 已冻结本轮模块接口",
                ]
            )
            collaboration_context = (
                "\n\n<module_collaboration_bundle>\n"
                + collaboration_bundle.model_dump_json()
                + "\n</module_collaboration_bundle>"
            )
        else:
            base_constraints.append(
                "本次没有三波协作包；不得调用 query_peer/reply_peer，部分模块运行不等待未调度的同伴"
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
            collaboration_bundle_ref=collaboration_bundle_ref,
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
                        *(
                            [module_input.collaboration_bundle_ref]
                            if module_input.collaboration_bundle_ref
                            else []
                        ),
                    ],
                    "input_contract_kind": "module_authoring_input",
                    "input_contract_ref": module_input_ref,
                    "inline_context": (
                        (planned.inline_context or "") + collaboration_context
                    ),
                    "artifact_delivery_modes": {
                        module_input_ref: "inline",
                        module_input.coverage_ref: "reference",
                        module_input.evidence_ref: "reference",
                        module_input.manifest_ref: "reference",
                        **(
                            {
                                module_input.collaboration_bundle_ref: (
                                    "hash_retained"
                                )
                            }
                            if module_input.collaboration_bundle_ref
                            else {}
                        ),
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
            if collaboration_bundle_ref:
                self._write_module_authoring_completion(
                    state,
                    module_id,
                    payload,
                    module_input_ref=module_input_ref,
                    envelope=envelope,
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
        await run_cross_review(self, state, workflow_id)

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
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
        editor_input = ChiefEditorInput(
            run_id=state["run_id"],
            approved_module_markers={
                module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
            },
            modules={
                module_id: module_content_view(state["module_submissions"][module_id])
                for module_id in REPORT_MODULE_IDS
            },
            cross_review_completion_ref=state["cross_review_completion_ref"],
            special_topic_plan=special_topic_plan,
        )
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
                state["preparation_refs"]["evidence"],
                state["preparation_refs"]["photo_manifest"],
                *special_topic_input_refs,
            ],
            constraints=[
                "不得改变批准事实、数值、风险等级和来源语义",
                "批准正文的引用与脚注由运行时保护和装配，总编只提交 schema 声明字段",
                "protected_claim_ids 由 submit_result 根据运行时已批准模块确定性注入；不得自行提交、打开或重传 Claim/Source ledger",
                "正文不得套用统一的事实-证据-风险模板",
                "tables 只提交可追溯的 E-* evidence_ids；photo_ids 提交空数组，运行时将原始表图片全量绑定到 Evidence 所属最小子模块",
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
            validate_final_report_markdown(canonical)
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
        self.service.store.write_json("Work/report-state.json", report.model_dump(mode="json"))
        self._validate_final_report_structure(state, delivery_markdown, "delivery-final")
        markdown_path = self.service.store.write_text(
            "Outputs/Reports/配电安全专家咨询报告.md", delivery_markdown
        )
        source_index_markdown = ledger.source_index_markdown(
            evidence_items=state.get("evidence_items", []),
            photo_assets=state.get("photo_assets", []),
        )
        source_index_path = self.service.store.write_text(
            "Outputs/Reports/证据与来源索引.md",
            source_index_markdown.rstrip() + "\n",
        )
        source_index_docx_path = (
            self.service.workspace / "Outputs/Reports/证据与来源索引.docx"
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
                report_id="power-distribution-report",
                version=state["run_id"],
                module_files={
                    module_id: self.service.workspace / f"Outputs/Modules/{module_id}.md"
                    for module_id in REPORT_MODULE_IDS
                },
                final_docx=output,
                report_state=self.service.workspace / "Work/report-state.json",
                source_index=source_index_path,
                source_index_docx=source_index_docx_path,
            )
        )
        receipt_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/delivery-receipt.json",
            receipt.model_dump(mode="json"),
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
        version = version_store.publish(
            ReportVersion(
                version_id=state["run_id"],
                run_id=state["run_id"],
                parent_version_id=state.get("parent_version_id"),
                artifact_refs={
                    "final_docx": receipt.final_docx.relative_to(self.service.workspace),
                    "report_state": receipt.report_state.relative_to(self.service.workspace),
                    "claim_ledger": claim_ledger_path.relative_to(self.service.workspace),
                    "source_ledger": source_ledger_path.relative_to(self.service.workspace),
                    "evidence": evidence_snapshot_path.relative_to(self.service.workspace),
                    "delivery_receipt": receipt_path.relative_to(self.service.workspace),
                    "delivery_manifest": receipt.manifest_path.relative_to(self.service.workspace),
                    **{
                        f"module:{module_id}": path.relative_to(self.service.workspace)
                        for module_id, path in receipt.module_files.items()
                    },
                    **{
                        f"module_submission:{module_id}": path.relative_to(self.service.workspace)
                        for module_id, path in approved_module_paths.items()
                    },
                    "edited_submission": edited_submission_path.relative_to(self.service.workspace),
                    "canonical_markdown": markdown_path.relative_to(self.service.workspace),
                    "source_index": source_index_path.relative_to(self.service.workspace),
                    "source_index_docx": source_index_docx_path.relative_to(
                        self.service.workspace
                    ),
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
                },
                skill_provenance=skill_provenance,
                session_summary_refs=summary_refs,
            ),
            trusted_handle_refs={
                key: receipt.trusted_handle_refs[key]
                for key in (
                    "final_docx",
                    "report_state",
                    "source_index",
                    "source_index_docx",
                    *(f"module:{module_id}" for module_id in REPORT_MODULE_IDS),
                )
                if key in receipt.trusted_handle_refs
            },
        )
        state["delivery_to_version_cas_metrics"] = (
            version_store.content_store.metrics_snapshot()
        )
        state["report_version"] = version
        storage_plan = ReportingRetentionPlanner(self.service.workspace).generate()
        state["storage_usage_ref"] = "Work/storage-usage.json"
        state["retention_plan_ref"] = "Work/retention-plan.json"
        state["storage_usage"] = storage_plan["usage"]
        state["output_artifacts"] = self._delivery_output_artifacts(
            final_review_ref=state["final_review_completion_ref"],
            delivery_manifest_ref=receipt.manifest_path.relative_to(self.service.workspace),
        )
        completion_ref = f"Work/runs/{state['run_id']}/delivery-completion.json"
        self.service.store.write_json(
            completion_ref,
            {
                "run_id": state["run_id"],
                "status": "completed",
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
            OutputArtifact(kind="report", path=Path("Outputs/Reports/证据与来源索引.md")),
            OutputArtifact(kind="report", path=Path("Outputs/Reports/证据与来源索引.docx")),
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
