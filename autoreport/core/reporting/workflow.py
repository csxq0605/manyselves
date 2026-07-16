"""Outer report workflow: orchestration only; professional reasoning stays in AgentLoop."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING

from .agent_runner import ReportingAgentRunner
from .agentic_models import (
    AgentRunStatus,
    AuditSubmission,
    CrossReviewSubmission,
    EditedReportSubmission,
    ModuleSubmission,
    PlanSubmission,
    TaskEnvelope,
)
from .claim_ledger import ClaimLedger
from .delivery import DeliveryPackage, ProjectDelivery
from .models import REPORT_MODULE_IDS, OutputArtifact
from .rendering.handoff_docx import HandoffDocxCore
from .rendering.pds_docx_renderer import ApprovedReport, PdsDocxRenderer
from .source_ledger import SourceLedger

if TYPE_CHECKING:
    from .service import ReportingService


class AgentWorkflowError(RuntimeError):
    pass


class ReportWorkflowRunner:
    """Execute declared dependencies, parallel pipelines, barriers and local revisions."""

    def __init__(self, service: "ReportingService", agent_runner: ReportingAgentRunner):
        self.service = service
        self.agent_runner = agent_runner
        self.agents = service.agents

    async def _agent(
        self,
        agent_id: str,
        envelope: TaskEnvelope,
        artifacts: list[str],
        workflow_id: str,
        *,
        session_key: str | None = None,
    ):
        result = await self.agent_runner.run(
            self.agents[agent_id], envelope, artifacts,
            workflow_id=workflow_id, session_key=session_key,
        )
        if result.status is not AgentRunStatus.COMPLETED:
            raise AgentWorkflowError(
                f"{agent_id} ended as {result.status.value}: {result.reason or 'no reason'}"
            )
        return result.payload

    async def run(self, state: dict) -> None:
        run_id = state["run_id"]
        workflow_id = f"full-power-distribution-report:{run_id}"
        phase = "preparation"
        try:
            await self.service._notice("正在整理项目资料并建立可追溯证据入口。")
            await self._prepare(state)
            self._checkpoint(state, phase, "completed")
            phase = "planning"
            await self.service._notice("资料入口已建立，Planner 正在拆分五个专业任务。")
            plan = await self._plan(state, workflow_id)
            state["plan_submission"] = plan
            self._checkpoint(state, phase, "completed")
            phase = "module-pipelines"
            await self.service._notice("五个专业模块已并行启动，各模块完成后立即进入独立审计。")
            outcomes = await asyncio.gather(
                *(self._module_pipeline(module_id, state, workflow_id) for module_id in REPORT_MODULE_IDS),
                return_exceptions=True,
            )
            submissions = [item for item in outcomes if isinstance(item, ModuleSubmission)]
            state["module_submissions"] = {item.module_id: item for item in submissions}
            self._checkpoint(state, phase, "completed" if len(submissions) == 5 else "failed")
            failures = [item for item in outcomes if isinstance(item, BaseException)]
            if failures:
                raise AgentWorkflowError(
                    "module pipelines failed after preserving successful results: "
                    + "; ".join(str(item) for item in failures)
                )
            phase = "cross-module-review"
            await self.service._notice("五个模块均已通过本地审计，开始跨模块一致性审查。")
            await self._cross_review(state, workflow_id)
            self._checkpoint(state, phase, "completed")
            phase = "chief-edit"
            await self.service._notice("跨模块审查通过，总编正在整合全文并保护来源语义。")
            await self._chief_edit(state, workflow_id)
            self._checkpoint(state, phase, "completed")
            phase = "delivery"
            await self.service._notice("正文与引用已批准，正在使用交接包渲染核心生成 DOCX。")
            self._deliver(state)
            self._checkpoint(state, phase, "completed")
        except Exception as exc:
            self._checkpoint(state, phase, "failed", str(exc))
            raise
        finally:
            await self.agent_runner.close_workflow(workflow_id)

    def _checkpoint(
        self, state: dict, phase: str, status: str, error: str | None = None
    ) -> None:
        """Persist recoverable phase/output references without serializing sessions."""
        completed_modules = sorted(state.get("module_submissions", {}))
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/workflow-state.json",
            {
                "workflow_id": f"full-power-distribution-report:{state['run_id']}",
                "run_id": state["run_id"],
                "phase": phase,
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
            source = item.source
            parts = [source.path.as_posix()]
            if source.sheet:
                parts.append(f"工作表={source.sheet}")
            if source.cell:
                parts.append(f"单元格={source.cell}")
            if source.page:
                parts.append(f"页码={source.page}")
            ledger.register_project(item.id, item.subject, "；".join(parts), item.model_dump_json())

    async def _plan(self, state: dict, workflow_id: str) -> PlanSubmission:
        request = state["request"]
        task = TaskEnvelope(
            task_id="report-plan",
            run_id=state["run_id"],
            agent_id="report-planner",
            objective=(
                "为完整配电安全专家报告规划 2.1-2.5 五个模块。"
                f"用户要求：{request.instruction}"
            ),
            input_refs=["Work/coverage.json", "Work/evidence.jsonl", "Work/manifest.json"],
            constraints=[
                "五个模块都必须形成独立 TaskEnvelope",
                "Knowledge 与网络只是可选参考，不能补成客户事实",
                f"缺失证据策略={request.missing_evidence_policy}",
            ],
            allowed_outputs=["plan_submission"],
        )
        payload = await self._agent(
            "report-planner", task, task.input_refs, workflow_id,
            session_key="planner",
        )
        if not isinstance(payload, PlanSubmission):
            raise AgentWorkflowError("report-planner returned the wrong payload type")
        planned = {item.agent_id for item in payload.module_tasks}
        required = {f"module-{module_id}-specialist" for module_id in REPORT_MODULE_IDS}
        if planned != required:
            raise AgentWorkflowError("planner must assign exactly one task to every module specialist")
        return payload

    async def _module_pipeline(
        self, module_id: str, state: dict, workflow_id: str
    ) -> ModuleSubmission:
        specialist_id = f"module-{module_id}-specialist"
        planned = next(
            item for item in state["plan_submission"].module_tasks
            if item.agent_id == specialist_id
        )
        task_id = f"module-{module_id}"
        envelope = planned.model_copy(
            update={
                "task_id": task_id,
                "run_id": state["run_id"],
                "agent_id": specialist_id,
                "allowed_outputs": ["module_submission"],
            }
        )
        artifacts = ["Work/evidence.jsonl", "Work/coverage.json", "Work/runs/%s/ledgers/sources.json" % state["run_id"]]
        for revision in range(3):
            current = envelope.model_copy(update={"revision": revision})
            payload = await self._agent(
                specialist_id, current, artifacts, workflow_id,
                session_key=f"specialist-{module_id}",
            )
            if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
                raise AgentWorkflowError(f"{specialist_id} returned the wrong module payload")
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
            )
            audit = await self._agent(
                "evidence-auditor", audit_task, audit_task.input_refs, workflow_id,
                session_key=f"auditor-{module_id}",
            )
            if not isinstance(audit, AuditSubmission) or audit.module_id != module_id:
                raise AgentWorkflowError("evidence-auditor returned the wrong payload type")
            self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/audit-{module_id}-r{revision}.json",
                audit.model_dump(mode="json"),
            )
            if audit.approved and not any(issue.severity == "blocking" for issue in audit.issues):
                self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
                await self.service._notice(f"模块 {module_id} 已通过独立证据审计。")
                return payload
            if revision == 2:
                raise AgentWorkflowError(f"module {module_id} exceeded its local revision budget")
            issue_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/issues-{module_id}-r{revision}.json",
                {"issues": [issue.model_dump(mode="json") for issue in audit.issues]},
            )
            envelope = envelope.model_copy(
                update={
                    "prior_result_ref": draft_path.relative_to(self.service.workspace).as_posix(),
                    "issue_refs": [issue_path.relative_to(self.service.workspace).as_posix()],
                }
            )
        raise AssertionError("unreachable")

    async def _cross_review(self, state: dict, workflow_id: str) -> None:
        prior_review: str | None = None
        for review_round in range(3):
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
            )
            payload = await self._agent(
                "cross-module-reviewer", envelope, envelope.input_refs,
                workflow_id, session_key="cross-reviewer",
            )
            if not isinstance(payload, CrossReviewSubmission):
                raise AgentWorkflowError("cross-module-reviewer returned the wrong payload type")
            review_path = self.service.store.write_json(
                f"Work/runs/{state['run_id']}/reviews/cross-r{review_round}.json",
                payload.model_dump(mode="json"),
            )
            prior_review = review_path.relative_to(self.service.workspace).as_posix()
            blocking = [issue for issue in payload.issues if issue.severity == "blocking"]
            if payload.approved and not blocking:
                self.service.store.write_json(
                    "Outputs/Reviews/full-review.json", payload.model_dump(mode="json")
                )
                state["cross_review"] = payload
                return
            if review_round == 2:
                raise AgentWorkflowError("cross-module review exceeded its targeted revision budget")
            modules = sorted({issue.module_id for issue in blocking})
            if not modules:
                raise AgentWorkflowError("cross-module reviewer rejected without actionable issues")
            await asyncio.gather(
                *(self._revise_cross_issue(module_id, blocking, state, workflow_id) for module_id in modules)
            )

    async def _revise_cross_issue(
        self,
        module_id: str,
        all_issues: list,
        state: dict,
        workflow_id: str,
    ) -> None:
        current: ModuleSubmission = state["module_submissions"][module_id]
        revision = current.revision + 1
        if revision > 2:
            raise AgentWorkflowError(f"module {module_id} exceeded its total revision budget")
        issues = [issue for issue in all_issues if issue.module_id == module_id]
        issue_path = self.service.store.write_json(
            f"Work/runs/{state['run_id']}/reviews/cross-issues-{module_id}-r{revision}.json",
            {"issues": [issue.model_dump(mode="json") for issue in issues]},
        )
        previous = f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
        planned = next(
            item for item in state["plan_submission"].module_tasks
            if item.agent_id == f"module-{module_id}-specialist"
        )
        envelope = planned.model_copy(
            update={
                "task_id": f"module-{module_id}",
                "run_id": state["run_id"],
                "agent_id": f"module-{module_id}-specialist",
                "revision": revision,
                "prior_result_ref": previous,
                "issue_refs": [issue_path.relative_to(self.service.workspace).as_posix()],
                "allowed_outputs": ["module_submission"],
            }
        )
        payload = await self._agent(
            envelope.agent_id, envelope,
            [previous, *envelope.issue_refs, "Work/evidence.jsonl"],
            workflow_id, session_key=f"specialist-{module_id}",
        )
        if not isinstance(payload, ModuleSubmission) or payload.module_id != module_id:
            raise AgentWorkflowError(f"targeted revision returned wrong module {module_id}")
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
            payload.model_dump(mode="json"),
        )
        audit_envelope = TaskEnvelope(
            task_id=f"audit-{module_id}-cross-r{revision}",
            run_id=state["run_id"],
            agent_id="evidence-auditor",
            objective=f"复核模块 {module_id} 的定向修订是否解决问题且未破坏证据边界。",
            input_refs=[
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{revision}.json",
                *envelope.issue_refs,
                "Work/evidence.jsonl",
            ],
            allowed_outputs=["audit_submission"],
            revision=revision,
        )
        audit = await self._agent(
            "evidence-auditor", audit_envelope, audit_envelope.input_refs,
            workflow_id, session_key=f"auditor-{module_id}",
        )
        if (
            not isinstance(audit, AuditSubmission)
            or not audit.approved
            or any(issue.severity == "blocking" for issue in audit.issues)
        ):
            raise AgentWorkflowError(f"module {module_id} targeted revision did not pass audit")
        self.service.store.write_text(f"Outputs/Modules/{module_id}.md", payload.markdown)
        state["module_submissions"][module_id] = payload

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
            ],
            constraints=[
                "不得改变批准事实、数值、风险等级和来源语义",
                "citation_anchors 必须是正文中唯一出现的准确片段",
                "正文不得套用统一的事实-证据-风险模板",
            ],
            allowed_outputs=["edited_report_submission"],
        )
        payload = await self._agent(
            "chief-editor", envelope, envelope.input_refs,
            workflow_id, session_key="chief-editor",
        )
        if not isinstance(payload, EditedReportSubmission):
            raise AgentWorkflowError("chief-editor returned the wrong payload type")
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
        report = ApprovedReport(
            title=edited.title,
            overview=edited.overview,
            module_narratives=dict(edited.module_narratives),
            conclusion=edited.conclusion,
            ledger=ledger,
            citation_anchors=edited.citation_anchors,
        )
        self.service.store.write_json(
            "Work/report-state.json", report.model_dump(mode="json")
        )
        core_path = Path(os.environ.get(
            "AUTOREPORT_HANDOFF_DOCX_CORE",
            "/Users/zzymima0000/Documents/Codex/work/配电安全报告工具V2-交接/插件源码目录/core/docx_renderer.py",
        ))
        output = self.service.workspace / "Outputs/Reports/配电安全专家咨询报告.docx"
        render = PdsDocxRenderer(
            HandoffDocxCore(core_path, template_path=self.service.report_template_path)
        ).render(report, output)
        self.service.store.write_json(
            "Outputs/Reports/render-log.json", render.model_dump(mode="json")
        )
        receipt = ProjectDelivery(
            self.service.workspace / "Outputs/Deliveries"
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
        self.service.store.write_json(
            f"Work/runs/{state['run_id']}/delivery-receipt.json",
            receipt.model_dump(mode="json"),
        )
        state["output_artifacts"] = [
            *(OutputArtifact(kind="module", path=Path(f"Outputs/Modules/{module_id}.md"), module_id=module_id) for module_id in REPORT_MODULE_IDS),
            OutputArtifact(kind="review", path=Path("Outputs/Reviews/full-review.json")),
            OutputArtifact(kind="report", path=Path("Outputs/Reports/配电安全专家咨询报告.docx")),
            OutputArtifact(
                kind="run",
                path=receipt.manifest_path.relative_to(self.service.workspace),
            ),
        ]
