"""Outer report workflow: orchestration only; professional reasoning stays in AgentLoop."""

from __future__ import annotations

import asyncio
import hashlib
import shutil
from pathlib import Path
from typing import TYPE_CHECKING
from uuid import uuid4

from .agent_runner import ReportingAgentRunner
from .agentic_models import (
    AgentRunStatus,
    AuditSubmission,
    CrossReviewSubmission,
    EditedReportSubmission,
    ModuleSubmission,
    PlanSubmission,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .assets import ReportAssetAssembler, validate_editor_protection
from .claim_ledger import ClaimLedger
from .delivery import DeliveryPackage, ProjectDelivery
from .models import (
    REPORT_MODULE_IDS,
    OutputArtifact,
    RevisionRequest,
    ScopeExpansionRequest,
)
from .module_skills import ModuleSkillLibrary
from .rendering.packaged_docx import PackagedDocxCore
from .rendering.pds_docx_renderer import ApprovedReport, PdsDocxRenderer
from .request_gate import ReportingBlockedError, RequestGate
from .research.project_evidence import project_evidence_locator
from .review_gate import ReviewGate
from .revision_diff import build_revision_diff
from .session_summary import SessionSummaryStore
from .source_ledger import SourceLedger
from .taxonomy import REPORT_TAXONOMY, resolve_submodule
from .versions import ReportVersion, ReportVersionStore, SkillProvenance

if TYPE_CHECKING:
    from .service import ReportingService


class AgentWorkflowError(RuntimeError):
    pass


class ReportingNeedsDecisionError(RuntimeError):
    """The lead Agent decided that safe autonomous progress is no longer useful."""


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
            "report-planner",
            "evidence-auditor",
            "cross-module-reviewer",
            "chief-editor",
            *(f"module-{module_id}-specialist" for module_id in REPORT_MODULE_IDS),
        }
        missing = sorted(required - set(self.agents))
        if missing:
            raise AgentWorkflowError(f"reporting Agent identities are missing: {missing}")

    async def _agent(
        self,
        agent_id: str,
        envelope: TaskEnvelope,
        artifacts: list[str],
        workflow_id: str,
        *,
        session_key: str | None = None,
    ):
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
        if result.status is not AgentRunStatus.COMPLETED:
            raise AgentWorkflowError(
                f"{agent_id} ended as {result.status.value}: {result.reason or 'no reason'}"
            )
        return result.payload

    async def run(self, state: dict) -> None:
        run_id = state["run_id"]
        workflow_id = f"full-power-distribution-report:{run_id}"
        activity = "preparation"
        try:
            await self.service._notice("正在整理项目资料并建立可追溯证据入口。")
            await self._prepare(state)
            gate = RequestGate.evaluate(state["request"], state["coverage_matrix"])
            state["gate_decision"] = gate
            if not gate.proceed:
                self._checkpoint(state, activity, "blocked", ", ".join(gate.missing_evidence))
                raise ReportingBlockedError(gate.missing_evidence, gate.affected_modules)
            self._checkpoint(state, activity, "completed")
            activity = "planning"
            await self.service._notice("资料入口已建立，Planner 正在拆分五个专业任务。")
            plan = await self._plan(state, workflow_id)
            state["plan_submission"] = plan
            self._checkpoint(state, activity, "completed")
            activity = "module-work"
            await self.service._notice("五个专业模块已并行启动，各模块完成后立即进入独立审计。")
            requested_modules = tuple(state["request"].target_modules)
            outcomes = await asyncio.gather(
                *(
                    self._module_pipeline(module_id, state, workflow_id)
                    for module_id in requested_modules
                ),
                return_exceptions=True,
            )
            submissions = [item for item in outcomes if isinstance(item, ModuleSubmission)]
            state["module_submissions"] = {item.module_id: item for item in submissions}
            self._checkpoint(
                state,
                activity,
                "completed" if len(submissions) == len(requested_modules) else "failed",
            )
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            if failures:
                needs_decision = next(
                    (item for item in failures if isinstance(item, ReportingNeedsDecisionError)),
                    None,
                )
                if needs_decision is not None:
                    raise needs_decision
                raise AgentWorkflowError(
                    "module pipelines failed after preserving successful results: "
                    + "; ".join(str(item) for item in failures)
                )
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
                await self.service._notice("目标模块已完成并通过独立证据审计。")
                return
            activity = "cross-module-review"
            await self.service._notice("五个模块均已通过本地审计，开始跨模块一致性审查。")
            await self._cross_review(state, workflow_id)
            self._checkpoint(state, activity, "completed")
            activity = "chief-edit"
            await self.service._notice("跨模块审查通过，总编正在整合全文并保护来源语义。")
            await self._chief_edit(state, workflow_id)
            self._checkpoint(state, activity, "completed")
            activity = "delivery"
            await self.service._notice("正文与引用已批准，正在使用交接包渲染核心生成 DOCX。")
            self._deliver(state)
            self._checkpoint(state, activity, "completed")
        except ReportingBlockedError:
            raise
        except asyncio.CancelledError:
            self._checkpoint(state, activity, "cancelled", "interrupted by user")
            raise
        except Exception as exc:
            self._checkpoint(state, activity, "failed", str(exc))
            raise
        finally:
            await self.agent_runner.close_workflow(workflow_id)

    async def run_revision(
        self,
        state: dict,
        request: RevisionRequest,
        baseline_edited: EditedReportSubmission,
    ) -> None:
        workflow_id = f"report-revision:{state['run_id']}"
        try:
            await self.service._notice(
                f"正在从报告版本 {request.baseline_version_id} 恢复结构化状态并执行局部修订。"
            )
            for module_id in REPORT_MODULE_IDS:
                submission = state["module_submissions"][module_id]
                self.service.store.write_json(
                    f"Work/runs/{state['run_id']}/modules/{module_id}-r{submission.revision}.json",
                    submission.model_dump(mode="json"),
                )
                self.service.store.write_text(
                    f"Outputs/Modules/{module_id}.md", submission.markdown
                )
            for module_id in request.target_module_ids:
                await self._post_delivery_module_revision(module_id, state, request, workflow_id)
            await self._cross_review(state, workflow_id)
            state["chief_editor_constraints"] = [
                "这是交付后局部修订：未获批准的模块正文必须逐字保持父版本内容",
                "可更新目标模块以及受其影响的 overview 与 conclusion",
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
            self._deliver(state)
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
        specialist_id = f"module-{module_id}-specialist"
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
        feedback_ref = f"Work/runs/{state['run_id']}/revision-request.json"
        prior_ref = state["baseline_module_refs"][module_id]
        issue_refs = [feedback_ref]
        revision = current.revision + 1
        decision_history: list[str] = [feedback_ref]
        while True:
            envelope = TaskEnvelope(
                task_id=f"module-{module_id}-post-delivery-r{revision}",
                run_id=state["run_id"],
                agent_id=specialist_id,
                objective=f"根据交付后反馈局部修订模块 {module_id}：{request.feedback}",
                input_refs=[prior_ref, feedback_ref, f"Work/runs/{state['run_id']}/evidence.jsonl"],
                constraints=[
                    "只修改明确授权的小节和相关 Claim；其他结构化内容保持父版本不变",
                    "会影响其他范围的必要变化必须作为 scope expansion 提出",
                ],
                allowed_outputs=["module_submission"],
                revision=revision,
                prior_result_ref=prior_ref,
                issue_refs=issue_refs,
                context_summary_refs=self._context_refs(state, specialist_id),
                target_submodule_ids=sorted(authorized_submodules),
            )
            payload = await self._agent(
                specialist_id,
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key=f"post-delivery-{module_id}",
            )
            if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
                raise AgentWorkflowError(
                    f"post-delivery revision returned wrong module {module_id}"
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
            diff_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/post-delivery-diff-{module_id}-r{revision}.json",
                diff,
            )
            draft_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
                payload.model_dump(mode="json"),
            )
            audit_envelope = TaskEnvelope(
                task_id=f"audit-{module_id}-post-delivery-r{revision}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=f"复核模块 {module_id} 的交付后局部修订及证据边界。",
                input_refs=[
                    draft_path.relative_to(self.service.workspace).as_posix(),
                    diff_path.relative_to(self.service.workspace).as_posix(),
                    f"Work/runs/{state['run_id']}/ledgers/sources.json",
                ],
                constraints=["验证反馈是否被准确处理且未破坏父版本证据边界"],
                allowed_outputs=["audit_submission"],
                revision=revision,
                context_summary_refs=self._context_refs(state, f"evidence-auditor:{module_id}"),
                target_submodule_ids=sorted(authorized_submodules),
            )
            audit = await self._agent(
                "evidence-auditor",
                audit_envelope,
                audit_envelope.input_refs,
                workflow_id,
                session_key=f"post-delivery-auditor-{module_id}",
            )
            if not isinstance(audit, AuditSubmission) or audit.module_id != module_id:
                raise AgentWorkflowError(f"post-delivery audit returned wrong module {module_id}")
            audit_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/post-delivery-audit-{module_id}-r{revision}.json",
                audit.model_dump(mode="json"),
            )
            if audit.approved and ReviewGate.evaluate(audit.issues).clear:
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                state["module_submissions"][module_id] = payload
                return
            audit_ref = audit_path.relative_to(self.service.workspace).as_posix()
            decision_history.extend(
                [diff_path.relative_to(self.service.workspace).as_posix(), audit_ref]
            )
            decision = await self._lead_decision(
                scope=f"post-delivery-{module_id}",
                state=state,
                workflow_id=workflow_id,
                review_ref=audit_ref,
                history_refs=decision_history,
                available_modules=[module_id],
            )
            if decision.decision == "accept":
                ReviewGate.ensure_decision_allowed(decision, audit.issues)
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                state["module_submissions"][module_id] = payload
                return
            if decision.decision in {"request_user", "stop_incomplete"}:
                raise ReportingNeedsDecisionError(decision.rationale)
            selected = set(decision.target_submodule_ids)
            outside = sorted(selected - authorized_submodules)
            if outside:
                self._raise_scope_expansion(
                    state,
                    request,
                    unexpected_submodule_ids=outside,
                    reason="Main Agent 建议的后续修订超出已批准范围，需要用户确认扩大范围。",
                )
            current = payload
            prior_ref = draft_path.relative_to(self.service.workspace).as_posix()
            issue_refs = [audit_ref]
            revision += 1

    def _checkpoint(
        self, state: dict, activity: str, status: str, error: str | None = None
    ) -> None:
        """Persist recoverable workflow/output references without serializing sessions."""
        completed_modules = sorted(state.get("module_submissions", {}))
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            {
                "workflow_id": f"full-power-distribution-report:{state['run_id']}",
                "run_id": state["run_id"],
                "activity": activity,
                "status": status,
                "completed_modules": completed_modules,
                "plan_ref": (
                    f"Work/runs/{state['run_id']}/results/report-plan.json"
                    if "plan_submission" in state
                    else None
                ),
                "report_state_ref": "Work/report-state.json" if "edited_report" in state else None,
                "error": error,
            },
        )

    async def _prepare(self, state: dict) -> None:
        # These are deterministic data transformations, deliberately not LLM personas.
        await self.service._build_manifest(state)
        await self.service._parse_artifacts(state)
        await self.service._normalize_evidence(state)
        await self.service._evaluate_coverage(state)
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

    async def _plan(self, state: dict, workflow_id: str) -> PlanSubmission:
        request = state["request"]
        requested_modules = tuple(request.target_modules)
        module_text = "、".join(requested_modules)
        task = TaskEnvelope(
            task_id="report-plan",
            run_id=state["run_id"],
            agent_id="report-planner",
            objective=(
                f"为配电安全专家报告规划目标模块：{module_text}。用户要求：{request.instruction}"
            ),
            input_refs=["Work/coverage.json", "Work/evidence.jsonl", "Work/manifest.json"],
            constraints=[
                f"仅为目标模块 {module_text} 形成独立 TaskEnvelope",
                "Knowledge 与网络只是可选参考，不能补成客户事实",
                f"缺失证据策略={request.missing_evidence_policy}",
                *self._evidence_policy_constraints(request.missing_evidence_policy),
                *request.execution_requirements,
            ],
            allowed_outputs=["plan_submission"],
        )
        payload = await self._agent(
            "report-planner",
            task,
            task.input_refs,
            workflow_id,
            session_key="planner",
        )
        if not isinstance(payload, PlanSubmission):
            raise AgentWorkflowError("report-planner returned the wrong payload type")
        planned = {item.agent_id for item in payload.module_tasks}
        required = {f"module-{module_id}-specialist" for module_id in requested_modules}
        if planned != required:
            raise AgentWorkflowError(
                "planner must assign exactly one task to every requested module"
            )
        return payload

    @staticmethod
    def _evidence_policy_constraints(policy: str) -> list[str]:
        if policy == "draft":
            return ["缺少客户证据的内容必须明确标注待核实或不确定性，禁止写成已确认项目事实"]
        if policy == "skip":
            return ["必须保留固定报告目录；缺少客户证据的子模块仅标注“未评估”，不得给出专业结论"]
        return []

    async def _module_pipeline(
        self, module_id: str, state: dict, workflow_id: str
    ) -> ModuleSubmission:
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item for item in state["plan_submission"].module_tasks if item.agent_id == specialist_id
        )
        task_id = f"module-{module_id}"
        envelope = planned.model_copy(
            update={
                "task_id": task_id,
                "run_id": state["run_id"],
                "agent_id": specialist_id,
                "allowed_outputs": ["module_submission"],
                "target_submodule_ids": list(REPORT_TAXONOMY[module_id].submodules),
                "constraints": list(
                    dict.fromkeys(
                        [
                            *planned.constraints,
                            *state["request"].execution_requirements,
                            f"缺失证据策略={state['request'].missing_evidence_policy}",
                            *self._evidence_policy_constraints(
                                state["request"].missing_evidence_policy
                            ),
                        ]
                    )
                ),
            }
        )

        artifacts = [
            "Work/evidence.jsonl",
            "Work/coverage.json",
            "Work/runs/%s/ledgers/sources.json" % state["run_id"],
        ]
        previous_payload: ModuleSubmission | None = None
        decision_history: list[str] = []
        revision = 0
        while True:
            current = envelope.model_copy(update={"revision": revision})
            payload = await self._agent(
                specialist_id,
                current,
                artifacts,
                workflow_id,
                session_key=f"specialist-{module_id}",
            )
            if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
                raise AgentWorkflowError(f"{specialist_id} returned the wrong module payload")
            if previous_payload is not None:
                diff_path = self.service.store.write_json(
                    f"Work/runs/{state['run_id']}/reviews/diff-{module_id}-r{revision}.json",
                    build_revision_diff(previous_payload, payload),
                )
                decision_history.append(diff_path.relative_to(self.service.workspace).as_posix())
            draft_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
                payload.model_dump(mode="json"),
            )
            audit_task = TaskEnvelope(
                task_id=f"audit-{module_id}-r{revision}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=f"独立审查模块 {module_id} 的事实、判断、来源和可判断边界。",
                input_refs=[draft_path.relative_to(self.service.workspace).as_posix(), *artifacts],
                constraints=["不得代替责任专家重写专业结论"],
                allowed_outputs=["audit_submission"],
                revision=revision,
                target_submodule_ids=list(current.target_submodule_ids),
            )
            audit = await self._agent(
                "evidence-auditor",
                audit_task,
                audit_task.input_refs,
                workflow_id,
                session_key=f"auditor-{module_id}",
            )
            if not isinstance(audit, AuditSubmission) or audit.module_id != module_id:
                raise AgentWorkflowError("evidence-auditor returned the wrong payload type")
            self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/audit-{module_id}-r{revision}.json",
                audit.model_dump(mode="json"),
            )
            audit_gate = ReviewGate.evaluate(audit.issues)
            if audit.approved and audit_gate.clear:
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                await self.service._notice(f"模块 {module_id} 已通过独立证据审计。")
                return payload
            issue_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/issues-{module_id}-r{revision}.json",
                {"issues": [issue.model_dump(mode="json") for issue in audit.issues]},
            )
            issue_ref = issue_path.relative_to(self.service.workspace).as_posix()
            decision_history.append(issue_ref)
            decision = await self._lead_decision(
                scope=f"module-{module_id}",
                state=state,
                workflow_id=workflow_id,
                review_ref=issue_ref,
                history_refs=decision_history,
                available_modules=[module_id],
            )
            if decision.decision == "accept":
                ReviewGate.ensure_decision_allowed(decision, audit.issues)
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                return payload
            if decision.decision in {"request_user", "stop_incomplete"}:
                raise ReportingNeedsDecisionError(decision.rationale)
            target_submodules = [
                item
                for item in decision.target_submodule_ids
                if resolve_submodule(item).module_id == module_id
            ]
            envelope = envelope.model_copy(
                update={
                    "prior_result_ref": draft_path.relative_to(self.service.workspace).as_posix(),
                    "issue_refs": [issue_ref],
                    "target_submodule_ids": target_submodules,
                }
            )
            previous_payload = payload
            revision += 1

    async def _lead_decision(
        self,
        *,
        scope: str,
        state: dict,
        workflow_id: str,
        review_ref: str,
        history_refs: list[str],
        available_modules: list[str],
    ) -> WorkflowDecisionSubmission:
        decision_index = sum(1 for ref in history_refs if "/decisions/" in ref)
        envelope = TaskEnvelope(
            task_id=f"decision-{scope}-r{decision_index}",
            run_id=state["run_id"],
            agent_id="main-agent",
            objective=(
                "根据当前审计与完整历史判断是否继续返工。识别重复问题、没有新证据的循环"
                "以及真实收敛；不要依据预设轮数作决定。"
            ),
            input_refs=[review_ref, *history_refs],
            constraints=[
                f"可选择的模块：{', '.join(available_modules)}",
                "revise 必须明确 target_module_ids；小节范围不明确时可以留空并交由责任专家整体处理",
                "request_user 或 stop_incomplete 必须说明无法继续自主推进的具体原因",
            ],
            allowed_outputs=["workflow_decision_submission"],
        )
        payload = await self._agent(
            "main-agent",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=f"lead-{scope}",
        )
        if not isinstance(payload, WorkflowDecisionSubmission):
            raise AgentWorkflowError("main-agent returned the wrong decision payload")
        if any(module_id not in available_modules for module_id in payload.target_module_ids):
            raise AgentWorkflowError("main-agent selected a module outside the available scope")
        decision_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/decisions/{scope}-r{decision_index}.json",
            payload.model_dump(mode="json"),
        )
        history_refs.append(decision_path.relative_to(self.service.workspace).as_posix())
        return payload

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        prior_review: str | None = None
        decision_history: list[str] = []
        review_round = 0
        while True:
            refs = [
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{state['module_submissions'][module_id].revision}.json"
                for module_id in REPORT_MODULE_IDS
            ]
            envelope = TaskEnvelope(
                task_id="cross-module-review",
                run_id=state["run_id"],
                agent_id="cross-module-reviewer",
                objective="在五条模块 Pipeline 全部通过后进行跨模块一致性、风险链和行动优先级审查。",
                input_refs=refs + [f"Work/runs/{state['run_id']}/ledgers/sources.json"],
                constraints=["只向责任模块提出定向问题，不统一各模块的论证形态"],
                allowed_outputs=["cross_review_submission"],
                revision=review_round,
                prior_result_ref=prior_review,
                context_summary_refs=self._context_refs(state, "cross-module-reviewer"),
            )
            payload = await self._agent(
                "cross-module-reviewer",
                envelope,
                envelope.input_refs,
                workflow_id,
                session_key="cross-reviewer",
            )
            if not isinstance(payload, CrossReviewSubmission):
                raise AgentWorkflowError("cross-module-reviewer returned the wrong payload type")
            review_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/cross-r{review_round}.json",
                payload.model_dump(mode="json"),
            )
            prior_review = review_path.relative_to(self.service.workspace).as_posix()
            review_gate = ReviewGate.evaluate(payload.issues)
            blocking = [
                issue
                for issue in payload.issues
                if issue.severity == "blocking" and issue.status == "open"
            ]
            if payload.approved and review_gate.clear:
                self.service.store.write_json(
                    "Outputs/Reviews/full-review.json", payload.model_dump(mode="json")
                )
                state["cross_review"] = payload
                return
            review_ref = review_path.relative_to(self.service.workspace).as_posix()
            decision_history.append(review_ref)
            decision = await self._lead_decision(
                scope="cross-module",
                state=state,
                workflow_id=workflow_id,
                review_ref=review_ref,
                history_refs=decision_history,
                available_modules=list(REPORT_MODULE_IDS),
            )
            if decision.decision == "accept":
                ReviewGate.ensure_decision_allowed(decision, payload.issues)
                self.service.store.write_json(
                    "Outputs/Reviews/full-review.json", payload.model_dump(mode="json")
                )
                state["cross_review"] = payload
                return
            if decision.decision in {"request_user", "stop_incomplete"}:
                raise ReportingNeedsDecisionError(decision.rationale)
            modules = list(decision.target_module_ids)
            await asyncio.gather(
                *(
                    self._revise_cross_issue(
                        module_id,
                        blocking,
                        decision.target_submodule_ids,
                        state,
                        workflow_id,
                    )
                    for module_id in modules
                )
            )
            review_round += 1

    async def _revise_cross_issue(
        self,
        module_id: str,
        all_issues: list,
        selected_submodules: list[str],
        state: dict,
        workflow_id: str,
    ) -> None:
        current: ModuleSubmission = state["module_submissions"][module_id]
        revision = current.revision + 1
        issues = [issue for issue in all_issues if issue.module_id == module_id]
        target_submodules = {
            item for item in selected_submodules if resolve_submodule(item).module_id == module_id
        }
        if not target_submodules:
            target_submodules = {issue.submodule_id for issue in issues if issue.submodule_id}
        for submodule_id in target_submodules:
            if resolve_submodule(submodule_id).module_id != module_id:
                raise AgentWorkflowError(
                    f"cross-review issue targets {submodule_id} outside module {module_id}"
                )
        issue_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/reviews/cross-issues-{module_id}-r{revision}.json",
            {"issues": [issue.model_dump(mode="json") for issue in issues]},
        )
        planned = next(
            item
            for item in state["plan_submission"].module_tasks
            if item.agent_id == f"module-{module_id}-specialist"
        )
        initial_issue_ref = issue_path.relative_to(self.service.workspace).as_posix()
        decision_history = [initial_issue_ref]
        issue_refs = [initial_issue_ref]

        while True:
            previous = f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
            envelope = planned.model_copy(
                update={
                    "task_id": f"module-{module_id}",
                    "run_id": state["run_id"],
                    "agent_id": f"module-{module_id}-specialist",
                    "revision": revision,
                    "prior_result_ref": previous,
                    "issue_refs": issue_refs,
                    "allowed_outputs": ["module_submission"],
                    "target_submodule_ids": sorted(target_submodules),
                    "context_summary_refs": self._context_refs(
                        state, f"module-{module_id}-specialist"
                    ),
                }
            )
            payload = await self._agent(
                envelope.agent_id,
                envelope,
                [previous, *envelope.issue_refs, "Work/evidence.jsonl"],
                workflow_id,
                session_key=f"specialist-{module_id}",
            )
            if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
                raise AgentWorkflowError(f"targeted revision returned wrong module {module_id}")
            diff_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/diff-{module_id}-r{revision}.json",
                build_revision_diff(current, payload),
            )
            draft_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
                payload.model_dump(mode="json"),
            )
            audit_envelope = TaskEnvelope(
                task_id=f"audit-{module_id}-cross-r{revision}",
                run_id=state["run_id"],
                agent_id="evidence-auditor",
                objective=f"复核模块 {module_id} 的定向修订是否解决问题且未破坏证据边界。",
                input_refs=[
                    draft_path.relative_to(self.service.workspace).as_posix(),
                    diff_path.relative_to(self.service.workspace).as_posix(),
                    *envelope.issue_refs,
                    "Work/evidence.jsonl",
                ],
                allowed_outputs=["audit_submission"],
                revision=revision,
                target_submodule_ids=list(envelope.target_submodule_ids),
                context_summary_refs=self._context_refs(state, f"evidence-auditor:{module_id}"),
            )
            audit = await self._agent(
                "evidence-auditor",
                audit_envelope,
                audit_envelope.input_refs,
                workflow_id,
                session_key=f"auditor-{module_id}",
            )
            if not isinstance(audit, AuditSubmission) or audit.module_id != module_id:
                raise AgentWorkflowError(f"module {module_id} revision returned an invalid audit")
            self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/audit-{module_id}-cross-r{revision}.json",
                audit.model_dump(mode="json"),
            )
            audit_gate = ReviewGate.evaluate(audit.issues)
            if audit.approved and audit_gate.clear:
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                state["module_submissions"][module_id] = payload
                return

            audit_issue_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/"
                f"cross-audit-issues-{module_id}-r{revision}.json",
                {"issues": [issue.model_dump(mode="json") for issue in audit.issues]},
            )
            audit_issue_ref = audit_issue_path.relative_to(self.service.workspace).as_posix()
            decision_history.extend(
                [diff_path.relative_to(self.service.workspace).as_posix(), audit_issue_ref]
            )
            decision = await self._lead_decision(
                scope=f"cross-module-{module_id}",
                state=state,
                workflow_id=workflow_id,
                review_ref=audit_issue_ref,
                history_refs=decision_history,
                available_modules=[module_id],
            )
            if decision.decision == "accept":
                ReviewGate.ensure_decision_allowed(decision, audit.issues)
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                state["module_submissions"][module_id] = payload
                return
            if decision.decision in {"request_user", "stop_incomplete"}:
                raise ReportingNeedsDecisionError(decision.rationale)

            selected = {
                submodule_id
                for submodule_id in decision.target_submodule_ids
                if resolve_submodule(submodule_id).module_id == module_id
            }
            if selected:
                target_submodules = selected
            current = payload
            revision += 1
            issue_refs = [audit_issue_ref]

    async def _chief_edit(self, state: dict, workflow_id: str) -> None:
        envelope = TaskEnvelope(
            task_id="chief-edit",
            run_id=state["run_id"],
            agent_id="chief-editor",
            objective="整合已批准五模块，形成自然、丰富、有专业差异且可溯源的完整报告。",
            input_refs=[
                *(f"Outputs/Modules/{module_id}.md" for module_id in REPORT_MODULE_IDS),
                "Outputs/Reviews/full-review.json",
                f"Work/runs/{state['run_id']}/ledgers/sources.json",
                "Work/evidence.jsonl",
                "Work/photo-manifest.json",
            ],
            constraints=[
                "不得改变批准事实、数值、风险等级和来源语义",
                "citation_anchors 必须是正文中唯一出现的准确片段",
                "正文不得套用统一的事实-证据-风险模板",
                "tables 和 photo_ids 只能选择可追溯到 Evidence 与 Claim 的资产",
                *state.get("chief_editor_constraints", []),
            ],
            allowed_outputs=["edited_report_submission"],
            context_summary_refs=self._context_refs(state, "chief-editor"),
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
        claims = [
            claim
            for module_id in REPORT_MODULE_IDS
            for claim in state["module_submissions"][module_id].claims
        ]
        validate_editor_protection(payload, claims)
        state["edited_report"] = payload

    def _deliver(self, state: dict) -> None:
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
            overview=edited.overview,
            module_narratives=dict(edited.module_narratives),
            conclusion=edited.conclusion,
            ledger=ledger,
            citation_anchors=edited.citation_anchors,
            tables=tables,
            photos=photos,
        )
        self.service.store.write_json("Work/report-state.json", report.model_dump(mode="json"))
        selected_template, template_source = self.service.resolve_report_template()
        template_snapshot = (
            self.service.workspace
            / f"Work/runs/{state['run_id']}/templates/report_template.docx"
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
                "snapshot_path": template_snapshot.relative_to(
                    self.service.workspace
                ).as_posix(),
                "sha256": template_sha256,
            },
        )
        output = self.service.workspace / "Outputs/Reports/配电安全专家咨询报告.docx"
        render = PdsDocxRenderer(PackagedDocxCore(template_snapshot)).render(
            report, output
        )
        render_log = render.model_dump(mode="json")
        render_log["template"] = {
            "source": template_source,
            "selected_path": selected_template_ref,
            "sha256": template_sha256,
        }
        self.service.store.write_json("Outputs/Reports/render-log.json", render_log)
        receipt = ProjectDelivery(self.service.workspace / "Outputs/Deliveries").deliver(
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
                    "report_request": request_snapshot_path.relative_to(self.service.workspace),
                    "photo_manifest": photo_manifest_path.relative_to(self.service.workspace),
                    "report_template": template_snapshot.relative_to(self.service.workspace),
                    "template_provenance": template_provenance_path.relative_to(
                        self.service.workspace
                    ),
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
        state["output_artifacts"] = [
            *(
                OutputArtifact(
                    kind="module", path=Path(f"Outputs/Modules/{module_id}.md"), module_id=module_id
                )
                for module_id in REPORT_MODULE_IDS
            ),
            OutputArtifact(kind="review", path=Path("Outputs/Reviews/full-review.json")),
            OutputArtifact(kind="report", path=Path("Outputs/Reports/配电安全专家咨询报告.docx")),
            OutputArtifact(
                kind="run",
                path=receipt.manifest_path.relative_to(self.service.workspace),
            ),
        ]
