"""Outer report workflow: orchestration only; professional reasoning stays in AgentLoop."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from pydantic import Field

from ..usage_ledger import UsageLedger
from .agent_runner import ReportingAgentRunner
from .agentic_models import (
    AgentRunStatus,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FINAL_REPORT_SECTION_IDS,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ModuleDispatchPlan,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleSubmission,
    StrictModel,
    TaskEnvelope,
    TemplateSkillSubmission,
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
from .delivery import DeliveryPackage, DeliveryReceipt, ProjectDelivery
from .input_contracts import (
    AggregateEditorInput,
    ChiefEditorInput,
    ModuleAuthoringInput,
    RequestedModuleChange,
    ReviewCompletionRecord,
    TemplateDistillationInput,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from .models import (
    REPORT_MODULE_IDS,
    CoverageMatrix,
    EvidenceItem,
    OutputArtifact,
    PhotoAsset,
    ProjectManifest,
    RevisionRequest,
    ScopeExpansionRequest,
)
from .module_skills import ModuleSkillLibrary
from .rendering.handoff_docx import PackagedV2DocxCore
from .rendering.pds_docx_renderer import ApprovedReport, PdsDocxRenderer
from .rendering.contracts import RenderRequest, RenderResult
from .evidence_readiness import EvidenceReadinessPolicy, ReportingBlockedError
from .research.project_evidence import project_evidence_locator
from .research.evidence_memory import EvidenceResearchMemory
from .research.knowledge_context import KnowledgeContextBuilder
from .review_lifecycle import (
    request_module_revision,
    run_cross_review,
    run_final_review,
    run_module_review,
)
from .revision_diff import build_revision_diff
from .session_summary import SessionSummaryStore
from .source_ledger import SourceLedger
from .taxonomy import REPORT_TAXONOMY, compose_module_markdown, resolve_submodule
from .versions import ReportVersion, ReportVersionStore, SkillProvenance

if TYPE_CHECKING:
    from .service import ReportingService


TEMPLATE_SKILL_ROOT = Path("Work/report-template-writing")
TEMPLATE_SKILL_SOURCE = TEMPLATE_SKILL_ROOT / "source.json"


class FullReportCheckpoint(StrictModel):
    """Durable full-report state; every reference is run-scoped and validated on restore."""

    version: int = 2
    workflow_id: str = ""
    run_id: str
    activity: str = "unknown"
    status: str = "unknown"
    preparation_refs: dict[str, str] = Field(default_factory=dict)
    preparation_sha256: dict[str, str] = Field(default_factory=dict)
    completed_modules: list[str] = Field(default_factory=list)
    specialist_modules: list[str] = Field(default_factory=list)
    module_dispatch_ref: str | None = None
    module_knowledge_refs: dict[str, str] = Field(default_factory=dict)
    quality_context_ref: str | None = None
    module_review_completion_refs: dict[str, str] = Field(default_factory=dict)
    cross_review_completion_ref: str | None = None
    chief_candidate_ref: str | None = None
    chief_editor_input_ref: str | None = None
    chief_editor_envelope_ref: str | None = None
    final_review_restart_round: int | None = Field(default=None, ge=1)
    final_review_completion_ref: str | None = None
    delivery_completion_ref: str | None = None
    report_state_ref: str | None = None
    cross_review_completed: bool = False
    final_review_completed: bool = False
    error: str | None = None
    budget: dict | None = None


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
    """Run-scoped usage telemetry without a hard stop or automatic retry boundary."""

    def __init__(self, workspace: Path, run_id: str, max_attempts: int, max_tokens: int):
        self.ledger = UsageLedger(workspace, run_id)
        self.max_attempts = max_attempts
        self.max_tokens = max_tokens
        self._issued_attempts = len(self.ledger.rows())
        self._active_dispatches = 0
        self._lock = asyncio.Lock()

    def snapshot(self) -> dict[str, int | bool]:
        rows = self.ledger.rows()
        return {
            "provider_attempts": len(rows),
            "total_tokens": sum(int(row.get("total_tokens", 0) or 0) for row in rows),
            "max_provider_attempts": self.max_attempts,
            "max_total_tokens": self.max_tokens,
            "issued_provider_attempts": self._issued_attempts,
            "active_dispatches": self._active_dispatches,
            "limits_enforced": False,
        }

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

    async def _agent(
        self,
        agent_id: str,
        envelope: TaskEnvelope,
        artifacts: list[str],
        workflow_id: str,
        *,
        session_key: str | None = None,
    ):
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
        required = [*refs.values(), TEMPLATE_SKILL_SOURCE]
        if not all((self.service.workspace / path).is_file() for path in required):
            return False
        state["template_skill_refs"] = {key: path.as_posix() for key, path in refs.items()}
        state["template_skill_text"] = {
            key: (self.service.workspace / path).read_text(encoding="utf-8")
            for key, path in refs.items()
        }
        return True

    def _require_template_skill(self, state: dict) -> None:
        if self._load_template_skill(state):
            return
        raise AgentWorkflowError(
            "固定模板写作 Skill 不完整：Work/report-template-writing/SKILL.md；"
            "请先单独运行 operation=distill_template_skill"
        )

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
        if not all((self.service.workspace / root / relative).is_file() for relative in files):
            raise AgentWorkflowError("Template Distiller did not materialize the complete Skill")

    async def _distill_template_skill(self, state: dict, workflow_id: str) -> None:
        """Let Template Distiller refresh the fixed project writing Skill."""
        selected, source = self.service.resolve_skill_distillation_template()
        snapshot = self.service.workspace / (
            f"Work/runs/{state['run_id']}/templates/template-for-skill.docx"
        )
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected, snapshot)
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
                "只迁移写作能力，不复制模板项目事实、具体数值、客户名称或原结论",
                "专家优化版只在本任务中作为一次性 Skill 蒸馏源；不得把其中的具体问题、风险判断、分析结论、建议内容、证据编号或项目措辞写入任何 Skill 文件",
                "禁止把五份长文本直接塞入 submit_result：先分别调用 write_result_part，part_id 固定为 skill、analysis、synthesis、visual、rubric；最终 submit_result 的对应字段只提交 artifact_refs",
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
                "template_sha256": hashlib.sha256(snapshot.read_bytes()).hexdigest(),
                "inspection_ref": (f"Work/runs/{state['run_id']}/context/template-inspection.json"),
                "producer": "template-distiller",
                "task_id": envelope.task_id,
                "skill_root": TEMPLATE_SKILL_ROOT.as_posix(),
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
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "template-skill-distillation"
        try:
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
                    "source.json",
                )
            ]
            await self.service._notice(
                "模板写作 Skill 已更新至固定路径 Work/report-template-writing。"
            )
        except asyncio.CancelledError:
            self._checkpoint(state, activity, "cancelled", "interrupted by user")
            raise
        except Exception as exc:
            self._checkpoint(state, activity, "failed", str(exc))
            raise
        finally:
            await self.agent_runner.close_workflow(workflow_id)

    @staticmethod
    def _template_skill_context(state: dict, *parts: str) -> str:
        texts = state.get("template_skill_text", {})
        return "\n\n".join(texts[part] for part in parts if texts.get(part))

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
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "preparation"
        suspended_for_user = False
        try:
            await self.service._notice("正在整理项目资料并建立可追溯证据入口。")
            await self._prepare(state)
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
            self._checkpoint(state, activity, "completed")
            if state.get("resume"):
                self._restore_resume_state(state, resume_checkpoint)
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
            self._checkpoint(state, activity, "completed")
            activity = "module-work"
            await self.service._notice(
                "目标专业模块将按固定顺序逐个执行；当前模块完成写作、独立审计和定向修订闭环后，"
                "才启动下一个模块。"
            )
            restored_submissions = dict(state.get("module_submissions", {}))
            pending_modules = tuple(
                module_id
                for module_id in requested_modules
                if module_id not in restored_submissions
            )
            state["module_submissions"] = restored_submissions
            for module_id in pending_modules:
                try:
                    submission = await self._module_pipeline(module_id, state, workflow_id)
                except BaseException:
                    self._checkpoint(state, activity, "failed")
                    raise
                state["module_submissions"][submission.module_id] = submission
                self._checkpoint(state, activity, "in_progress")
            self._checkpoint(state, activity, "completed")
            if set(requested_modules) != set(REPORT_MODULE_IDS):
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
            activity = "cross-module-review"
            if "cross_review_completion_ref" not in state:
                await self.service._notice("五个模块均已通过各自独立审查，开始跨模块一致性审查。")
                await self._cross_review(state, workflow_id)
                self._checkpoint(state, activity, "completed")
            else:
                await self.service._notice("已恢复本 run 完成的跨模块审查，直接进入总编。")
            if "final_review_completion_ref" not in state:
                if "chief_candidate_ref" not in state:
                    activity = "chief-edit"
                    await self.service._notice("跨模块审查通过，总编正在整合全文并保护来源语义。")
                    await self._chief_edit(state, workflow_id)
                    self._checkpoint(state, activity, "completed")
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
                self._checkpoint(state, activity, "completed")
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
            self._checkpoint(
                state,
                activity,
                "waiting_user" if suspended_for_user else "stopped_incomplete",
                str(exc),
            )
            raise
        except asyncio.CancelledError:
            self._checkpoint(state, activity, "cancelled", "interrupted by user")
            raise
        except Exception as exc:
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
        configured_refs = request.source_module_refs or {
            module_id: Path(f"Outputs/Modules/{module_id}.md") for module_id in REPORT_MODULE_IDS
        }
        module_refs: dict[str, str] = {}
        structured_modules: dict[str, ModuleSubmission] = {}
        markdown_modules: dict[str, str] = {}
        structured_sources = {}
        for module_id in REPORT_MODULE_IDS:
            relative = Path(configured_refs[module_id])
            source = (self.service.workspace / relative).resolve()
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
            module_refs[module_id] = relative.as_posix()
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
        aggregate_source_format = "structured_module" if structured_modules else "markdown"
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
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "aggregate-chief-edit"
        suspended_for_user = False
        try:
            self._aggregate_checkpoint(state, activity, "in_progress")
            await self.service._notice(
                "已识别为已有分块报告汇总任务；读取固定模板写作 Skill 后启动总编和独立成稿审计，"
                "不启动模块专家、单模块审计或跨模块审查。"
            )
            self._require_template_skill(state)
            await self.service._notice(
                "已从 Work/report-template-writing 加载 Skill；总编不会读取或蒸馏模板 DOCX。"
            )
            if structured_modules:
                editor_input = AggregateEditorInput(
                    run_id=run_id,
                    source_format="structured_module",
                    approved_module_markers={
                        module_id: f"[[APPROVED_MODULE:{module_id}]]"
                        for module_id in REPORT_MODULE_IDS
                    },
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
                ],
                constraints=[
                    "输入是已完成的分块报告；不得重新检索项目证据，但必须在完整保留原文基础上进行跨模块联合分析",
                    "不得创造、删除或改变分块报告中的事实、数值、风险等级和建议语义",
                    "必须保留且仅汇总 2.1、2.2、2.3、2.4、2.5 五个模块",
                    "每个 module_narrative 必须包含对应 [[APPROVED_MODULE:2.x]] 标记，可在标记前后增加短过渡；不得重新输出或改写原文，工作流会确定性嵌回批准正文",
                    "aggregate-editor-input.json 是唯一模块内容读取入口；一次使用 160000 字符完整读取，只有明确返回 next_offset 时才继续，禁止搜索或重新打开原始模块文件",
                    "总编按四大块固定结构新增 assessment_background、findings_overview、regional_executive_summary、risk_panorama、dimension_risk_analysis、cross_module_analysis、data_gap_analysis、improvement_action_plan、new_factory_planning、capacity_expansion_plan、daily_power_management、emergency_compliance_management；十二个长字段分别使用同名 part_id 的 write_result_part 持久化。只提交各节正文，不输出章节标题",
                    "五个 module_narratives 只提交精确 APPROVED_MODULE 标记，禁止为省 token 压缩批准正文",
                    "任何综合节都必须自足地给出归纳事实、综合判断和决策含义；章节号只能作为句末追溯，不得用‘详见第二章’‘见2.x’或模块编号清单代替汇总分析",
                    "regional_executive_summary 必须按真实区域或责任边界归纳重点、优先行动与验证状态；没有区域划分证据时必须明确边界，禁止编造区域名称",
                    "dimension_risk_analysis 必须逐一比较五个专业维度的主导风险、相互作用和管理含义；data_gap_analysis 必须归并重复缺口并说明它影响哪些判断和补证优先级；improvement_action_plan 必须按依赖顺序列出责任接口、行动、验收指标和剩余风险",
                    "专项问题分析四节必须分别形成新建规划、增容决策、日常用电管理、应急与合规管理的自足分析；只能综合当前项目批准事实和明确标注的通用工程原则，禁止迁移专家优化版的具体问题、判断、结论或建议",
                    "Template Distiller 产出的固定模板写作 Skill 已在 inline_context 中提供；按其风格、叙述、思考和质量量表整合，不复制模板客户事实",
                    "可使用模型世界知识解释模块联系、机制、整改依赖和行业实践；不得把通用知识写成当前项目事实",
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
                inline_context=self._template_skill_context(
                    state, "core", "analysis", "synthesis", "visual", "rubric"
                ),
            )
            payload = await self._agent(
                "chief-editor",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key="aggregate-existing",
            )
            if not isinstance(payload, EditedReportSubmission):
                raise AgentWorkflowError("chief-editor returned the wrong payload type")
            if not structured_modules and (
                payload.protected_claim_ids or payload.tables or payload.photo_ids
            ):
                raise AgentWorkflowError(
                    "markdown aggregate submission bypassed its input/output contract; "
                    "unverified structured bindings were not accepted or rewritten"
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
            payload = expand_approved_module_markers(payload, source_modules)
            state["editor_quality_observations"] = validate_aggregate_retention(
                payload, source_modules
            )
            ledger = None
            claims = []
            if structured_modules:
                claims = [
                    claim
                    for module_id in REPORT_MODULE_IDS
                    for claim in structured_modules[module_id].claims
                ]
                ledger = ClaimLedger(
                    claims=claims,
                    sources=list(structured_sources.values()),
                )
                validate_editor_protection(payload, claims)
                state["module_submissions"] = structured_modules
                self.service.store.write_json(
                    f"Work/runs/{run_id}/ledgers/claims.json",
                    ledger.model_dump(mode="json"),
                )
            state["edited_report"] = payload
            self._aggregate_checkpoint(state, activity, "completed")
            activity = "aggregate-chief-editor-audit"
            await self.service._notice("总编汇总稿已形成，正在进行独立全文质量与交付就绪审计。")
            await self._final_review_loop(
                state,
                workflow_id,
                chief_envelope=envelope,
                chief_session_key="aggregate-existing",
                approved_module_text=source_modules,
                claims=claims,
                aggregate_mode=True,
            )
            self._aggregate_checkpoint(state, activity, "completed")
            activity = "aggregate-markdown"
            payload = state["edited_report"]
            filename = request.output_filename or "配电安全专家咨询报告.docx"
            markdown_ref = Path("Outputs/Reports") / f"{Path(filename).stem}.md"
            markdown = self._canonical_markdown(payload)
            if ledger is not None:
                markdown = ledger.bind_citations(markdown)
                if payload.tables:
                    table_lines = ["", "**结构化表格**", ""]
                    for table in payload.tables:
                        table_lines.extend(
                            [
                                f"**{table.title}**",
                                "",
                                "| " + " | ".join(table.headers) + " |",
                                "| " + " | ".join("---" for _ in table.headers) + " |",
                                *("| " + " | ".join(row) + " |" for row in table.rows),
                                "",
                                "来源：" + "、".join(table.source_ids),
                                "",
                            ]
                        )
                    table_markdown = "\n".join(table_lines)
                    chapter_four = "\n## 4. 专项问题分析"
                    if chapter_four not in markdown:
                        raise AgentWorkflowError("总报告缺少第4章，无法放置结构化表格")
                    markdown = markdown.replace(
                        chapter_four,
                        f"\n{table_markdown}\n{chapter_four}",
                        1,
                    )
                markdown += "\n\n" + ledger.source_index_markdown() + "\n"
            self._validate_final_report_structure(state, markdown, "aggregate-final")
            self.service.store.write_text(markdown_ref.as_posix(), markdown)
            state["aggregate_markdown_ref"] = markdown_ref
            state["output_artifacts"] = [OutputArtifact(kind="report", path=markdown_ref)]
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
                },
            )
            self._aggregate_checkpoint(state, activity, "completed")
        except ReportingNeedsDecisionError as exc:
            suspended_for_user = exc.keep_agents_alive
            self._aggregate_checkpoint(
                state,
                activity,
                "waiting_user" if suspended_for_user else "stopped_incomplete",
                str(exc),
            )
            raise
        except asyncio.CancelledError:
            self._aggregate_checkpoint(state, activity, "cancelled", "interrupted by user")
            raise
        except Exception as exc:
            self._aggregate_checkpoint(state, activity, "failed", str(exc))
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
        self._budget = ReportingRunBudget(
            self.service.workspace,
            state["run_id"],
            request.max_provider_attempts,
            request.max_total_tokens,
        )
        self.agent_runner.set_provider_attempt_guard(self._budget.acquire_provider_attempt)
        activity = "revision-restore"
        try:
            await self.service._notice(
                f"正在从报告版本 {request.baseline_version_id} 恢复结构化状态并执行局部修订。"
            )
            self._require_template_skill(state)
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
            for module_id in request.target_module_ids:
                if module_id in completed_revision_modules:
                    continue
                await self._post_delivery_module_revision(module_id, state, request, workflow_id)
                completed_revision_modules.add(module_id)
                state["completed_revision_modules"] = sorted(completed_revision_modules)
                self._revision_checkpoint(state, activity, "in_progress")
            activity = "revision-cross-review"
            if "cross_review_completion_ref" not in state:
                await self._cross_review(state, workflow_id)
                self._revision_checkpoint(state, activity, "completed")
            else:
                await self.service._notice("已恢复本修订 run 完成的跨模块审查，直接进入总编。")
            if "final_review_completion_ref" not in state:
                activity = "revision-chief-edit"
                state["chief_editor_constraints"] = [
                    "这是交付后局部修订：未获批准的模块正文必须逐字保持父版本内容",
                    "只可更新输入合同授权的目标模块与固定综合章节字段",
                ]
                await self._chief_edit(state, workflow_id)
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
                self._revision_checkpoint(state, activity, "completed")
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
        except asyncio.CancelledError:
            self._revision_checkpoint(state, activity, "cancelled", "interrupted by user")
            raise
        except Exception as exc:
            self._revision_checkpoint(state, activity, "failed", str(exc))
            raise
        finally:
            await self.agent_runner.close_workflow(workflow_id)

    def _context_refs(self, state: dict, agent_id: str) -> list[str]:
        return list(state.get("revision_context_by_agent", {}).get(agent_id, []))

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
            completed_modules=completed_modules,
            specialist_modules=sorted(state.get("specialist_submissions", {})),
            module_dispatch_ref=(
                f"Work/runs/{state['run_id']}/workflow/module-dispatch.json"
                if "module_dispatch" in state
                else None
            ),
            module_knowledge_refs=dict(state.get("module_knowledge_refs", {})),
            quality_context_ref=state.get("quality_context_ref"),
            report_state_ref=("Work/report-state.json" if "edited_report" in state else None),
            module_review_completion_refs=dict(state.get("module_review_completion_refs", {})),
            cross_review_completed="cross_review_completion_ref" in state,
            cross_review_completion_ref=state.get("cross_review_completion_ref"),
            chief_candidate_ref=state.get("chief_candidate_ref"),
            chief_editor_input_ref=state.get("chief_editor_input_ref"),
            chief_editor_envelope_ref=state.get("chief_editor_envelope_ref"),
            final_review_restart_round=state.get("final_review_restart_round"),
            final_review_completed="final_review_completion_ref" in state,
            final_review_completion_ref=state.get("final_review_completion_ref"),
            delivery_completion_ref=state.get("delivery_completion_ref"),
            error=error,
            budget=self._budget.snapshot() if self._budget is not None else None,
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
                "aggregate_markdown_ref": (
                    str(state["aggregate_markdown_ref"])
                    if state.get("aggregate_markdown_ref")
                    else None
                ),
                "error": error,
                "budget": (self._budget.snapshot() if self._budget is not None else None),
            },
        )

    def _load_current_review_completion(
        self,
        *,
        run_id: str,
        completion_ref: str,
        lifecycle: str,
        reviewer_agent_id: str,
        reviewer_session_key: str,
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
        if (
            completion.lifecycle != lifecycle
            or completion.run_id != run_id
            or completion.reviewer_agent_id != reviewer_agent_id
            or completion.reviewer_session_key != reviewer_session_key
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
            if typed_checkpoint.quality_context_ref is None:
                raise AgentWorkflowError("checkpoint lacks the chief quality context ref")
            state["quality_context_ref"] = require_run_ref(
                typed_checkpoint.quality_context_ref,
                label="chief quality context",
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
                            reviewer_session_key=(f"module-auditor-{module_id}-{lifecycle_id}"),
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

        canonical_chief_candidate = f"Work/runs/{run_id}/edited-revisions/chief-r0.json"
        canonical_chief_input = f"Work/runs/{run_id}/context/chief-editor-input.json"
        canonical_chief_envelope = f"Work/runs/{run_id}/context/chief-editor-envelope.json"
        discovered_candidate_ref = typed_checkpoint.chief_candidate_ref
        if discovered_candidate_ref is None and all(
            (self.service.workspace / ref).is_file()
            for ref in (
                canonical_chief_candidate,
                canonical_chief_input,
                canonical_chief_envelope,
            )
        ):
            discovered_candidate_ref = canonical_chief_candidate
        if discovered_candidate_ref:
            candidate_ref = require_run_ref(
                discovered_candidate_ref,
                label="chief candidate",
            )
            candidate = EditedReportSubmission.model_validate_json(
                (self.service.workspace / candidate_ref).read_text(encoding="utf-8")
            )
            claims = [
                claim
                for module_id in REPORT_MODULE_IDS
                for claim in approved_subjects[module_id].claims
            ]
            validate_editor_protection(candidate, claims)
            state["editor_quality_observations"] = validate_editor_quality(
                candidate, approved_subjects
            )
            validate_final_report_markdown(self._canonical_markdown(candidate))
            envelope_ref = require_run_ref(
                typed_checkpoint.chief_editor_envelope_ref or canonical_chief_envelope,
                label="chief editor envelope",
            )
            input_ref = require_run_ref(
                typed_checkpoint.chief_editor_input_ref or canonical_chief_input,
                label="chief editor input",
            )
            state["edited_report"] = candidate
            state["chief_candidate_ref"] = candidate_ref
            state["chief_editor_input_ref"] = input_ref
            state["chief_editor_envelope_ref"] = envelope_ref
            state["chief_editor_envelope"] = TaskEnvelope.model_validate_json(
                (self.service.workspace / envelope_ref).read_text(encoding="utf-8")
            )
            state["chief_editor_session_key"] = "chief-editor"
            state["approved_module_text"] = {
                module_id: self._approved_module_text(approved_subjects[module_id])
                for module_id in REPORT_MODULE_IDS
            }

        final_ref = f"Work/runs/{run_id}/reviews/final-completion.json"
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
            validate_final_report_markdown(self._canonical_markdown(edited))
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
                "final_review_completed": "final_review_completion_ref" in state,
                "final_review_completion_ref": state.get("final_review_completion_ref"),
                "error": error,
                "budget": self._budget.snapshot() if self._budget is not None else None,
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
                completion_ref = state.get("module_review_completion_refs", {}).get(module_id)
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
                        reviewer_session_key=(f"module-auditor-{module_id}-{lifecycle_id}"),
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
            validate_final_report_markdown(self._canonical_markdown(edited))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise AgentWorkflowError(
                "refusing to replay final revision because its current-run "
                f"completion is invalid: {final_ref}: {exc}"
            ) from exc
        state["edited_report"] = edited
        state["final_review_completion_ref"] = final_ref
        state["final_residual_risks"] = self._latest_final_residual_risks(final_artifacts)

    async def _prepare(self, state: dict) -> None:
        # Preparation is immutable inside one run. A resumed run must never
        # silently ingest a newer version of Inputs.
        if state.get("resume"):
            self._restore_preparation_snapshot(state)
        else:
            # These are deterministic data transformations, deliberately not LLM personas.
            await self.service._build_manifest(state)
            await self.service._parse_artifacts(state)
            await self.service._normalize_evidence(state)
            await self.service._evaluate_coverage(state)
            self._persist_preparation_snapshot(state)
        ledger = SourceLedger(self.service.workspace, state["run_id"])
        for item in state.get("evidence_items", []):
            ledger.register_project(
                item.id,
                item.subject,
                project_evidence_locator(item),
                item.model_dump_json(),
            )
        if not ledger.path.is_file():
            self.service.store.write_json(
                ledger.path.relative_to(self.service.workspace).as_posix(), []
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _preparation_refs(self, run_id: str) -> dict[str, str]:
        root = f"Work/runs/{run_id}/preparation"
        return {
            "manifest": f"{root}/manifest.json",
            "evidence": f"{root}/evidence.jsonl",
            "photo_manifest": f"{root}/photo-manifest.json",
            "mapping_gaps": f"{root}/mapping-gaps.json",
            "coverage": f"{root}/coverage.json",
        }

    def _persist_preparation_snapshot(self, state: dict) -> None:
        refs = self._preparation_refs(state["run_id"])
        self.service.store.write_json(
            refs["manifest"], state["project_manifest"].model_dump(mode="json")
        )
        self.service.store.write_jsonl(
            refs["evidence"],
            [item.model_dump(mode="json") for item in state["evidence_items"]],
        )
        self.service.store.write_json(
            refs["photo_manifest"],
            {"assets": [item.model_dump(mode="json") for item in state["photo_assets"]]},
        )
        self.service.store.write_json(refs["mapping_gaps"], {"gaps": state["mapping_gaps"]})
        self.service.store.write_json(
            refs["coverage"], state["coverage_matrix"].model_dump(mode="json")
        )
        state["preparation_refs"] = refs
        state["preparation_sha256"] = {
            name: self._sha256(self.service.workspace / ref) for name, ref in refs.items()
        }

    def _restore_preparation_snapshot(self, state: dict) -> None:
        refs = self._preparation_refs(state["run_id"])
        missing = [ref for ref in refs.values() if not (self.service.workspace / ref).is_file()]
        if missing:
            raise AgentWorkflowError(
                f"resume requires a complete immutable preparation snapshot; missing={missing}"
            )
        checkpoint_path = (
            self.service.workspace / f"Work/runs/{state['run_id']}/workflow-state.json"
        )
        checkpoint = FullReportCheckpoint.model_validate_json(
            checkpoint_path.read_text(encoding="utf-8")
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
        manifest_path = self.service.workspace / refs["manifest"]
        evidence_path = self.service.workspace / refs["evidence"]
        photo_path = self.service.workspace / refs["photo_manifest"]
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
        state["mapping_gaps"] = json.loads(gaps_path.read_text(encoding="utf-8")).get("gaps", [])
        state["coverage_matrix"] = CoverageMatrix.model_validate_json(
            coverage_path.read_text(encoding="utf-8")
        )
        state["preparation_refs"] = refs
        state["preparation_sha256"] = actual_hashes

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
        quality_context = knowledge.build_quality()
        state["module_knowledge_refs"] = {
            module_id: context.path.as_posix() for module_id, context in module_knowledge.items()
        }
        state["quality_context_ref"] = quality_context.path.as_posix()
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
                    module_knowledge[module_id].path.as_posix(),
                ],
                constraints=[
                    f"仅分析目标模块 {module_id}",
                    "inline_context 已注入项目 Knowledge 与 Template Distiller 产出的固定模板写作 Skill；按其分析语言、叙述节奏、推理链、建议方法和图证规则写作，不得重复打开同一内容",
                    "R-* 是优先参考而非认知边界；可使用模型世界知识解释机理、备选原因和行业实践，但不能把它补成客户事实",
                    "每个固定子模块必须形成带标题的完整正文，至少包含适用的现状、结论、风险机理和可执行建议",
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
                inline_context=(
                    module_knowledge[module_id].text
                    + "\n\n"
                    + self._template_skill_context(state, "core", "analysis", "visual", "rubric")
                ),
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
                "consumer": "module specialists, cross-module reviewer, chief editor",
                "input": "Work/report-template-writing/SKILL.md + progressive references",
                "output": "fixed Skill context attached to writing tasks",
                "content_checks": [
                    "semantic style, narrative, reasoning, synthesis, and visual guidance",
                    "no project facts copied from the template",
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
                "input": "ChiefEditorInput + ClaimLedger + SourceLedger",
                "output": "EditedReportSubmission + canonical Markdown",
                "content_checks": [
                    "exactly modules 2.1-2.5",
                    "all fixed submodule ids and titles retained",
                    "every approved submodule narrative is deterministically preserved verbatim",
                    "cross_module_analysis connects at least four modules with causal links and joint verification",
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
                    "all fixed final sections checked",
                    "approved module prose and Claim semantics retained",
                    "cross-module conclusions implemented without contradiction",
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
            return ["缺少客户证据的内容必须明确标注待核实或不确定性，禁止写成已确认项目事实"]
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

    async def _module_pipeline(
        self, module_id: str, state: dict, workflow_id: str
    ) -> ModuleSubmission:
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item for item in state["module_dispatch"].module_tasks if item.agent_id == specialist_id
        )
        resumed_payload = state.get("specialist_submissions", {}).get(module_id)
        revision = resumed_payload.revision if resumed_payload is not None else 0
        if state.get("resume") and resumed_payload is None:
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

        resume_part_constraints: list[str] = []
        resume_allowed_tools: list[str] = []
        saved_parts: list[str] = []
        rewrite_part_ids: list[str] = []
        base_constraints = list(
            dict.fromkeys(
                [
                    *planned.constraints,
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
        if state.get("resume"):
            if revision > 0:
                self._inherit_module_result_parts(
                    state["run_id"], module_id, revision - 1, revision
                )
            draft_root = self.service.workspace / (
                f"Work/runs/{state['run_id']}/drafts/module-{module_id}/r{revision}"
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
                if not binding_ready or "[[CLAIM:" in part_path.read_text(encoding="utf-8"):
                    rewrite_part_ids.append(part_id)
            resume_part_constraints = [
                "这是同一 run 的恢复任务；已有正文分段=" + (", ".join(saved_parts) or "无"),
                "固定 taxonomy 尚缺正文分段=" + (", ".join(missing_parts) or "无"),
                "当前协议只接收 write_result_part 保存的读者可见正文和 evidence_ids。",
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
        envelope = TaskEnvelope.model_validate(
            planned.model_copy(
                update={
                    "task_id": f"module-{module_id}",
                    "run_id": state["run_id"],
                    "agent_id": specialist_id,
                    "allowed_outputs": ["module_submission"],
                    "allowed_tools": resume_allowed_tools,
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
                        module_input.knowledge_ref,
                    ],
                    "input_contract_kind": "module_authoring_input",
                    "input_contract_ref": module_input_ref,
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
            self._checkpoint(state, "module-work", "in_progress")

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
        self.service.store.write_json(claim_ledger_ref, claim_ledger.model_dump(mode="json"))
        quality_context_ref = state.get("quality_context_ref")
        if quality_context_ref is None:
            candidate = Path(f"Work/runs/{state['run_id']}/context/report-quality-criteria.md")
            if (self.service.workspace / candidate).is_file():
                quality_context_ref = candidate.as_posix()
        self._load_template_skill(state)
        quality_context_text = ""
        if quality_context_ref and (self.service.workspace / quality_context_ref).is_file():
            quality_context_text = (self.service.workspace / quality_context_ref).read_text(
                encoding="utf-8"
            )
        editor_input = ChiefEditorInput(
            run_id=state["run_id"],
            approved_module_markers={
                module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_MODULE_IDS
            },
            modules={
                module_id: module_content_view(state["module_submissions"][module_id])
                for module_id in REPORT_MODULE_IDS
            },
            cross_synthesis_inputs=state["cross_synthesis_inputs"],
            cross_review_completion_ref=state["cross_review_completion_ref"],
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
                claim_ledger_ref,
                f"Work/runs/{state['run_id']}/ledgers/sources.json",
                state["preparation_refs"]["evidence"],
                state["preparation_refs"]["photo_manifest"],
            ],
            constraints=[
                "不得改变批准事实、数值、风险等级和来源语义",
                "批准正文的引用与脚注由运行时保护和装配，总编只提交 schema 声明字段",
                "正文不得套用统一的事实-证据-风险模板",
                "tables 只提交可追溯的 E-* evidence_ids；内部绑定由运行时推导，photo_ids 只选项目资产",
                "每个 module_narrative 必须逐一保留该模块全部固定 submodule_id 和标题，不得压缩为核心发现摘要",
                "每个已批准子模块正文必须原样包含在所属 module_narrative 中；总编只能增加章节引言、过渡、交叉引用和综合判断，不能删除或缩写专家正文",
                "为避免重复输出和截断，每个 module_narrative 使用对应 [[APPROVED_MODULE:2.x]] 标记作为正文基线，可在标记前后增加短过渡；工作流会确定性嵌回批准正文",
                f"{editor_input_ref} 是唯一模块正文与跨模块审查读取入口；一次完整读取，不得再打开 Outputs/Modules 或 Outputs/Reviews 重读",
                "必须对 chief-editor-input 中每个 Cross synthesis_input 恰好提交一个 synthesis_disposition；只能 integrated，或明确 merged 到另一条 integrated 输入",
                "每个 disposition 的 target_section_ids 只能取 Cross 授权章节，result_part_refs 必须指向本次 chief task 对应章节的 write_result_part 结果",
                "必须提交 risk_cluster_matrix 与 action_dependency_matrix 两类 synthesis_tables；每行用 row_synthesis_input_ids 绑定其精确 Cross 输入，所有表合计覆盖全部输入",
                "cross_module_analysis 必须落实全部 Cross synthesis_inputs 的因果/风险传播链，并提出有依赖顺序和验收方式的联合建议，不能只满足固定条数",
                "按四大块固定结构分别提交 assessment_background、findings_overview、regional_executive_summary、risk_panorama、dimension_risk_analysis、cross_module_analysis、data_gap_analysis、improvement_action_plan、new_factory_planning、capacity_expansion_plan、daily_power_management、emergency_compliance_management；只写各节正文，禁止自带章节标题",
                "十二个综合章节必须分别使用同名 part_id 的 write_result_part 持久化",
                "任何综合节都必须自足地包含归纳事实、综合判断和决策含义；模块号只能用于句末追溯，禁止用‘详见第二章’‘见2.x’或模块编号清单代替分析",
                "regional_executive_summary 必须按真实区域或责任边界归纳重点、优先行动与验证状态；没有区域划分证据时必须明确边界，禁止编造区域名称",
                "dimension_risk_analysis 必须比较五个维度的主导风险、相互放大和决策含义；data_gap_analysis 必须归并重复缺口并说明结论影响与补证优先级；improvement_action_plan 必须按整改依赖给出责任接口、动作、验收指标和剩余风险",
                "专项问题分析四节必须分别形成新建规划、增容决策、日常用电管理、应急与合规管理的自足分析；禁止迁移专家优化版的具体项目内容",
                *self._user_supplement_constraints(
                    state,
                    stage="chief_edit",
                    target_ids={
                        *REPORT_MODULE_IDS,
                    },
                ),
                "risk_panorama 必须按共同根因和传播能力组织风险簇，不得重复五章摘要",
                "图片选择必须服务于问题证明并依 Claim 对应子模块就近组织；同类多图形成图证组，不得统一堆到模块末尾",
                "质量参考文件只用于结构和写作质量检查，不得据此创造客户事实",
                "项目 Knowledge 是优先参考而非认知边界；可使用模型世界知识解释机制、备选原因、方案权衡和行业实践，但必须与客户事实明确区分",
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
            context_summary_refs=self._context_refs(state, "chief-editor"),
            inline_context="\n\n".join(
                text
                for text in (
                    quality_context_text,
                    self._template_skill_context(
                        state, "core", "analysis", "synthesis", "visual", "rubric"
                    ),
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
            "3.1.3": edited.cross_module_analysis,
            "3.1.4": edited.data_gap_analysis,
            "3.2": edited.improvement_action_plan,
            "4.1": edited.new_factory_planning,
            "4.2": edited.capacity_expansion_plan,
            "4.3": edited.daily_power_management,
            "4.4": edited.emergency_compliance_management,
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
            "cross_module_analysis",
            "data_gap_analysis",
            "improvement_action_plan",
            "new_factory_planning",
            "capacity_expansion_plan",
            "daily_power_management",
            "emergency_compliance_management",
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
            run_id=state["run_id"],
            subject_ref=(
                f"Work/runs/{state['run_id']}/modules/{module.module_id}-r{module.revision}.json"
            ),
            validator="module-structure/v1",
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
        try:
            signals = validate_final_report_markdown(markdown)
        except ValueError as exc:
            self.service.store.write_json(
                validation_ref,
                ValidationReport(
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    validator="final-report-structure/v1",
                    check_ids=["final_report.fixed_sections_and_markdown"],
                    failures=[
                        ValidationFailure(
                            check_id="final_report.fixed_sections_and_markdown",
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
                run_id=state["run_id"],
                subject_ref=subject_ref,
                validator="final-report-structure/v1",
                check_ids=["final_report.fixed_sections_and_markdown"],
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

    def _deliver(self, state: dict) -> None:
        if "final_review_completion_ref" not in state:
            raise AgentWorkflowError(
                "delivery requires an independent final review completion record"
            )
        self._validate_module_exports(state, "delivery")
        self._write_handoff_contracts(state)
        edited: EditedReportSubmission = state["edited_report"]
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
            cross_module_analysis=edited.cross_module_analysis,
            risk_panorama=edited.risk_panorama,
            dimension_risk_analysis=edited.dimension_risk_analysis,
            data_gap_analysis=edited.data_gap_analysis,
            improvement_action_plan=edited.improvement_action_plan,
            new_factory_planning=edited.new_factory_planning,
            capacity_expansion_plan=edited.capacity_expansion_plan,
            daily_power_management=edited.daily_power_management,
            emergency_compliance_management=edited.emergency_compliance_management,
            ledger=ledger,
            tables=tables,
            photos=photos,
        )
        self.service.store.write_json("Work/report-state.json", report.model_dump(mode="json"))
        canonical_markdown = PdsDocxRenderer._compose_markdown(report)
        self._validate_final_report_structure(state, canonical_markdown, "delivery-final")
        delivery_markdown = ledger.bind_citations(canonical_markdown)
        markdown_path = self.service.store.write_text(
            "Outputs/Reports/配电安全专家咨询报告.md", delivery_markdown
        )
        selected_template, template_source = self.service.resolve_report_template()
        template_snapshot = (
            self.service.workspace / f"Work/runs/{state['run_id']}/templates/report_template.docx"
        )
        template_snapshot.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(selected_template, template_snapshot)
        template_sha256 = hashlib.sha256(template_snapshot.read_bytes()).hexdigest()
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
        version = ReportVersionStore(self.service.workspace).publish(
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
                    "render_request": Path(f"Work/runs/{state['run_id']}/render-request.json"),
                    "render_result": render_result_ref,
                    "handoff_contracts": Path(
                        f"Work/runs/{state['run_id']}/handoff-contracts.json"
                    ),
                    "final_review_completion": Path(state["final_review_completion_ref"]),
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
            )
        )
        state["report_version"] = version
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
            OutputArtifact(
                kind="run",
                path=delivery_manifest_ref,
            ),
        ]

    @staticmethod
    def _canonical_markdown(edited: EditedReportSubmission) -> str:
        """Adapt approved synthesis to the fixed four-block Render contract."""

        def section_body(value: str) -> str:
            """Keep prose and tables while the workflow exclusively owns headings."""

            return "\n".join(
                line
                for line in value.splitlines()
                if not re.match(r"^#{1,6}\s+", line.strip())
                and line.strip() not in {"---", "***", "___"}
            ).strip()

        sections = [
            f"# {edited.title}",
            "",
            "## 1. 配电评估概述",
            "",
            "### 1.1 评估背景",
            "",
            section_body(edited.assessment_background),
            "",
            "### 1.2 健康度总览",
            "",
            section_body(edited.findings_overview),
            "",
            "### 1.3 各区域执行摘要",
            "",
            section_body(edited.regional_executive_summary),
            "",
            "## 2. 评估内容描述",
        ]
        for module_id in REPORT_MODULE_IDS:
            definition = REPORT_TAXONOMY[module_id]
            sections.extend(
                [
                    "",
                    f"### {module_id} {definition.title}",
                    "",
                    PdsDocxRenderer._strip_leading_module_heading(
                        edited.module_narratives[module_id], module_id
                    ).strip(),
                ]
            )
        sections.extend(
            [
                "",
                "## 3. 结论与建议",
                "",
                "### 3.1 风险/问题汇总与概览",
                "",
                "#### 3.1.1 风险全景图",
                "",
                section_body(edited.risk_panorama),
                "",
                "#### 3.1.2 各维度风险分析",
                "",
                section_body(edited.dimension_risk_analysis),
                "",
                "#### 3.1.3 跨领域关联风险",
                "",
                section_body(edited.cross_module_analysis),
                "",
                "#### 3.1.4 数据缺口分析",
                "",
                section_body(edited.data_gap_analysis),
                "",
                "### 3.2 改善行动速查表",
                "",
                section_body(edited.improvement_action_plan),
                "",
                "## 4. 专项问题分析",
                "",
                "### 4.1 新工厂建厂时规划建议",
                "",
                section_body(edited.new_factory_planning),
                "",
                "### 4.2 增容建议",
                "",
                section_body(edited.capacity_expansion_plan),
                "",
                "### 4.3 日常用电管理建议",
                "",
                section_body(edited.daily_power_management),
                "",
                "### 4.4 应急管理及合规性管理建议",
                "",
                section_body(edited.emergency_compliance_management),
                "",
            ]
        )
        if edited.tables:
            for table in edited.tables:
                sections.extend(["", table.title, ""])
                sections.append("| " + " | ".join(table.headers) + " |")
                sections.append("| " + " | ".join("---" for _ in table.headers) + " |")
                sections.extend("| " + " | ".join(row) + " |" for row in table.rows)
        return "\n".join(sections)
