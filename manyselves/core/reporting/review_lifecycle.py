"""Finding/response/verdict review lifecycles with no mutable-issue compatibility."""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from pydantic import Field

from .agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    FINAL_REPORT_SECTION_IDS,
    CrossReviewFinding,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    EditedReportSubmission,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    FinalReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .assets import (
    expand_approved_module_markers,
    validate_aggregate_retention,
    validate_editor_protection,
    validate_editor_quality,
)
from .input_contracts import (
    ChiefRevisionInput,
    CrossReviewInput,
    FinalReviewInput,
    ModuleReviewInput,
    ModuleRevisionInput,
    RequestedModuleChange,
    ReviewEvidenceExcerpt,
    ReviewCompletionRecord,
    ValidationFailure,
    ValidationReport,
    WorkflowExceptionInput,
    edited_report_content_view,
    module_content_view,
    strip_runtime_claim_markers,
)
from .revision_diff import build_revision_diff
from .source_ledger import SourceLedger
from .taxonomy import REPORT_TAXONOMY

if TYPE_CHECKING:
    from .workflow import ReportWorkflowRunner


class ReviewLifecycleError(RuntimeError):
    """A deterministic review-protocol failure that requires code or model correction."""


class ModuleReviewProgress(StrictModel):
    run_id: str
    module_id: str
    next_action: str
    current: ModuleSubmission
    pending: list[ModuleReviewFinding] = Field(default_factory=list)
    responses: list[RevisionResponse] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    review_round: int = 0
    phase: str = "initial"
    scope: list[str] = Field(default_factory=list)


class CrossReviewProgress(StrictModel):
    run_id: str
    next_action: str
    modules: dict[str, ModuleSubmission]
    pending: list[CrossReviewFinding] = Field(default_factory=list)
    responses_by_module: dict[str, list[RevisionResponse]] = Field(default_factory=dict)
    local_review_refs: dict[str, str] = Field(default_factory=dict)
    machine_refs: list[str] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    prior_synthesis: list = Field(default_factory=list)
    phase: str = "initial"
    review_round: int = 0
    revised_owner_ids: list[str] = Field(default_factory=list)


class FinalReviewProgress(StrictModel):
    run_id: str
    next_action: str
    current: EditedReportSubmission
    pending: list[FinalReviewFinding] = Field(default_factory=list)
    responses: list[RevisionResponse] = Field(default_factory=list)
    finding_refs: list[str] = Field(default_factory=list)
    verdict_refs: list[str] = Field(default_factory=list)
    resolved_ids: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    phase: str = "initial"
    review_round: int = 0
    chief_revision_number: int = 0


def _relative(runner: "ReportWorkflowRunner", path: Path) -> str:
    return path.relative_to(runner.service.workspace).as_posix()


def _write_model(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
) -> str:
    return _relative(
        runner,
        runner.service.store.write_json(relative, model.model_dump(mode="json")),
    )


def _load_progress(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
):
    path = runner.service.workspace / relative
    if not path.is_file():
        return None
    return model.model_validate_json(path.read_text(encoding="utf-8"))


def _unique_ids(values: Iterable[str], *, label: str) -> set[str]:
    items = list(values)
    if len(items) != len(set(items)):
        raise ReviewLifecycleError(f"{label} contains duplicate ids")
    return set(items)


def _validate_responses(
    responses: list[RevisionResponse],
    required_ids: set[str],
    allowed_targets: set[str],
) -> None:
    response_ids = _unique_ids(
        (response.finding_id for response in responses),
        label="revision responses",
    )
    if response_ids != required_ids:
        raise ReviewLifecycleError(
            "revision responses must cover exactly the assigned findings; "
            f"missing={sorted(required_ids - response_ids)}; "
            f"unexpected={sorted(response_ids - required_ids)}"
        )
    invalid_targets = sorted(
        {
            target_id
            for response in responses
            for target_id in response.changed_target_ids
            if target_id not in allowed_targets
        }
    )
    if invalid_targets:
        raise ReviewLifecycleError(
            f"revision responses declare out-of-scope targets: {invalid_targets}"
        )


def _apply_module_patch(
    baseline: ModuleSubmission,
    patch: ModuleRevisionSubmission,
    *,
    target_submodule_ids: set[str],
    required_finding_ids: set[str],
) -> ModuleSubmission:
    if patch.module_id != baseline.module_id:
        raise ReviewLifecycleError("module patch belongs to a different module")
    if patch.base_revision != baseline.revision:
        raise ReviewLifecycleError(
            "module patch base_revision does not match the supplied subject"
        )
    if set(patch.submodule_narratives) - target_submodule_ids:
        raise ReviewLifecycleError("module patch replaces an unassigned submodule")
    _validate_responses(
        patch.revision_responses,
        required_finding_ids,
        target_submodule_ids,
    )
    baseline_claims = {claim.id: claim for claim in baseline.claims}
    for claim_id in patch.claim_ids_remove:
        claim = baseline_claims.get(claim_id)
        if claim is None:
            raise ReviewLifecycleError(
                f"module patch removes unknown Claim id: {claim_id}"
            )
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(
                f"module patch removes out-of-scope Claim id: {claim_id}"
            )
        del baseline_claims[claim_id]
    for claim in patch.claims_upsert:
        if claim.module_id != baseline.module_id:
            raise ReviewLifecycleError(
                f"module patch Claim belongs to another module: {claim.id}"
            )
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(
                f"module patch changes out-of-scope Claim id: {claim.id}"
            )
        baseline_claims[claim.id] = claim
    claim_source_ids = {
        source_id for claim in baseline_claims.values() for source_id in claim.source_ids
    }
    if not claim_source_ids.issubset(set(patch.source_ids)):
        raise ReviewLifecycleError(
            "module patch source_ids do not cover every resulting Claim source"
        )
    narratives = dict(baseline.submodule_narratives)
    narratives.update(patch.submodule_narratives)
    return ModuleSubmission(
        module_id=baseline.module_id,
        submodule_narratives=narratives,
        claims=list(baseline_claims.values()),
        source_ids=patch.source_ids,
        unresolved_questions=patch.unresolved_questions,
        revision=patch.revision,
        revision_responses=patch.revision_responses,
    )


def _validate_module_findings(
    findings: list[ModuleReviewFinding],
    subject: ModuleSubmission,
    scope: set[str],
) -> None:
    _unique_ids((finding.id for finding in findings), label="module findings")
    for finding in findings:
        if finding.target_submodule_id not in scope:
            raise ReviewLifecycleError(
                f"module finding targets unreviewed submodule: {finding.id}"
            )


def _module_review_evidence_packet(
    runner: "ReportWorkflowRunner",
    subject: ModuleSubmission,
    run_id: str,
) -> list[ReviewEvidenceExcerpt]:
    """Attach cited E-* evidence once so review does not become a retrieval loop."""

    ledger = SourceLedger(runner.service.workspace, run_id)
    records = {record.id: record for record in ledger.records}
    evidence_ids = sorted(
        {
            source_id
            for claim in subject.claims
            for source_id in claim.source_ids
            if source_id.startswith("E-")
        }
    )
    packet: list[ReviewEvidenceExcerpt] = []
    for evidence_id in evidence_ids:
        record = records.get(evidence_id)
        content_ref = ledger.content_ref(evidence_id)
        if record is None or content_ref is None:
            raise ReviewLifecycleError(
                f"review subject cites unreadable current-run evidence: {evidence_id}"
            )
        content = (runner.service.workspace / content_ref).read_text(encoding="utf-8")
        if len(content) > 4_000:
            content = content[:3_960].rstrip() + "\n[bounded excerpt]"
        packet.append(
            ReviewEvidenceExcerpt(
                evidence_id=evidence_id,
                title=record.title,
                locator=record.locator,
                content=content,
            )
        )
    return packet


def _validate_verdicts(
    verdicts: list[ResolutionVerdict],
    required_ids: set[str],
) -> None:
    verdict_ids = _unique_ids(
        (verdict.finding_id for verdict in verdicts), label="review verdicts"
    )
    if verdict_ids != required_ids:
        raise ReviewLifecycleError(
            "review verdicts must cover exactly the required findings; "
            f"missing={sorted(required_ids - verdict_ids)}; "
            f"unexpected={sorted(verdict_ids - required_ids)}"
        )


async def _main_exception_decision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    scope: str,
    subject_refs: list[str],
    finding_refs: list[str],
    verdicts: list[ResolutionVerdict],
    responses: list[RevisionResponse],
    trigger: str = "reviewer_escalation",
) -> WorkflowDecisionSubmission:
    exception_ids = (
        {
            verdict.finding_id
            for verdict in verdicts
            if verdict.verdict == "escalate"
        }
        if trigger == "reviewer_escalation"
        else {
            response.finding_id
            for response in responses
            if response.action in {"disputed", "needs_input"}
        }
    )
    if not exception_ids:
        raise ReviewLifecycleError("Main exception decision requires explicit finding ids")
    exception_input = WorkflowExceptionInput(
        run_id=state["run_id"],
        scope=scope,
        trigger=trigger,
        finding_ids=sorted(exception_ids),
        subject_refs=subject_refs,
        finding_refs=finding_refs,
        verdicts=(
            [verdict for verdict in verdicts if verdict.verdict == "escalate"]
            if trigger == "reviewer_escalation"
            else []
        ),
        revision_responses=[
            response for response in responses if response.finding_id in exception_ids
        ],
    )
    input_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/exceptions/"
            f"{scope}-{trigger}-{'-'.join(sorted(exception_ids))}.json"
        ),
        exception_input,
    )
    envelope = TaskEnvelope(
        task_id=f"{scope}-review-exception",
        run_id=state["run_id"],
        agent_id="main-agent",
        objective=(
            "只处理明确进入例外节点的 finding：核对当前正文、作者响应与可用审查理由，"
            "决定退回作者、接受争议、请求用户或停止不完整。"
        ),
        input_refs=[input_ref, *subject_refs, *finding_refs],
        constraints=[
            "不得重新执行整轮专业审查",
            "不得改写 finding、author response 或 reviewer verdict",
            "finding_ids 必须精确覆盖全部且仅覆盖本次 exception finding",
        ],
        allowed_outputs=["workflow_decision_submission"],
        input_contract_kind="workflow_exception_input",
        input_contract_ref=input_ref,
    )
    result = await runner._agent(
        "main-agent",
        envelope,
        envelope.input_refs,
        workflow_id,
        session_key=f"main-{scope}-exception",
    )
    if not isinstance(result, WorkflowDecisionSubmission):
        raise ReviewLifecycleError("Main returned the wrong exception-decision type")
    if set(result.finding_ids) != exception_ids:
        raise ReviewLifecycleError(
            "Main exception decision must cover exactly the exception findings"
        )
    decision_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/exceptions/"
            f"{scope}-{trigger}-decision-{'-'.join(sorted(exception_ids))}.json"
        ),
        result,
    )
    state.setdefault("review_exception_refs", []).append(decision_ref)
    if result.decision == "request_user":
        from .workflow import ReportingNeedsDecisionError

        raise ReportingNeedsDecisionError(result.rationale)
    if result.decision == "stop_incomplete":
        from .workflow import ReportingNeedsDecisionError

        raise ReportingNeedsDecisionError(result.rationale, keep_agents_alive=False)
    return result


async def request_module_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    subject: ModuleSubmission,
    module_findings: list[ModuleReviewFinding] | None = None,
    cross_findings: list[CrossReviewFinding] | None = None,
    requested_changes: list[RequestedModuleChange] | None = None,
    validation_ref: str | None = None,
) -> tuple[ModuleSubmission, str]:
    module_findings = module_findings or []
    cross_findings = cross_findings or []
    requested_changes = requested_changes or []
    validation_report = (
        ValidationReport.model_validate_json(
            (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
        )
        if validation_ref
        else None
    )
    required_ids = {
        *(finding.id for finding in module_findings),
        *(finding.id for finding in cross_findings),
        *(change.id for change in requested_changes),
    }
    targets = {
        *(finding.target_submodule_id for finding in module_findings),
        *(
            target_id
            for finding in cross_findings
            for target_id in finding.target_submodule_ids
        ),
        *(
            target_id
            for change in requested_changes
            for target_id in change.target_submodule_ids
        ),
    }
    revision_input = ModuleRevisionInput(
        run_id=state["run_id"],
        module_id=subject.module_id,
        subject_ref=(
            f"Work/runs/{state['run_id']}/modules/"
            f"{subject.module_id}-r{subject.revision}.json"
        ),
        subject=module_content_view(subject),
        target_submodule_ids=sorted(targets),
        module_findings=module_findings,
        cross_findings=cross_findings,
        requested_changes=requested_changes,
        validation_report_ref=validation_ref,
        validation_report=validation_report,
    )
    input_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/module-revision-input-"
            f"{subject.module_id}-r{subject.revision + 1}.json"
        ),
        revision_input,
    )
    subject_path = runner.service.workspace / revision_input.subject_ref
    if not subject_path.is_file():
        _write_model(runner, revision_input.subject_ref, subject)
    input_refs = [input_ref, revision_input.subject_ref]
    if validation_ref:
        input_refs.append(validation_ref)
    specialist_id = f"module-{subject.module_id}-specialist"
    envelope = TaskEnvelope(
        task_id=f"module-{subject.module_id}-revision-r{subject.revision + 1}",
        run_id=state["run_id"],
        agent_id=specialist_id,
        objective="按结构化 finding 对当前模块执行显式、定向的补丁修订。",
        input_refs=input_refs,
        constraints=[
            "只提交小型 module_revision_submission commit；不得在其中重复正文",
            "每个 assigned target_submodule_id 必须先用 write_result_part 保存完整替换正文和 evidence_ids",
            "最终提交只使用 schema 声明的简短字段；运行时从保存的小节自动生成补丁",
            "revision_responses 必须逐项覆盖 assigned finding ids",
            "disputed 或 needs_input 不得伪造 changed_target_ids",
            *(
                [f"上一版显式机器检查未通过；只修复 {validation_ref} 中列出的谓词失败"]
                if validation_ref
                else []
            ),
            *runner._user_supplement_constraints(
                state,
                stage="module_authoring",
                target_ids={
                    subject.module_id,
                    *targets,
                    *(claim.id for claim in subject.claims),
                },
            ),
        ],
        allowed_outputs=["module_revision_submission"],
        revision=subject.revision + 1,
        prior_result_ref=revision_input.subject_ref,
        target_submodule_ids=sorted(targets),
        input_contract_kind="module_revision_input",
        input_contract_ref=input_ref,
        context_summary_refs=runner._context_refs(state, specialist_id),
        inline_context=runner._template_skill_context(
            state, "core", "analysis", "visual", "rubric"
        ),
    )
    patch = await runner._agent(
        specialist_id,
        envelope,
        envelope.input_refs,
        workflow_id,
        session_key=f"specialist-{subject.module_id}",
    )
    if not isinstance(patch, ModuleRevisionSubmission):
        raise ReviewLifecycleError("module specialist returned the wrong revision type")
    revised = _apply_module_patch(
        subject,
        patch,
        target_submodule_ids=targets,
        required_finding_ids=required_ids,
    )
    subject_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/modules/"
            f"{subject.module_id}-r{revised.revision}.json"
        ),
        revised,
    )
    diff = build_revision_diff(subject, revised)
    runner.service.store.write_json(
        (
            f"Work/runs/{state['run_id']}/reviews/module-diff-"
            f"{subject.module_id}-r{revised.revision}.json"
        ),
        diff,
    )
    return revised, subject_ref


def _module_review_completion(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module: ModuleSubmission,
    reviewer_session_key: str,
    subject_ref: str,
    finding_refs: list[str],
    verdict_refs: list[str],
    resolved_ids: set[str],
) -> str:
    completion = ReviewCompletionRecord(
        lifecycle="module",
        run_id=state["run_id"],
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=reviewer_session_key,
        subject_refs=[subject_ref],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_finding_ids=sorted(resolved_ids),
    )
    ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/module-completion-"
            f"{module.module_id}-r{module.revision}.json"
        ),
        completion,
    )
    state.setdefault("module_review_completion_refs", {})[module.module_id] = ref
    runner.service.store.write_text(
        f"Outputs/Modules/{module.module_id}.md", module.markdown
    )
    return ref


async def run_module_review(
    runner: "ReportWorkflowRunner",
    module_id: str,
    payload: ModuleSubmission,
    state: dict,
    workflow_id: str,
    *,
    initial_scope: set[str],
) -> ModuleSubmission:
    """Run module-local finding/response/verdict closure with one reviewer session."""

    if payload.module_id != module_id:
        raise ReviewLifecycleError("module review payload belongs to a different module")
    reviewer_session_key = f"module-auditor-{module_id}"
    current = payload
    pending: dict[str, ModuleReviewFinding] = {}
    responses: list[RevisionResponse] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    review_round = 0
    phase = "initial"
    scope = set(initial_scope)
    progress_ref = (
        f"Work/runs/{state['run_id']}/reviews/"
        f"module-progress-{module_id}.json"
    )

    def save_progress(next_action: str) -> None:
        _write_model(
            runner,
            progress_ref,
            ModuleReviewProgress(
                run_id=state["run_id"],
                module_id=module_id,
                next_action=next_action,
                current=current,
                pending=list(pending.values()),
                responses=responses,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=sorted(resolved_ids),
                review_round=review_round,
                phase=phase,
                scope=sorted(scope),
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, ModuleReviewProgress)
        if state.get("resume")
        else None
    )
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"] or progress.module_id != module_id:
            raise ReviewLifecycleError("module review progress identity mismatch")
        current = progress.current
        pending = {finding.id: finding for finding in progress.pending}
        responses = progress.responses
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        review_round = progress.review_round
        phase = progress.phase
        scope = set(progress.scope)
        if progress.next_action == "revise":
            candidate_path = runner.service.workspace / (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{current.revision + 1}.json"
            )
            candidate = None
            if candidate_path.is_file():
                try:
                    candidate = ModuleSubmission.model_validate_json(
                        candidate_path.read_text(encoding="utf-8")
                    )
                    _validate_responses(
                        candidate.revision_responses,
                        set(pending),
                        {
                            finding.target_submodule_id
                            for finding in pending.values()
                        },
                    )
                except (OSError, ValueError):
                    candidate = None
            if candidate is None:
                current, _ = await request_module_revision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    subject=current,
                    module_findings=list(pending.values()),
                )
            else:
                current = candidate
            responses = current.revision_responses
            exceptional = [
                response
                for response in responses
                if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="module",
                    subject_refs=[
                        f"Work/runs/{state['run_id']}/modules/"
                        f"{module_id}-r{current.revision}.json"
                    ],
                    finding_refs=finding_refs,
                    verdicts=[],
                    responses=exceptional,
                    trigger="author_response",
                )
                if decision.decision == "return_to_author":
                    current, _ = await request_module_revision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        subject=current,
                        module_findings=list(pending.values()),
                    )
                    responses = current.revision_responses
            scope = {
                finding.target_submodule_id for finding in pending.values()
            }
            phase = "recheck"
            review_round += 1
            save_progress("review")

    while True:
        subject_ref = (
            f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
        )
        if not (runner.service.workspace / subject_ref).is_file():
            _write_model(runner, subject_ref, current)
        signal_ref = runner._validate_module_structure(
            state, current, f"review-r{review_round}"
        )
        validation_report = ValidationReport.model_validate_json(
            (runner.service.workspace / signal_ref).read_text(encoding="utf-8")
        )
        review_input = ModuleReviewInput(
            phase=phase,
            run_id=state["run_id"],
            module_id=module_id,
            subject_ref=subject_ref,
            subject_revision=current.revision,
            subject=module_content_view(current),
            evidence=_module_review_evidence_packet(
                runner, current, state["run_id"]
            ),
            required_submodule_ids=sorted(scope),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
            validation_report_ref=signal_ref,
            validation_report=validation_report,
        )
        input_ref = _write_model(
            runner,
            (
                f"Work/runs/{state['run_id']}/reviews/module-review-input-"
                f"{module_id}-r{review_round}.json"
            ),
            review_input,
        )
        output_kind = (
            "module_review_finding_submission"
            if phase == "initial"
            else "module_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"module-{module_id}-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="evidence-auditor",
            objective=(
                f"审查模块 {module_id} 的当前正文、Claim 与证据边界。"
                if phase == "initial"
                else f"只对模块 {module_id} 的 required_findings 返回逐项 verdict，并检查修改回归。"
            ),
            input_refs=[input_ref, subject_ref, signal_ref],
            constraints=[
                "coverage 记录实际检查范围，不是批准状态",
                "finding 首次提出后不可改写；复审不得复述旧 finding",
                "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
                (
                    "首轮必须覆盖 input 中全部 required_submodule_ids"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="module_review",
                    target_ids={
                        module_id,
                        *scope,
                        *(claim.id for claim in current.claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            target_submodule_ids=sorted(scope),
            input_contract_kind="module_review_input",
            input_contract_ref=input_ref,
            context_summary_refs=runner._context_refs(state, "evidence-auditor"),
            allowed_tools=["submit_result"],
        )
        result = await runner._agent(
            "evidence-auditor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        if phase == "initial":
            if not isinstance(result, ModuleReviewFindingSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong initial type")
            if not scope.issubset(set(result.coverage.submodule_ids)):
                raise ReviewLifecycleError(
                    "initial module review coverage omitted assigned submodules"
                )
            _validate_module_findings(result.findings, current, scope)
            review_ref = _write_model(
                runner,
                (
                    f"Work/runs/{state['run_id']}/reviews/module-findings-"
                    f"{module_id}-r{review_round}.json"
                ),
                result,
            )
            finding_refs.append(review_ref)
            pending = {finding.id: finding for finding in result.findings}
        else:
            if not isinstance(result, ModuleReviewVerdictSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong recheck type")
            required_ids = set(pending)
            _validate_verdicts(result.verdicts, required_ids)
            _validate_module_findings(result.new_findings, current, scope)
            verdict_ref = _write_model(
                runner,
                (
                    f"Work/runs/{state['run_id']}/reviews/module-verdicts-"
                    f"{module_id}-r{review_round}.json"
                ),
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [
                verdict for verdict in result.verdicts if verdict.verdict == "escalate"
            ]
            main_accepts: set[str] = set()
            if escalated:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="module",
                    subject_refs=[subject_ref],
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=responses,
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (
                    verdict.verdict == "escalate"
                    and verdict.finding_id not in main_accepts
                )
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved"
                or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new module finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_model(
                    runner,
                    (
                        f"Work/runs/{state['run_id']}/reviews/module-regression-findings-"
                        f"{module_id}-r{review_round}.json"
                    ),
                    ModuleReviewFindingSubmission(
                        coverage=result.coverage,
                        findings=result.new_findings,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending

        if not pending:
            _module_review_completion(
                runner,
                state=state,
                module=current,
                reviewer_session_key=reviewer_session_key,
                subject_ref=subject_ref,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=resolved_ids,
            )
            save_progress("completed")
            return current

        save_progress("revise")
        while True:
            current, current_ref = await request_module_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                subject=current,
                module_findings=list(pending.values()),
            )
            responses = current.revision_responses
            exceptional = [
                response
                for response in responses
                if response.action in {"disputed", "needs_input"}
            ]
            if not exceptional:
                break
            decision = await _main_exception_decision(
                runner,
                state=state,
                workflow_id=workflow_id,
                scope="module",
                subject_refs=[current_ref],
                finding_refs=finding_refs,
                verdicts=[],
                responses=exceptional,
                trigger="author_response",
            )
            if decision.decision != "return_to_author":
                break
        scope = {
            finding.target_submodule_id for finding in pending.values()
        }
        phase = "recheck"
        review_round += 1
        save_progress("review")


def _resolve_subject_path(subject: ModuleSubmission, path: str) -> str:
    if path.startswith("submodule_narratives."):
        key = path.removeprefix("submodule_narratives.")
        try:
            return subject.submodule_narratives[key]
        except KeyError as exc:
            raise ReviewLifecycleError(f"machine check target does not exist: {path}") from exc
    if path.startswith("claims."):
        remainder = path.removeprefix("claims.")
        claim_id, separator, field = remainder.rpartition(".")
        if not separator:
            raise ReviewLifecycleError(f"machine Claim path requires a field: {path}")
        claim = next((value for value in subject.claims if value.id == claim_id), None)
        if claim is None or field not in claim.model_fields:
            raise ReviewLifecycleError(f"machine check target does not exist: {path}")
        return str(getattr(claim, field))
    raise ReviewLifecycleError(f"unsupported explicit machine-check path: {path}")


def _run_cross_machine_checks(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    subject: ModuleSubmission,
    subject_ref: str,
    findings: list[CrossReviewFinding],
) -> str:
    failures: list[ValidationFailure] = []
    check_ids: list[str] = []
    for finding in findings:
        for index, check in enumerate(finding.machine_checks, start=1):
            check_id = f"{finding.id}:machine:{index}"
            check_ids.append(check_id)
            values = [
                _resolve_subject_path(subject, path) for path in check.target_paths
            ]
            if check.kind == "forbidden_terms_absent":
                for path, value in zip(check.target_paths, values, strict=True):
                    present = [term for term in check.expected_values if term in value]
                    if present:
                        failures.append(
                            ValidationFailure(
                                check_id=check_id,
                                finding_id=finding.id,
                                target_path=path,
                                message=f"forbidden terms still present: {present}",
                            )
                        )
            elif check.kind == "required_terms_present":
                combined = "\n".join(values)
                missing = [
                    term for term in check.expected_values if term not in combined
                ]
                if missing:
                    failures.append(
                        ValidationFailure(
                            check_id=check_id,
                            finding_id=finding.id,
                            target_path=", ".join(check.target_paths),
                            message=f"required terms missing: {missing}",
                        )
                    )
            elif check.kind == "field_equals":
                if len(check.target_paths) != len(check.expected_values):
                    raise ReviewLifecycleError(
                        f"{check_id} field_equals requires one expected value per path"
                    )
                for path, value, expected in zip(
                    check.target_paths,
                    values,
                    check.expected_values,
                    strict=True,
                ):
                    if value != expected:
                        failures.append(
                            ValidationFailure(
                                check_id=check_id,
                                finding_id=finding.id,
                                target_path=path,
                                message="field value does not equal the declared expected value",
                            )
                        )
    report = ValidationReport(
        run_id=state["run_id"],
        subject_ref=subject_ref,
        validator="explicit-cross-predicates-v1",
        check_ids=check_ids,
        failures=failures,
        passed=not failures,
    )
    return _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/validations/cross-"
            f"{subject.module_id}-r{subject.revision}.json"
        ),
        report,
    )


def _validate_cross_findings(
    findings: list[CrossReviewFinding],
    modules: dict[str, ModuleSubmission],
) -> None:
    _unique_ids((finding.id for finding in findings), label="cross findings")


async def run_cross_review(
    runner: "ReportWorkflowRunner",
    state: dict,
    workflow_id: str,
) -> None:
    reviewer_session_key = "cross-module-reviewer"
    modules: dict[str, ModuleSubmission] = dict(state["module_submissions"])
    pending: dict[str, CrossReviewFinding] = {}
    responses_by_module: dict[str, list[RevisionResponse]] = {}
    local_review_refs: dict[str, str] = {}
    machine_refs: list[str] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    prior_synthesis = []
    phase = "initial"
    review_round = 0
    revised_owner_ids: set[str] = set()
    progress_ref = (
        f"Work/runs/{state['run_id']}/reviews/cross-progress.json"
    )

    def save_progress(next_action: str) -> None:
        _write_model(
            runner,
            progress_ref,
            CrossReviewProgress(
                run_id=state["run_id"],
                next_action=next_action,
                modules=modules,
                pending=list(pending.values()),
                responses_by_module=responses_by_module,
                local_review_refs=local_review_refs,
                machine_refs=machine_refs,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=sorted(resolved_ids),
                prior_synthesis=prior_synthesis,
                phase=phase,
                review_round=review_round,
                revised_owner_ids=sorted(revised_owner_ids),
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, CrossReviewProgress)
        if state.get("resume")
        else None
    )
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"]:
            raise ReviewLifecycleError("Cross review progress identity mismatch")
        modules = progress.modules
        state["module_submissions"].update(modules)
        pending = {finding.id: finding for finding in progress.pending}
        responses_by_module = progress.responses_by_module
        local_review_refs = progress.local_review_refs
        machine_refs = progress.machine_refs
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        prior_synthesis = progress.prior_synthesis
        phase = progress.phase
        review_round = progress.review_round
        revised_owner_ids = set(progress.revised_owner_ids)

    async def complete_revision_wave() -> None:
        nonlocal responses_by_module, local_review_refs, machine_refs
        grouped: dict[str, list[CrossReviewFinding]] = defaultdict(list)
        for finding in pending.values():
            grouped[finding.owner_module_id].append(finding)
        if not revised_owner_ids:
            responses_by_module = {}
            local_review_refs = {}
            machine_refs = []
        for module_id in sorted(grouped):
            if module_id in revised_owner_ids:
                continue
            current = modules[module_id]
            validation_ref: str | None = None
            machine_attempts = 0
            machine_failure_fingerprints: dict[tuple, int] = {}
            persisted_candidate: ModuleSubmission | None = None
            modules_root = (
                runner.service.workspace
                / f"Work/runs/{state['run_id']}/modules"
            )
            candidates: list[ModuleSubmission] = []
            for path in modules_root.glob(f"{module_id}-r*.json"):
                try:
                    candidate = ModuleSubmission.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                    if candidate.revision <= current.revision:
                        continue
                    _validate_responses(
                        candidate.revision_responses,
                        {finding.id for finding in grouped[module_id]},
                        {
                            target_id
                            for finding in grouped[module_id]
                            for target_id in finding.target_submodule_ids
                        },
                    )
                    candidates.append(candidate)
                except (OSError, ValueError):
                    continue
            if candidates:
                persisted_candidate = max(
                    candidates, key=lambda item: item.revision
                )
            while True:
                machine_attempts += 1
                if persisted_candidate is not None:
                    revised = persisted_candidate
                    revised_ref = (
                        f"Work/runs/{state['run_id']}/modules/"
                        f"{module_id}-r{revised.revision}.json"
                    )
                    persisted_candidate = None
                else:
                    revised, revised_ref = await request_module_revision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        subject=current,
                        cross_findings=grouped[module_id],
                        validation_ref=validation_ref,
                    )
                exceptional = [
                    response
                    for response in revised.revision_responses
                    if response.action in {"disputed", "needs_input"}
                ]
                if exceptional:
                    decision = await _main_exception_decision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        scope="cross",
                        subject_refs=[revised_ref],
                        finding_refs=finding_refs,
                        verdicts=[],
                        responses=exceptional,
                        trigger="author_response",
                    )
                    if decision.decision == "return_to_author":
                        current = revised
                        validation_ref = None
                        continue
                validation_ref = _run_cross_machine_checks(
                    runner,
                    state=state,
                    subject=revised,
                    subject_ref=revised_ref,
                    findings=grouped[module_id],
                )
                report = ValidationReport.model_validate_json(
                    (runner.service.workspace / validation_ref).read_text(
                        encoding="utf-8"
                    )
                )
                if report.passed:
                    break
                fingerprint = tuple(
                    sorted(
                        (
                            failure.check_id,
                            failure.finding_id or "",
                            failure.target_path,
                            failure.message,
                        )
                        for failure in report.failures
                    )
                )
                repeated = machine_failure_fingerprints.get(fingerprint, 0) + 1
                machine_failure_fingerprints[fingerprint] = repeated
                if repeated >= 2 or machine_attempts >= 3:
                    raise ReviewLifecycleError(
                        "Cross machine validation failed repeatedly; the workflow "
                        "stopped without rewriting the module or reviewer verdict. "
                        f"module={module_id}; attempts={machine_attempts}; "
                        f"validation_ref={validation_ref}"
                    )
                current = revised
            modules[module_id] = revised
            state["module_submissions"][module_id] = revised
            state.setdefault("specialist_submissions", {})[module_id] = revised
            responses_by_module[module_id] = revised.revision_responses
            machine_refs.append(validation_ref)
            local_scope = {
                target_id
                for finding in grouped[module_id]
                for target_id in finding.target_submodule_ids
            }
            local_reviewed = await run_module_review(
                runner,
                module_id,
                revised,
                state,
                workflow_id,
                initial_scope=local_scope,
            )
            modules[module_id] = local_reviewed
            state["module_submissions"][module_id] = local_reviewed
            local_review_refs[module_id] = state["module_review_completion_refs"][
                module_id
            ]
            revised_owner_ids.add(module_id)
            save_progress("revise")

    if progress is not None and progress.next_action == "revise":
        await complete_revision_wave()
        phase = "recheck"
        review_round += 1
        revised_owner_ids = set()
        save_progress("review")

    while True:
        module_refs = {
            module_id: (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{modules[module_id].revision}.json"
            )
            for module_id in REPORT_TAXONOMY
        }
        cross_input = CrossReviewInput(
            phase=phase,
            run_id=state["run_id"],
            module_refs=module_refs,
            module_revisions={
                module_id: modules[module_id].revision for module_id in REPORT_TAXONOMY
            },
            modules={
                module_id: module_content_view(module)
                for module_id, module in modules.items()
            },
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses_by_module=responses_by_module if phase == "recheck" else {},
            local_regression_review_refs=local_review_refs if phase == "recheck" else {},
            prior_synthesis_inputs=prior_synthesis if phase == "recheck" else [],
            machine_validation_refs=machine_refs if phase == "recheck" else [],
            machine_validation_reports=(
                [
                    ValidationReport.model_validate_json(
                        (runner.service.workspace / ref).read_text(encoding="utf-8")
                    )
                    for ref in machine_refs
                ]
                if phase == "recheck"
                else []
            ),
        )
        input_ref = _write_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/cross-review-input-r{review_round}.json",
            cross_input,
        )
        output_kind = (
            "cross_review_finding_submission"
            if phase == "initial"
            else "cross_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"cross-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="cross-module-reviewer",
            objective=(
                "审查五个已完成模块之间的术语、事实、风险、依赖、传播和联合验证接口。"
                if phase == "initial"
                else "由原 Cross reviewer 逐项判断 required_findings 是否关闭并检查接口回归。"
            ),
            input_refs=[input_ref, *module_refs.values()],
            constraints=[
                "coverage 只记录审查范围，不含 integrated 或 approved 状态",
                "findings 只包含必须写回责任模块的问题",
                "synthesis_inputs 只包含无需模块返工、可供总编综合的已支持关系",
                "不得审查单模块局部写作质量",
                (
                    "首轮 coverage 必须对五个模块逐项覆盖全部六个维度"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；模块 auditor 的结果不能代替 Cross verdict"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="cross_review",
                    target_ids={
                        *REPORT_TAXONOMY,
                        *(
                            claim.id
                            for module in modules.values()
                            for claim in module.claims
                        ),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            input_contract_kind="cross_review_input",
            input_contract_ref=input_ref,
            context_summary_refs=runner._context_refs(
                state, "cross-module-reviewer"
            ),
            inline_context=runner._template_skill_context(
                state, "core", "analysis", "synthesis", "rubric"
            ),
        )
        result = await runner._agent(
            "cross-module-reviewer",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        if phase == "initial":
            if not isinstance(result, CrossReviewFindingSubmission):
                raise ReviewLifecycleError("Cross reviewer returned the wrong initial type")
            for entry in result.coverage:
                missing = set(CROSS_REVIEW_DIMENSIONS) - set(entry.checked_dimensions)
                if missing:
                    raise ReviewLifecycleError(
                        f"Cross coverage for {entry.module_id} omitted dimensions: {sorted(missing)}"
                    )
            _validate_cross_findings(result.findings, modules)
            finding_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-findings-r{review_round}.json",
                result,
            )
            finding_refs.append(finding_ref)
            pending = {finding.id: finding for finding in result.findings}
            prior_synthesis = result.synthesis_inputs
        else:
            if not isinstance(result, CrossReviewVerdictSubmission):
                raise ReviewLifecycleError("Cross reviewer returned the wrong recheck type")
            _validate_verdicts(result.verdicts, set(pending))
            _validate_cross_findings(result.new_findings, modules)
            verdict_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [
                verdict for verdict in result.verdicts if verdict.verdict == "escalate"
            ]
            main_accepts: set[str] = set()
            if escalated:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="cross",
                    subject_refs=list(module_refs.values()),
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=[
                        response
                        for values in responses_by_module.values()
                        for response in values
                    ],
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (
                    verdict.verdict == "escalate"
                    and verdict.finding_id not in main_accepts
                )
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved"
                or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new Cross finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_model(
                    runner,
                    (
                        f"Work/runs/{state['run_id']}/reviews/"
                        f"cross-regression-findings-r{review_round}.json"
                    ),
                    CrossReviewFindingSubmission(
                        coverage=result.coverage,
                        findings=result.new_findings,
                        synthesis_inputs=result.synthesis_inputs,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending
            prior_synthesis = result.synthesis_inputs

        if not pending:
            completion = ReviewCompletionRecord(
                lifecycle="cross",
                run_id=state["run_id"],
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key=reviewer_session_key,
                subject_refs=list(module_refs.values()),
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
            )
            completion_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-completion.json",
                completion,
            )
            state["cross_review_completion_ref"] = completion_ref
            state["cross_synthesis_inputs"] = prior_synthesis
            save_progress("completed")
            return

        revised_owner_ids = set()
        save_progress("revise")
        await complete_revision_wave()
        phase = "recheck"
        review_round += 1
        revised_owner_ids = set()
        save_progress("review")


async def _request_chief_revision(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    workflow_id: str,
    current: EditedReportSubmission,
    current_ref: str,
    pending: dict[str, FinalReviewFinding],
    approved_module_text: dict[str, str],
    claims: list,
    aggregate_mode: bool,
    chief_envelope: TaskEnvelope,
    chief_session_key: str,
    revision_number: int,
) -> tuple[EditedReportSubmission, str]:
    target_sections = {
        section_id
        for finding in pending.values()
        for section_id in finding.target_section_ids
    }
    revision_input = ChiefRevisionInput(
        run_id=state["run_id"],
        subject_ref=current_ref,
        subject=edited_report_content_view(current),
        target_section_ids=sorted(target_sections),
        findings=list(pending.values()),
    )
    revision_input_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/"
            f"chief-revision-input-r{revision_number}.json"
        ),
        revision_input,
    )
    revision_envelope = chief_envelope.model_copy(
        update={
            "task_id": f"chief-edit-r{revision_number}",
            "objective": "按 final review findings 定向修订当前成稿。",
            "input_refs": [
                revision_input_ref,
                current_ref,
                *chief_envelope.input_refs,
            ],
            "allowed_outputs": ["edited_report_submission"],
            "revision": revision_number,
            "prior_result_ref": current_ref,
            "target_submodule_ids": [],
            "input_contract_kind": "chief_revision_input",
            "input_contract_ref": revision_input_ref,
            "constraints": [
                *chief_envelope.constraints,
                "只能修改 target_section_ids",
                "revision_responses 必须逐项且仅覆盖 assigned finding ids",
                "不得让工作流替你补写响应、章节或引用",
            ],
        }
    )
    revised = await runner._agent(
        "chief-editor",
        revision_envelope,
        revision_envelope.input_refs,
        workflow_id,
        session_key=chief_session_key,
    )
    if not isinstance(revised, EditedReportSubmission):
        raise ReviewLifecycleError("chief editor returned the wrong revision type")
    _validate_responses(
        revised.revision_responses,
        set(pending),
        target_sections,
    )
    revised = expand_approved_module_markers(revised, approved_module_text)
    diff = runner._final_revision_diff(current, revised)
    unexpected_sections = sorted(
        set(diff["changed_section_ids"]) - target_sections
    )
    unexpected_contract = sorted(
        set(diff["changed_contract_fields"]) - {"revision_responses"}
    )
    if unexpected_sections or unexpected_contract:
        raise ReviewLifecycleError(
            "chief revision changed content outside finding scope: "
            f"sections={unexpected_sections}; contract_fields={unexpected_contract}"
        )
    if aggregate_mode:
        state["editor_quality_observations"] = validate_aggregate_retention(
            revised, approved_module_text
        )
        if claims:
            validate_editor_protection(revised, claims)
    else:
        validate_editor_protection(revised, claims)
        state["editor_quality_observations"] = validate_editor_quality(
            revised, state["module_submissions"]
        )
    revised_ref = _write_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/edited-revisions/"
            f"chief-author-r{revision_number}.json"
        ),
        revised,
    )
    return revised, revised_ref


async def run_final_review(
    runner: "ReportWorkflowRunner",
    state: dict,
    workflow_id: str,
    *,
    chief_envelope: TaskEnvelope,
    chief_session_key: str,
    approved_module_text: dict[str, str],
    claims: list,
    aggregate_mode: bool = False,
) -> None:
    reviewer_session_key = "chief-editor-auditor"
    current: EditedReportSubmission = state["edited_report"]
    pending: dict[str, FinalReviewFinding] = {}
    responses: list[RevisionResponse] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    residual_risks: list[str] = []
    phase = "initial"
    review_round = 0
    chief_revision_number = 0
    progress_ref = (
        f"Work/runs/{state['run_id']}/reviews/final-progress.json"
    )

    def save_progress(next_action: str) -> None:
        _write_model(
            runner,
            progress_ref,
            FinalReviewProgress(
                run_id=state["run_id"],
                next_action=next_action,
                current=current,
                pending=list(pending.values()),
                responses=responses,
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_ids=sorted(resolved_ids),
                residual_risks=residual_risks,
                phase=phase,
                review_round=review_round,
                chief_revision_number=chief_revision_number,
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, FinalReviewProgress)
        if state.get("resume")
        else None
    )
    if progress is not None and progress.next_action != "completed":
        if progress.run_id != state["run_id"]:
            raise ReviewLifecycleError("final review progress identity mismatch")
        current = progress.current
        pending = {finding.id: finding for finding in progress.pending}
        responses = progress.responses
        finding_refs = progress.finding_refs
        verdict_refs = progress.verdict_refs
        resolved_ids = set(progress.resolved_ids)
        residual_risks = progress.residual_risks
        phase = progress.phase
        review_round = progress.review_round
        chief_revision_number = progress.chief_revision_number
        if progress.next_action == "revise":
            next_revision = chief_revision_number + 1
            candidate_ref = (
                f"Work/runs/{state['run_id']}/edited-revisions/"
                f"chief-author-r{next_revision}.json"
            )
            candidate_path = runner.service.workspace / candidate_ref
            revised = None
            if candidate_path.is_file():
                try:
                    revised = EditedReportSubmission.model_validate_json(
                        candidate_path.read_text(encoding="utf-8")
                    )
                    _validate_responses(
                        revised.revision_responses,
                        set(pending),
                        {
                            section_id
                            for finding in pending.values()
                            for section_id in finding.target_section_ids
                        },
                    )
                except (OSError, ValueError):
                    revised = None
            chief_revision_number = next_revision
            if revised is None:
                revised, candidate_ref = await _request_chief_revision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    current=current,
                    current_ref=(
                        f"Work/runs/{state['run_id']}/edited-revisions/"
                        f"chief-r{review_round}.json"
                    ),
                    pending=pending,
                    approved_module_text=approved_module_text,
                    claims=claims,
                    aggregate_mode=aggregate_mode,
                    chief_envelope=chief_envelope,
                    chief_session_key=chief_session_key,
                    revision_number=chief_revision_number,
                )
            current = revised
            responses = revised.revision_responses
            exceptional = [
                response
                for response in responses
                if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="final",
                    subject_refs=[candidate_ref],
                    finding_refs=finding_refs,
                    verdicts=[],
                    responses=exceptional,
                    trigger="author_response",
                )
                if decision.decision == "return_to_author":
                    chief_revision_number += 1
                    current, candidate_ref = await _request_chief_revision(
                        runner,
                        state=state,
                        workflow_id=workflow_id,
                        current=current,
                        current_ref=candidate_ref,
                        pending=pending,
                        approved_module_text=approved_module_text,
                        claims=claims,
                        aggregate_mode=aggregate_mode,
                        chief_envelope=chief_envelope,
                        chief_session_key=chief_session_key,
                        revision_number=chief_revision_number,
                    )
                    responses = current.revision_responses
            state["edited_report"] = current
            phase = "recheck"
            review_round += 1
            save_progress("review")

    while True:
        subject_ref = _write_model(
            runner,
            (
                f"Work/runs/{state['run_id']}/edited-revisions/"
                f"chief-r{review_round}.json"
            ),
            current,
        )
        canonical = runner._canonical_markdown(current)
        runner._validate_final_report_structure(
            state, canonical, f"chief-candidate-r{review_round}"
        )
        integrity_ref = (
            f"Work/runs/{state['run_id']}/reviews/"
            f"report-integrity-chief-candidate-r{review_round}.json"
        )
        validation_report = ValidationReport.model_validate_json(
            (runner.service.workspace / integrity_ref).read_text(encoding="utf-8")
        )
        review_input = FinalReviewInput(
            phase=phase,
            run_id=state["run_id"],
            subject_ref=subject_ref,
            subject_revision=review_round,
            subject=edited_report_content_view(current),
            canonical_markdown=strip_runtime_claim_markers(canonical),
            required_section_ids=list(FINAL_REPORT_SECTION_IDS),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
            cross_synthesis_inputs=state.get("cross_synthesis_inputs", []),
            validation_report_ref=integrity_ref,
            validation_report=validation_report,
        )
        input_ref = _write_model(
            runner,
            f"Work/runs/{state['run_id']}/reviews/final-review-input-r{review_round}.json",
            review_input,
        )
        output_kind = (
            "final_review_finding_submission"
            if phase == "initial"
            else "final_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"final-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="chief-editor-auditor",
            objective=(
                "独立审查当前成稿的保真、综合、可追溯、可执行和交付质量。"
                if phase == "initial"
                else "由原 final reviewer 逐项判断 required_findings 是否关闭并检查全文回归。"
            ),
            input_refs=[input_ref, subject_ref, integrity_ref],
            constraints=[
                "只审查总编整合与最终交付质量，不重做模块或 Cross 专业审查",
                "residual_risks 只记录无需内容修订的透明限制",
                (
                    "首轮 checked_section_ids 必须覆盖全部固定章节"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="final_review",
                    target_ids={
                        *FINAL_REPORT_SECTION_IDS,
                        *(claim.id for claim in claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            input_contract_kind="final_review_input",
            input_contract_ref=input_ref,
            context_summary_refs=runner._context_refs(
                state, "chief-editor-auditor"
            ),
            inline_context=runner._template_skill_context(
                state, "core", "synthesis", "visual", "rubric"
            ),
        )
        result = await runner._agent(
            "chief-editor-auditor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        if phase == "initial":
            if not isinstance(result, FinalReviewFindingSubmission):
                raise ReviewLifecycleError("final reviewer returned the wrong initial type")
            if set(result.checked_section_ids) != set(FINAL_REPORT_SECTION_IDS):
                raise ReviewLifecycleError(
                    "initial final review did not cover every fixed section"
                )
            finding_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-findings-r{review_round}.json",
                result,
            )
            finding_refs.append(finding_ref)
            pending = {finding.id: finding for finding in result.findings}
            residual_risks = result.residual_risks
        else:
            if not isinstance(result, FinalReviewVerdictSubmission):
                raise ReviewLifecycleError("final reviewer returned the wrong recheck type")
            _validate_verdicts(result.verdicts, set(pending))
            verdict_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [
                verdict for verdict in result.verdicts if verdict.verdict == "escalate"
            ]
            main_accepts: set[str] = set()
            if escalated:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="final",
                    subject_refs=[subject_ref],
                    finding_refs=finding_refs,
                    verdicts=escalated,
                    responses=responses,
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (
                    verdict.verdict == "escalate"
                    and verdict.finding_id not in main_accepts
                )
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved"
                or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new final finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_model(
                    runner,
                    (
                        f"Work/runs/{state['run_id']}/reviews/"
                        f"final-regression-findings-r{review_round}.json"
                    ),
                    FinalReviewFindingSubmission(
                        checked_section_ids=result.checked_section_ids,
                        findings=result.new_findings,
                        residual_risks=result.residual_risks,
                    ),
                )
                finding_refs.append(new_ref)
            pending = next_pending
            residual_risks = result.residual_risks

        if not pending:
            completion = ReviewCompletionRecord(
                lifecycle="final",
                run_id=state["run_id"],
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key=reviewer_session_key,
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
            )
            completion_ref = _write_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-completion.json",
                completion,
            )
            state["edited_report"] = current
            state["final_review_completion_ref"] = completion_ref
            state["final_residual_risks"] = residual_risks
            save_progress("completed")
            return

        save_progress("revise")
        while True:
            chief_revision_number += 1
            revised, revised_ref = await _request_chief_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                current=current,
                current_ref=subject_ref,
                pending=pending,
                approved_module_text=approved_module_text,
                claims=claims,
                aggregate_mode=aggregate_mode,
                chief_envelope=chief_envelope,
                chief_session_key=chief_session_key,
                revision_number=chief_revision_number,
            )
            current = revised
            subject_ref = revised_ref
            responses = revised.revision_responses
            exceptional = [
                response
                for response in responses
                if response.action in {"disputed", "needs_input"}
            ]
            if not exceptional:
                break
            decision = await _main_exception_decision(
                runner,
                state=state,
                workflow_id=workflow_id,
                scope="final",
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdicts=[],
                responses=exceptional,
                trigger="author_response",
            )
            if decision.decision != "return_to_author":
                break
        state["edited_report"] = current
        phase = "recheck"
        review_round += 1
        save_progress("review")
