"""Finding/response/verdict review lifecycles with no mutable-issue compatibility."""

from __future__ import annotations

import hashlib
import json
import re
from collections import defaultdict
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from pydantic import Field

from .agentic_models import (
    CROSS_REVIEW_DIMENSIONS,
    FINAL_AUDIT_SECTION_IDS,
    ChiefRevisionSubmission,
    CrossReviewFinding,
    CrossReviewFindingSubmission,
    CrossReviewVerdictSubmission,
    CrossSynthesisInput,
    EditedReportSubmission,
    FinalReviewFinding,
    FinalReviewFindingSubmission,
    FinalReviewVerdictSubmission,
    ClaimRecord,
    ModuleReviewFinding,
    ModuleReviewFindingSubmission,
    ModuleReviewVerdictSubmission,
    ModuleRevisionSubmission,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    StrictModel,
    TaskEnvelope,
    WorkflowDecisionSubmission,
)
from .assets import (
    validate_aggregate_retention,
    validate_editor_protection,
    validate_editor_quality,
)
from .input_contracts import (
    ChiefRevisionInput,
    CrossReviewInput,
    FinalAuditSnapshot,
    FinalReviewInput,
    ModuleReviewInput,
    ModuleRevisionDiff,
    ModuleRevisionInput,
    RequestedModuleChange,
    ReviewClaimStatement,
    ReviewCompletionRecord,
    ReviewEvidenceExcerpt,
    ValidationFailure,
    ValidationReport,
    WorkflowExceptionInput,
    final_audit_metadata_view,
    module_content_view,
    strip_runtime_claim_markers,
)
from .models import CHIEF_SECTION_RESULT_PART_IDS
from .revision_diff import build_revision_diff
from .review_preflight import evaluate_module_review_preflight
from .source_ledger import SourceLedger
from .taxonomy import REPORT_TAXONOMY

if TYPE_CHECKING:
    from .workflow import ReportWorkflowRunner


class ReviewLifecycleError(RuntimeError):
    """A deterministic review-protocol failure that requires code or model correction."""


def _require_validation_binding(
    runner: "ReportWorkflowRunner",
    report: ValidationReport,
    *,
    subject_ref: str,
    subject_revision: int,
) -> None:
    """Require one v2 validation report to bind the exact persisted subject bytes."""

    subject_path = runner.service.workspace / subject_ref
    try:
        content_sha256 = hashlib.sha256(subject_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewLifecycleError(
            f"validated subject is unreadable: {subject_ref}"
        ) from exc
    if (
        report.validation_protocol_version < 2
        or report.subject_ref != subject_ref
        or report.subject_revision != subject_revision
        or report.content_sha256 != content_sha256
    ):
        raise ReviewLifecycleError(
            "validation report does not bind the exact final subject; "
            f"subject_ref={subject_ref}; subject_revision={subject_revision}"
        )


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
    reviewer_session_key: str | None = None
    last_reviewed_subject_ref: str | None = None
    review_protocol_version: int = 1


class ModuleLocalRegressionContext(StrictModel):
    """Cross-triggered context needed for a scoped local regression review."""

    prior_review_completion_ref: str
    prior_review_completion: ReviewCompletionRecord
    baseline_subject_ref: str
    trigger_cross_findings: list[CrossReviewFinding] = Field(min_length=1)
    trigger_revision_responses: list[RevisionResponse] = Field(min_length=1)
    revision_diff_ref: str
    revision_diff: ModuleRevisionDiff


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


def _write_immutable_model(
    runner: "ReportWorkflowRunner",
    relative: str,
    model,
) -> str:
    """Persist semantic audit evidence once; identical resume replay is allowed."""

    path = runner.service.workspace / relative
    payload = model.model_dump(mode="json")
    if path.is_file():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise ReviewLifecycleError(
                f"immutable audit artifact is unreadable: {relative}"
            ) from exc
        if existing != payload:
            raise ReviewLifecycleError(
                f"refusing to overwrite immutable audit artifact: {relative}"
            )
        return _relative(runner, path)
    return _relative(runner, runner.service.store.write_json(relative, payload))


def _artifact_sha256(
    runner: "ReportWorkflowRunner",
    refs: Iterable[str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for ref in refs:
        path = (runner.service.workspace / ref).resolve()
        if not path.is_relative_to(runner.service.workspace) or not path.is_file():
            raise ReviewLifecycleError(f"cannot complete review with unreadable artifact: {ref}")
        hashes[ref] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


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


def _apply_chief_patch(
    baseline: EditedReportSubmission,
    patch: ChiefRevisionSubmission,
    *,
    target_section_ids: set[str],
    required_finding_ids: set[str],
) -> EditedReportSubmission:
    """Merge a compact chief patch while inheriting all unassigned report state."""

    if set(patch.section_bodies) != target_section_ids:
        raise ReviewLifecycleError("chief patch must contain exactly the assigned final sections")
    _validate_responses(
        patch.revision_responses,
        required_finding_ids,
        target_section_ids,
    )
    updates = {
        CHIEF_SECTION_RESULT_PART_IDS[section_id]: body
        for section_id, body in patch.section_bodies.items()
    }
    updates["revision_responses"] = list(patch.revision_responses)
    payload = baseline.model_dump(mode="python")
    payload.update(updates)
    return EditedReportSubmission.model_validate(payload)


def _final_audit_markdown(canonical_markdown: str) -> str:
    """Remove the immutable Chapter 2 block from model-visible final-audit prose."""

    chapter_two = "\n## 2. 评估内容描述"
    chapter_three = "\n## 3. 结论与建议"
    start = canonical_markdown.find(chapter_two)
    end = canonical_markdown.find(chapter_three)
    if start < 0 or end < 0 or end <= start:
        raise ReviewLifecycleError("canonical report is missing the fixed Chapter 2/3 boundary")
    return canonical_markdown[:start] + canonical_markdown[end:]


def _final_audit_section_bodies(
    subject: EditedReportSubmission,
) -> dict[str, str]:
    """Return the fixed chief-owned section bodies without repeating Chapter 2."""

    return {
        section_id: strip_runtime_claim_markers(
            getattr(subject, CHIEF_SECTION_RESULT_PART_IDS[section_id]) or ""
        ).rstrip()
        for section_id in FINAL_AUDIT_SECTION_IDS
        if section_id != "4" or subject.special_topic_plan is not None
    }


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model_sha256(model: StrictModel) -> str:
    payload = json.dumps(
        model.model_dump(mode="json"),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _text_sha256(payload)


def _review_claim_statement(claim: ClaimRecord) -> ReviewClaimStatement:
    return ReviewClaimStatement(
        statement_ref=(
            "statement-" + hashlib.sha256(claim.id.encode("utf-8")).hexdigest()[:12]
        ),
        submodule_id=claim.submodule_id,
        text=claim.text,
        statement_type=claim.claim_type,
        evidence_ids=claim.source_ids,
        confidence=claim.confidence,
        unresolved=claim.unresolved,
    )


def _module_recheck_delta(
    baseline: ModuleSubmission,
    current: ModuleSubmission,
    scope: set[str],
) -> dict[str, object]:
    """Build a bounded delta from the last subject seen by the paid reviewer."""

    raw = build_revision_diff(baseline, current)
    changed_narratives = set(raw["changed_submodule_narratives"])
    if not changed_narratives.issubset(scope):
        raise ReviewLifecycleError(
            "module recheck revision changed narratives outside reviewer finding scope: "
            f"{sorted(changed_narratives - scope)}"
        )
    baseline_claims = {claim.id: claim for claim in baseline.claims}
    current_claims = {claim.id: claim for claim in current.claims}
    changed_claim_ids = set(raw["changed_claim_ids"])
    changed_claim_submodules = {
        claim.submodule_id
        for claim_id in changed_claim_ids
        for claim in (
            baseline_claims.get(claim_id),
            current_claims.get(claim_id),
        )
        if claim is not None
    }
    if not changed_claim_submodules.issubset(scope):
        raise ReviewLifecycleError(
            "module recheck revision changed Claims outside reviewer finding scope: "
            f"{sorted(changed_claim_submodules - scope)}"
        )
    current_statements = [
        _review_claim_statement(current_claims[claim_id])
        for claim_id in sorted(changed_claim_ids)
        if claim_id in current_claims
    ]
    prior_statements = [
        _review_claim_statement(baseline_claims[claim_id])
        for claim_id in sorted(changed_claim_ids)
        if claim_id in baseline_claims
    ]
    changed_statement_refs = sorted(
        {
            statement.statement_ref
            for statement in [*current_statements, *prior_statements]
        }
    )
    delta_submodule_ids = changed_narratives | {
        statement.submodule_id for statement in current_statements
    }
    subject_view = module_content_view(current, changed_narratives).model_copy(
        update={
            "evidence_ids_by_submodule": {
                submodule_id: sorted(
                    {
                        evidence_id
                        for statement in current_statements
                        if statement.submodule_id == submodule_id
                        for evidence_id in statement.evidence_ids
                        if evidence_id.startswith("E-")
                    }
                )
                for submodule_id in delta_submodule_ids
            }
        }
    )
    changed_statement_ids = set(changed_statement_refs)
    unchanged_statement_sha256 = {
        statement.statement_ref: _model_sha256(statement)
        for statement in (
            _review_claim_statement(claim)
            for claim in current.claims
            if claim.submodule_id in scope and claim.id not in changed_claim_ids
        )
        if statement.statement_ref not in changed_statement_ids
    }
    revision_diff = ModuleRevisionDiff(
        module_id=current.module_id,
        from_revision=baseline.revision,
        to_revision=current.revision,
        changed_submodule_narratives=sorted(changed_narratives),
        changed_statement_refs=changed_statement_refs,
        evidence_ids_added=raw["source_ids_added"],
        evidence_ids_removed=raw["source_ids_removed"],
    )
    relevant_evidence_ids = {
        evidence_id
        for statement in [*current_statements, *prior_statements]
        for evidence_id in statement.evidence_ids
        if evidence_id.startswith("E-")
    }
    return {
        "subject": subject_view,
        "claim_statements": current_statements,
        "prior_claim_statements": prior_statements,
        "unchanged_submodule_sha256": {
            submodule_id: _text_sha256(
                strip_runtime_claim_markers(
                    current.submodule_narratives[submodule_id]
                ).rstrip()
            )
            for submodule_id in sorted(scope - changed_narratives)
        },
        "unchanged_statement_sha256": unchanged_statement_sha256,
        "revision_diff": revision_diff,
        "relevant_evidence_ids": relevant_evidence_ids,
    }


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
        raise ReviewLifecycleError("module patch base_revision does not match the supplied subject")
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
            raise ReviewLifecycleError(f"module patch removes unknown Claim id: {claim_id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(f"module patch removes out-of-scope Claim id: {claim_id}")
        del baseline_claims[claim_id]
    for claim in patch.claims_upsert:
        if claim.module_id != baseline.module_id:
            raise ReviewLifecycleError(f"module patch Claim belongs to another module: {claim.id}")
        if claim.submodule_id not in target_submodule_ids:
            raise ReviewLifecycleError(f"module patch changes out-of-scope Claim id: {claim.id}")
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
    *,
    id_prefix: str,
) -> None:
    _unique_ids((finding.id for finding in findings), label="module findings")
    for finding in findings:
        if not finding.id.startswith(id_prefix):
            raise ReviewLifecycleError(
                f"module finding id must start with {id_prefix}: {finding.id}"
            )
        if finding.target_submodule_id not in scope:
            raise ReviewLifecycleError(f"module finding targets unreviewed submodule: {finding.id}")


def _module_review_evidence_packet(
    runner: "ReportWorkflowRunner",
    subject: ModuleSubmission,
    run_id: str,
    submodule_ids: set[str],
    *,
    evidence_ids: set[str] | None = None,
) -> list[ReviewEvidenceExcerpt]:
    """Attach cited E-* evidence once so review does not become a retrieval loop."""

    ledger = SourceLedger(runner.service.workspace, run_id)
    records = {record.id: record for record in ledger.records}
    selected_evidence_ids = sorted(
        evidence_ids
        if evidence_ids is not None
        else {
            source_id
            for claim in subject.claims
            if claim.submodule_id in submodule_ids
            for source_id in claim.source_ids
            if source_id.startswith("E-")
        }
    )
    packet: list[ReviewEvidenceExcerpt] = []
    for evidence_id in selected_evidence_ids:
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


def _module_review_knowledge_packet(
    runner: "ReportWorkflowRunner",
    state: dict,
    module_id: str,
    submodule_ids: set[str],
) -> tuple[str | None, str]:
    """Return only taxonomy sections relevant to the current module-review scope."""

    knowledge_ref = state.get("module_knowledge_refs", {}).get(module_id)
    if not knowledge_ref:
        return None, ""
    path = runner.service.workspace / knowledge_ref
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ReviewLifecycleError(
            f"module review Knowledge is unreadable: {knowledge_ref}"
        ) from exc

    selected: list[str] = []
    current_selected = True
    for line in text.splitlines():
        match = re.match(r"^##\s+(2\.[1-5](?:\.\d+)+)\b", line)
        if match:
            current_selected = match.group(1) in submodule_ids
        if current_selected:
            selected.append(line)
    bounded = "\n".join(selected).strip()
    if len(bounded) > 14_000:
        bounded = (
            bounded[:14_000].rsplit("\n", 1)[0]
            + "\n\n[审计知识上下文已按成本上限截断]"
        )
    return knowledge_ref, bounded


def _module_reviewer_session_key(
    runner: "ReportWorkflowRunner",
    *,
    state: dict,
    module_id: str,
    lifecycle_id: str,
    progress: ModuleReviewProgress | None,
    regression_context: ModuleLocalRegressionContext | None,
) -> str:
    """Reuse the module's original reviewer while preserving legacy resumes."""

    stable = f"module-auditor-{module_id}"
    def require_module_identity(value: str) -> str:
        if value != stable and not value.startswith(f"{stable}-"):
            raise ReviewLifecycleError(
                f"module review progress belongs to another reviewer: {value}"
            )
        return value

    if regression_context is not None:
        return require_module_identity(
            regression_context.prior_review_completion.reviewer_session_key
        )
    if progress is not None and progress.reviewer_session_key:
        return require_module_identity(progress.reviewer_session_key)
    registry_path = (
        runner.service.workspace
        / f"Work/runs/{state['run_id']}/agent-identities.json"
    )
    if not registry_path.is_file():
        return stable
    try:
        identities = json.loads(
            registry_path.read_text(encoding="utf-8")
        ).get("identities", {})
    except (OSError, ValueError, AttributeError):
        return stable
    if stable in identities:
        return stable
    current_legacy = f"{stable}-{lifecycle_id}"
    if progress is not None and current_legacy in identities:
        return current_legacy
    initial_legacy = f"{stable}-initial"
    if initial_legacy in identities:
        return initial_legacy
    return stable


def _validate_verdicts(
    verdicts: list[ResolutionVerdict],
    required_ids: set[str],
) -> None:
    verdict_ids = _unique_ids((verdict.finding_id for verdict in verdicts), label="review verdicts")
    if verdict_ids != required_ids:
        raise ReviewLifecycleError(
            "review verdicts must cover exactly the required findings; "
            f"missing={sorted(required_ids - verdict_ids)}; "
            f"unexpected={sorted(verdict_ids - required_ids)}"
        )


def _validate_final_findings(
    findings: list[FinalReviewFinding],
    allowed_section_ids: set[str],
) -> None:
    _unique_ids((finding.id for finding in findings), label="final-review findings")
    for finding in findings:
        invalid = sorted(set(finding.target_section_ids) - allowed_section_ids)
        if invalid:
            raise ReviewLifecycleError(
                "final-review finding targets an inactive report section: "
                f"finding={finding.id}; sections={invalid}"
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
        {verdict.finding_id for verdict in verdicts if verdict.verdict == "escalate"}
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
    validation_target_submodule_ids: set[str] | None = None,
) -> tuple[ModuleSubmission, str]:
    module_findings = module_findings or []
    cross_findings = cross_findings or []
    requested_changes = requested_changes or []
    validation_target_submodule_ids = validation_target_submodule_ids or set()
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
        *(target_id for finding in cross_findings for target_id in finding.target_submodule_ids),
        *(target_id for change in requested_changes for target_id in change.target_submodule_ids),
        *validation_target_submodule_ids,
    }
    if validation_report is not None:
        _require_validation_binding(
            runner,
            validation_report,
            subject_ref=(
                f"Work/runs/{state['run_id']}/modules/"
                f"{subject.module_id}-r{subject.revision}.json"
            ),
            subject_revision=subject.revision,
        )
        if not targets:
            raise ReviewLifecycleError(
                "failed machine validation requires explicit module-local correction targets"
            )
    revision_input = ModuleRevisionInput(
        run_id=state["run_id"],
        module_id=subject.module_id,
        subject_ref=(
            f"Work/runs/{state['run_id']}/modules/{subject.module_id}-r{subject.revision}.json"
        ),
        subject=module_content_view(subject, targets),
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
    input_refs = [input_ref]
    specialist_id = f"module-{subject.module_id}-specialist"
    envelope = TaskEnvelope(
        task_id=f"module-{subject.module_id}-revision-r{subject.revision + 1}",
        run_id=state["run_id"],
        agent_id=specialist_id,
        objective=(
            "只修复确定性 preflight 中可复现的机器谓词失败。"
            if validation_report is not None and not required_ids
            else "按结构化 finding 和机器谓词对当前模块执行显式、定向的补丁修订。"
        ),
        input_refs=input_refs,
        constraints=[
            "只提交小型 module_revision_submission commit；不得在其中重复正文",
            "assigned target_submodule_id 只用 write_result_part 逐项保存完整替换正文和 evidence_ids；先用 list_result_parts 确认状态，ready 项不得重写",
            "最终提交只使用 schema 声明的简短字段；运行时从保存的小节自动生成补丁",
            (
                "本次只有机器 preflight 触发；revision_responses 必须为空，"
                "不得伪造 reviewer finding 或 verdict"
                if not required_ids
                else "revision_responses 必须逐项且仅覆盖 assigned finding ids"
            ),
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
        artifact_delivery_modes={
            input_ref: "inline",
            revision_input.subject_ref: "hash_retained",
        },
        target_submodule_ids=sorted(targets),
        input_contract_kind="module_revision_input",
        input_contract_ref=input_ref,
        inline_context=runner._role_skill_context(state, "module-author"),
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
        (f"Work/runs/{state['run_id']}/modules/{subject.module_id}-r{revised.revision}.json"),
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
    lifecycle_id: str,
) -> str:
    refs = [subject_ref, *finding_refs, *verdict_refs]
    completion = ReviewCompletionRecord(
        review_protocol_version=2,
        lifecycle="module",
        run_id=state["run_id"],
        reviewer_agent_id="evidence-auditor",
        reviewer_session_key=reviewer_session_key,
        subject_refs=[subject_ref],
        finding_refs=finding_refs,
        verdict_refs=verdict_refs,
        resolved_finding_ids=sorted(resolved_ids),
        artifact_sha256=_artifact_sha256(runner, refs),
    )
    ref = _write_immutable_model(
        runner,
        (
            f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/"
            f"{module.module_id}/completion-r{module.revision}.json"
        ),
        completion,
    )
    state.setdefault("module_review_completion_refs", {})[module.module_id] = ref
    runner.service.store.write_text(f"Outputs/Modules/{module.module_id}.md", module.markdown)
    return ref


async def run_module_review(
    runner: "ReportWorkflowRunner",
    module_id: str,
    payload: ModuleSubmission,
    state: dict,
    workflow_id: str,
    *,
    initial_scope: set[str],
    lifecycle_id: str,
    regression_context: ModuleLocalRegressionContext | None = None,
) -> ModuleSubmission:
    """Run module-local finding/response/verdict closure with one reviewer session."""

    if payload.module_id != module_id:
        raise ReviewLifecycleError("module review payload belongs to a different module")
    if not re.fullmatch(r"[a-z0-9-]+", lifecycle_id):
        raise ReviewLifecycleError("module review lifecycle_id is not a safe component")
    current = payload
    pending: dict[str, ModuleReviewFinding] = {}
    responses: list[RevisionResponse] = []
    finding_refs: list[str] = []
    verdict_refs: list[str] = []
    resolved_ids: set[str] = set()
    review_round = 0
    phase = "local_regression" if regression_context is not None else "initial"
    scope = set(initial_scope)
    last_reviewed_subject_ref: str | None = None
    review_root = f"Work/runs/{state['run_id']}/reviews/module/{lifecycle_id}/{module_id}"
    progress_ref = f"{review_root}/progress.json"

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
                reviewer_session_key=reviewer_session_key,
                last_reviewed_subject_ref=last_reviewed_subject_ref,
                review_protocol_version=2,
            ),
        )

    progress = (
        _load_progress(runner, progress_ref, ModuleReviewProgress) if state.get("resume") else None
    )
    reviewer_session_key = _module_reviewer_session_key(
        runner,
        state=state,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        progress=progress,
        regression_context=regression_context,
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
        last_reviewed_subject_ref = progress.last_reviewed_subject_ref
        if progress.next_action == "revise":
            candidate_path = runner.service.workspace / (
                f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision + 1}.json"
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
                        {finding.target_submodule_id for finding in pending.values()},
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
                response for response in responses if response.action in {"disputed", "needs_input"}
            ]
            if exceptional:
                decision = await _main_exception_decision(
                    runner,
                    state=state,
                    workflow_id=workflow_id,
                    scope="module",
                    subject_refs=[
                        f"Work/runs/{state['run_id']}/modules/{module_id}-r{current.revision}.json"
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
            scope = {finding.target_submodule_id for finding in pending.values()}
            phase = "recheck"
            review_round += 1
            save_progress("review")

    while True:
        machine_attempts = 0
        machine_failure_fingerprints: dict[tuple, int] = {}
        while True:
            subject_ref = (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{current.revision}.json"
            )
            if not (runner.service.workspace / subject_ref).is_file():
                _write_model(runner, subject_ref, current)
            structure_ref = runner._validate_module_structure(
                state,
                current,
                f"review-r{review_round}",
            )
            structure_report = ValidationReport.model_validate_json(
                (runner.service.workspace / structure_ref).read_text(encoding="utf-8")
            )
            _require_validation_binding(
                runner,
                structure_report,
                subject_ref=subject_ref,
                subject_revision=current.revision,
            )
            preflight = evaluate_module_review_preflight(
                runner.service.workspace,
                run_id=state["run_id"],
                subject=current,
                subject_ref=subject_ref,
                upstream_report=structure_report,
            )
            signal_ref = _write_model(
                runner,
                (
                    f"{review_root}/preflight-subject-r{current.revision}-"
                    f"review-r{review_round}.json"
                ),
                preflight.report,
            )
            validation_report = preflight.report
            if validation_report.passed:
                break
            machine_attempts += 1
            fingerprint = tuple(
                sorted(
                    (
                        failure.check_id,
                        failure.target_path,
                        failure.message,
                    )
                    for failure in validation_report.failures
                )
            )
            repeated = machine_failure_fingerprints.get(fingerprint, 0) + 1
            machine_failure_fingerprints[fingerprint] = repeated
            if repeated >= 2 or machine_attempts >= 3:
                raise ReviewLifecycleError(
                    "module preflight failed repeatedly before semantic review; "
                    "no reviewer finding or verdict was created. "
                    f"module={module_id}; attempts={machine_attempts}; "
                    f"validation_ref={signal_ref}"
                )
            current, _ = await request_module_revision(
                runner,
                state=state,
                workflow_id=workflow_id,
                subject=current,
                module_findings=(
                    list(pending.values()) if phase == "recheck" else []
                ),
                cross_findings=(
                    regression_context.trigger_cross_findings
                    if phase == "local_regression"
                    and regression_context is not None
                    else []
                ),
                validation_ref=signal_ref,
                validation_target_submodule_ids=set(
                    preflight.target_submodule_ids
                ),
            )
            if phase == "recheck":
                responses = current.revision_responses
            save_progress("review")

        review_subject = module_content_view(current, scope)
        review_claim_statements = [
            _review_claim_statement(claim)
            for claim in current.claims
            if claim.submodule_id in scope
        ]
        prior_claim_statements: list[ReviewClaimStatement] = []
        unchanged_submodule_sha256: dict[str, str] = {}
        unchanged_statement_sha256: dict[str, str] = {}
        baseline_subject_ref: str | None = None
        revision_diff_ref: str | None = None
        revision_diff: ModuleRevisionDiff | None = None
        relevant_evidence_ids: set[str] | None = None
        if phase == "recheck":
            baseline_subject_ref = last_reviewed_subject_ref
            if baseline_subject_ref is None:
                legacy_candidate = (
                    f"Work/runs/{state['run_id']}/modules/"
                    f"{module_id}-r{current.revision - 1}.json"
                )
                if current.revision > 0 and (
                    runner.service.workspace / legacy_candidate
                ).is_file():
                    baseline_subject_ref = legacy_candidate
                else:
                    raise ReviewLifecycleError(
                        "module recheck lacks the last subject seen by its reviewer"
                    )
            try:
                baseline = ModuleSubmission.model_validate_json(
                    (
                        runner.service.workspace / baseline_subject_ref
                    ).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    "module recheck last-reviewed baseline is unreadable: "
                    f"{baseline_subject_ref}"
                ) from exc
            delta = _module_recheck_delta(baseline, current, scope)
            review_subject = delta["subject"]
            review_claim_statements = delta["claim_statements"]
            prior_claim_statements = delta["prior_claim_statements"]
            unchanged_submodule_sha256 = delta["unchanged_submodule_sha256"]
            unchanged_statement_sha256 = delta["unchanged_statement_sha256"]
            revision_diff = delta["revision_diff"]
            relevant_evidence_ids = set(delta["relevant_evidence_ids"])
            relevant_evidence_ids.update(
                evidence_ref
                for finding in pending.values()
                for evidence_ref in finding.evidence_refs
                if evidence_ref.startswith("E-")
            )
            revision_diff_ref = _write_model(
                runner,
                f"{review_root}/recheck-diff-r{review_round}.json",
                revision_diff,
            )
        # ReportingAgentRunner deliberately resets provider working memory at
        # every typed task boundary.  A stable reviewer identity therefore does
        # not retain the initial Knowledge slice.  Re-send the same bounded,
        # immutable taxonomy packet alongside the delta; unchanged subject
        # prose and evidence remain hash-only/delta-only.
        knowledge_ref, knowledge_context = _module_review_knowledge_packet(
            runner,
            state,
            module_id,
            scope,
        )
        review_input = ModuleReviewInput(
            phase=phase,
            run_id=state["run_id"],
            module_id=module_id,
            lifecycle_id=lifecycle_id,
            review_round=review_round,
            subject_ref=subject_ref,
            subject_revision=current.revision,
            subject=review_subject,
            claim_statements=review_claim_statements,
            prior_claim_statements=prior_claim_statements,
            unchanged_submodule_sha256=unchanged_submodule_sha256,
            unchanged_statement_sha256=unchanged_statement_sha256,
            knowledge_ref=knowledge_ref,
            knowledge_context=knowledge_context,
            evidence=_module_review_evidence_packet(
                runner,
                current,
                state["run_id"],
                scope,
                evidence_ids=relevant_evidence_ids,
            ),
            required_submodule_ids=sorted(scope),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
            prior_review_completion_ref=(
                regression_context.prior_review_completion_ref
                if phase == "local_regression" and regression_context is not None
                else None
            ),
            prior_review_completion=(
                regression_context.prior_review_completion
                if phase == "local_regression" and regression_context is not None
                else None
            ),
            baseline_subject_ref=(
                regression_context.baseline_subject_ref
                if phase == "local_regression" and regression_context is not None
                else baseline_subject_ref
            ),
            trigger_cross_findings=(
                regression_context.trigger_cross_findings
                if phase == "local_regression" and regression_context is not None
                else []
            ),
            trigger_revision_responses=(
                regression_context.trigger_revision_responses
                if phase == "local_regression" and regression_context is not None
                else []
            ),
            revision_diff_ref=(
                regression_context.revision_diff_ref
                if phase == "local_regression" and regression_context is not None
                else revision_diff_ref
            ),
            revision_diff=(
                regression_context.revision_diff
                if phase == "local_regression" and regression_context is not None
                else revision_diff
            ),
            validation_report_ref=signal_ref,
            validation_report=validation_report,
        )
        input_ref = _write_model(
            runner,
            f"{review_root}/input-r{review_round}.json",
            review_input,
        )
        output_kind = (
            "module_review_finding_submission"
            if phase in {"initial", "local_regression"}
            else "module_review_verdict_submission"
        )
        envelope = TaskEnvelope(
            task_id=f"module-{module_id}-{lifecycle_id}-review-r{review_round}",
            run_id=state["run_id"],
            agent_id="evidence-auditor",
            objective=(
                f"审查模块 {module_id} 的当前正文、Claim 与证据边界。"
                if phase == "initial"
                else (
                    f"由模块 {module_id} 的原审查者仅检查 Cross 回改范围、diff、"
                    "Claim 与证据回归。"
                    if phase == "local_regression"
                    else (
                        f"只对模块 {module_id} 的 required_findings 返回逐项 verdict，"
                        "并检查修改回归。"
                    )
                )
            ),
            input_refs=[input_ref],
            constraints=[
                "coverage 记录实际检查范围，不是批准状态",
                "finding 首次提出后不可改写；复审不得复述旧 finding",
                "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
                "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
                (
                    "首轮必须覆盖 input 中全部 required_submodule_ids"
                    if phase in {"initial", "local_regression"}
                    else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                ),
                *(
                    [
                        "这是原模块审查者的 local_regression，不得重新审查未修改小节",
                        "只根据 prior completion、Cross finding/作者响应、revision diff、目标 Claim/证据和当前 validation 提出真实回归 finding",
                    ]
                    if phase == "local_regression"
                    else []
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
            artifact_delivery_modes={
                input_ref: "inline",
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            target_submodule_ids=sorted(scope),
            input_contract_kind="module_review_input",
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "module-auditor"),
            allowed_tools=["submit_result"],
        )
        result = await runner._agent(
            "evidence-auditor",
            envelope,
            envelope.input_refs,
            workflow_id,
            session_key=reviewer_session_key,
        )
        # This exact subject, not merely the immediately preceding revision, is
        # the semantic baseline for the next paid reviewer turn. Machine-only
        # corrections may introduce additional revisions between the two.
        last_reviewed_subject_ref = subject_ref
        if phase in {"initial", "local_regression"}:
            if not isinstance(result, ModuleReviewFindingSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong initial type")
            if not scope.issubset(set(result.coverage.submodule_ids)):
                raise ReviewLifecycleError(
                    "initial module review coverage omitted assigned submodules"
                )
            _validate_module_findings(
                result.findings,
                current,
                scope,
                id_prefix=f"M-{module_id}-{lifecycle_id}-r{review_round}-",
            )
            review_ref = _write_immutable_model(
                runner,
                f"{review_root}/findings-r{review_round}.json",
                result,
            )
            finding_refs.append(review_ref)
            pending = {finding.id: finding for finding in result.findings}
        else:
            if not isinstance(result, ModuleReviewVerdictSubmission):
                raise ReviewLifecycleError("module auditor returned the wrong recheck type")
            required_ids = set(pending)
            _validate_verdicts(result.verdicts, required_ids)
            _validate_module_findings(
                result.new_findings,
                current,
                scope,
                id_prefix=f"M-{module_id}-{lifecycle_id}-r{review_round}-",
            )
            verdict_ref = _write_immutable_model(
                runner,
                f"{review_root}/verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
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
                or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new module finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
                    runner,
                    f"{review_root}/regression-findings-r{review_round}.json",
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
                lifecycle_id=lifecycle_id,
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
                response for response in responses if response.action in {"disputed", "needs_input"}
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
        scope = {finding.target_submodule_id for finding in pending.values()}
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
    subject_path = runner.service.workspace / subject_ref
    try:
        content_sha256 = hashlib.sha256(subject_path.read_bytes()).hexdigest()
    except OSError as exc:
        raise ReviewLifecycleError(
            f"Cross machine-check subject is unreadable: {subject_ref}"
        ) from exc
    failures: list[ValidationFailure] = []
    check_ids: list[str] = []
    for finding in findings:
        for index, check in enumerate(finding.machine_checks, start=1):
            check_id = f"{finding.id}:machine:{index}"
            check_ids.append(check_id)
            values = [_resolve_subject_path(subject, path) for path in check.target_paths]
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
                missing = [term for term in check.expected_values if term not in combined]
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
        validation_protocol_version=2,
        run_id=state["run_id"],
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        content_sha256=content_sha256,
        validator="explicit-cross-predicates/v2",
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


def _validate_cross_synthesis_portfolio(
    synthesis_inputs: list[CrossSynthesisInput],
    modules: dict[str, ModuleSubmission],
) -> None:
    """Require system synthesis rather than treating six-dimension coverage as evidence."""

    if len(synthesis_inputs) < 2:
        raise ReviewLifecycleError(
            "Cross completion requires at least two supported system relationships"
        )
    kinds = {item.cluster_type for item in synthesis_inputs}
    required_kinds = {"risk_cluster", "global_propagation"}
    if not required_kinds.issubset(kinds):
        raise ReviewLifecycleError(
            "Cross synthesis portfolio requires a risk cluster and a global propagation "
            f"chain; missing={sorted(required_kinds - kinds)}"
        )


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
    progress_ref = f"Work/runs/{state['run_id']}/reviews/cross-progress.json"

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
        _load_progress(runner, progress_ref, CrossReviewProgress) if state.get("resume") else None
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
            reviewed_baseline = current
            baseline_subject_ref = (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{reviewed_baseline.revision}.json"
            )
            prior_completion_ref = state.get(
                "module_review_completion_refs",
                {},
            ).get(module_id)
            if not prior_completion_ref:
                raise ReviewLifecycleError(
                    f"Cross local regression requires prior module review: {module_id}"
                )
            try:
                prior_completion = ReviewCompletionRecord.model_validate_json(
                    (
                        runner.service.workspace / prior_completion_ref
                    ).read_text(encoding="utf-8")
                )
            except (OSError, ValueError) as exc:
                raise ReviewLifecycleError(
                    f"Cross local regression prior completion is unreadable: {module_id}"
                ) from exc
            if (
                prior_completion.lifecycle != "module"
                or prior_completion.run_id != state["run_id"]
                or baseline_subject_ref not in prior_completion.subject_refs
            ):
                raise ReviewLifecycleError(
                    f"Cross local regression prior completion does not bind {module_id}"
                )
            validation_ref: str | None = None
            machine_attempts = 0
            machine_failure_fingerprints: dict[tuple, int] = {}
            persisted_candidate: ModuleSubmission | None = None
            modules_root = runner.service.workspace / f"Work/runs/{state['run_id']}/modules"
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
                persisted_candidate = max(candidates, key=lambda item: item.revision)
            while True:
                machine_attempts += 1
                if persisted_candidate is not None:
                    revised = persisted_candidate
                    revised_ref = (
                        f"Work/runs/{state['run_id']}/modules/{module_id}-r{revised.revision}.json"
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
                    (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
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
            local_scope = {
                target_id
                for finding in grouped[module_id]
                for target_id in finding.target_submodule_ids
            }
            local_diff_ref = (
                f"Work/runs/{state['run_id']}/reviews/module/cross-r{review_round}/"
                f"{module_id}/trigger-diff-r{revised.revision}.json"
            )
            raw_local_diff = build_revision_diff(reviewed_baseline, revised)
            local_diff = ModuleRevisionDiff(
                module_id=raw_local_diff["module_id"],
                from_revision=raw_local_diff["from_revision"],
                to_revision=raw_local_diff["to_revision"],
                changed_submodule_narratives=raw_local_diff[
                    "changed_submodule_narratives"
                ],
                changed_statement_refs=[
                    "statement-"
                    + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
                    for value in raw_local_diff["changed_claim_ids"]
                ],
                evidence_ids_added=raw_local_diff["source_ids_added"],
                evidence_ids_removed=raw_local_diff["source_ids_removed"],
            )
            runner.service.store.write_json(
                local_diff_ref,
                local_diff.model_dump(mode="json"),
            )
            local_reviewed = await run_module_review(
                runner,
                module_id,
                revised,
                state,
                workflow_id,
                initial_scope=local_scope,
                lifecycle_id=f"cross-r{review_round}",
                regression_context=ModuleLocalRegressionContext(
                    prior_review_completion_ref=prior_completion_ref,
                    prior_review_completion=prior_completion,
                    baseline_subject_ref=baseline_subject_ref,
                    trigger_cross_findings=grouped[module_id],
                    trigger_revision_responses=revised.revision_responses,
                    revision_diff_ref=local_diff_ref,
                    revision_diff=local_diff,
                ),
            )
            modules[module_id] = local_reviewed
            state["module_submissions"][module_id] = local_reviewed
            state.setdefault("specialist_submissions", {})[module_id] = local_reviewed
            final_subject_ref = (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{local_reviewed.revision}.json"
            )
            validation_ref = _run_cross_machine_checks(
                runner,
                state=state,
                subject=local_reviewed,
                subject_ref=final_subject_ref,
                findings=grouped[module_id],
            )
            final_validation = ValidationReport.model_validate_json(
                (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
            )
            _require_validation_binding(
                runner,
                final_validation,
                subject_ref=final_subject_ref,
                subject_revision=local_reviewed.revision,
            )
            if not final_validation.passed:
                raise ReviewLifecycleError(
                    "Cross machine validation failed after the module-local regression "
                    "review; the workflow stopped before Cross recheck without starting "
                    "another author revision. "
                    f"module={module_id}; validation_ref={validation_ref}"
                )
            machine_refs.append(validation_ref)
            local_review_refs[module_id] = state["module_review_completion_refs"][module_id]
            revised_owner_ids.add(module_id)
            save_progress("revise")

    if progress is not None and progress.next_action == "revise":
        await complete_revision_wave()
        phase = "recheck"
        review_round += 1
        save_progress("review")

    while True:
        module_refs = {
            module_id: (
                f"Work/runs/{state['run_id']}/modules/"
                f"{module_id}-r{modules[module_id].revision}.json"
            )
            for module_id in REPORT_TAXONOMY
        }
        changed_module_ids = set(REPORT_TAXONOMY) if phase == "initial" else set(revised_owner_ids)
        if phase == "recheck" and not changed_module_ids:
            raise ReviewLifecycleError(
                "Cross recheck requires the current revision wave module ids"
            )
        unchanged_module_sha256 = (
            {}
            if phase == "initial"
            else {
                module_id: hashlib.sha256(
                    (runner.service.workspace / module_refs[module_id]).read_bytes()
                ).hexdigest()
                for module_id in set(REPORT_TAXONOMY) - changed_module_ids
            }
        )
        machine_validation_reports: list[ValidationReport] = []
        if phase == "recheck":
            for validation_ref in machine_refs:
                report = ValidationReport.model_validate_json(
                    (runner.service.workspace / validation_ref).read_text(encoding="utf-8")
                )
                _require_validation_binding(
                    runner,
                    report,
                    subject_ref=report.subject_ref,
                    subject_revision=report.subject_revision
                    if report.subject_revision is not None
                    else -1,
                )
                machine_validation_reports.append(report)
        cross_input = CrossReviewInput(
            phase=phase,
            run_id=state["run_id"],
            module_refs=module_refs,
            module_revisions={
                module_id: modules[module_id].revision for module_id in REPORT_TAXONOMY
            },
            modules={
                module_id: module_content_view(modules[module_id])
                for module_id in changed_module_ids
            },
            changed_module_ids=sorted(changed_module_ids),
            unchanged_module_sha256=unchanged_module_sha256,
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses_by_module=responses_by_module if phase == "recheck" else {},
            local_regression_review_refs=local_review_refs if phase == "recheck" else {},
            prior_synthesis_inputs=prior_synthesis if phase == "recheck" else [],
            machine_validation_refs=machine_refs if phase == "recheck" else [],
            machine_validation_reports=machine_validation_reports,
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
            input_refs=[input_ref],
            constraints=[
                "coverage 只记录审查范围，不含 integrated 或 approved 状态",
                "findings 只包含必须写回责任模块的问题",
                "synthesis_inputs 只包含无需模块返工、可供总编综合的已支持关系",
                (
                    "recheck 只接收 changed_module_ids 的当前正文；未修改模块由首轮"
                    "不可变 finding/synthesis 输入及当前 SHA-256 证明保持不变，"
                    "不得依赖或要求进程内对话记忆"
                ),
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
                        *(claim.id for module in modules.values() for claim in module.claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            artifact_delivery_modes={
                input_ref: "inline",
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            input_contract_kind="cross_review_input",
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "cross-reviewer"),
            allowed_tools=["submit_result"],
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
            finding_ref = _write_immutable_model(
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
            verdict_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/cross-verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
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
                        response for values in responses_by_module.values() for response in values
                    ],
                )
                if decision.decision == "accept_dispute":
                    main_accepts = set(decision.finding_ids)
            next_pending = {
                verdict.finding_id: pending[verdict.finding_id]
                for verdict in result.verdicts
                if verdict.verdict == "open"
                or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new Cross finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
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
            _validate_cross_synthesis_portfolio(prior_synthesis, modules)
            completion_refs = [
                *module_refs.values(),
                *finding_refs,
                *verdict_refs,
            ]
            completion = ReviewCompletionRecord(
                lifecycle="cross",
                run_id=state["run_id"],
                reviewer_agent_id="cross-module-reviewer",
                reviewer_session_key=reviewer_session_key,
                subject_refs=list(module_refs.values()),
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
                artifact_sha256=_artifact_sha256(runner, completion_refs),
            )
            completion_ref = _write_immutable_model(
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
        section_id for finding in pending.values() for section_id in finding.target_section_ids
    }
    consistency_section_ids = {"1.1", "1.2", "1.3"}
    if any(section_id.startswith(("3.", "4.")) for section_id in target_sections):
        consistency_section_ids.update({"3.1.1", "3.1.3", "3.2"})
    consistency_section_ids.difference_update(target_sections)
    revision_input = ChiefRevisionInput(
        run_id=state["run_id"],
        subject_ref=current_ref,
        target_section_bodies={
            section_id: getattr(
                current,
                CHIEF_SECTION_RESULT_PART_IDS[section_id],
            )
            for section_id in sorted(target_sections)
        },
        consistency_context={
            section_id: getattr(
                current,
                CHIEF_SECTION_RESULT_PART_IDS[section_id],
            )
            for section_id in sorted(consistency_section_ids)
        },
        revision=revision_number,
        target_section_ids=sorted(target_sections),
        findings=list(pending.values()),
    )
    revision_input_ref = _write_model(
        runner,
        (f"Work/runs/{state['run_id']}/reviews/chief-revision-input-r{revision_number}.json"),
        revision_input,
    )
    revision_context = runner._role_skill_context(
        state,
        "chief-revision",
        target_section_ids=target_sections,
    )
    revision_envelope = TaskEnvelope.model_validate(
        chief_envelope.model_copy(
            update={
                "task_id": f"chief-edit-r{revision_number}",
                "objective": "按 final review findings 仅修订指定的实际总编小节。",
                "input_refs": [revision_input_ref],
                "allowed_outputs": ["chief_revision_submission"],
                "allowed_tools": [
                    "write_result_part",
                    "list_result_parts",
                    "submit_result",
                ],
                "revision": revision_number,
                "prior_result_ref": current_ref,
                "artifact_delivery_modes": {
                    revision_input_ref: "inline",
                    current_ref: "hash_retained",
                },
                "target_submodule_ids": [],
                "input_contract_kind": "chief_revision_input",
                "input_contract_ref": revision_input_ref,
                "constraints": [
                    "只为 target_section_ids 调用 write_result_part 逐项保存并提交 chief_revision_submission 小补丁；先用 list_result_parts 确认状态，ready 项不得重写",
                    "不得提交全文、第二章、表格、图片或其他元数据；运行时确定性继承",
                    "revision_responses 必须逐项且仅覆盖 assigned finding ids",
                    "不得让工作流替你补写响应、章节或引用",
                    (
                        "章节 ID 与 part_id 的固定映射为："
                        + "；".join(
                            f"{section_id} -> {CHIEF_SECTION_RESULT_PART_IDS[section_id]}"
                            for section_id in sorted(target_sections)
                        )
                    ),
                    *runner._user_supplement_constraints(
                        state,
                        stage="chief_edit",
                        target_ids=target_sections,
                    ),
                ],
                "context_summary_refs": [],
                "inline_context": revision_context,
            },
        ).model_dump(mode="python")
    )
    revised = await runner._agent(
        "chief-editor",
        revision_envelope,
        revision_envelope.input_refs,
        workflow_id,
        session_key=chief_session_key,
    )
    if not isinstance(revised, ChiefRevisionSubmission):
        raise ReviewLifecycleError("chief editor returned the wrong revision type")
    if revised.base_subject_ref != current_ref or revised.revision != revision_number:
        raise ReviewLifecycleError("chief editor patch targets the wrong subject revision")
    revised_report = _apply_chief_patch(
        current,
        revised,
        target_section_ids=target_sections,
        required_finding_ids=set(pending),
    )
    diff = runner._final_revision_diff(current, revised_report)
    unexpected_sections = sorted(set(diff["changed_section_ids"]) - target_sections)
    allowed_contract_fields = {"revision_responses"}
    unexpected_contract = sorted(set(diff["changed_contract_fields"]) - allowed_contract_fields)
    if unexpected_sections or unexpected_contract:
        raise ReviewLifecycleError(
            "chief revision changed content outside finding scope: "
            f"sections={unexpected_sections}; contract_fields={unexpected_contract}"
        )
    if aggregate_mode:
        state["editor_quality_observations"] = validate_aggregate_retention(
            revised_report, approved_module_text
        )
        if claims:
            validate_editor_protection(revised_report, claims)
    else:
        validate_editor_protection(revised_report, claims)
        state["editor_quality_observations"] = validate_editor_quality(
            revised_report, state["module_submissions"]
        )
    revised_ref = _write_model(
        runner,
        (f"Work/runs/{state['run_id']}/edited-revisions/chief-author-r{revision_number}.json"),
        revised_report,
    )
    return revised_report, revised_ref


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
    restart_round = state.get("final_review_restart_round")
    if restart_round is not None:
        review_round = int(restart_round)
        chief_revision_number = int(restart_round)
        progress_ref = f"Work/runs/{state['run_id']}/reviews/final-progress-r{review_round}.json"
    else:
        progress_ref = f"Work/runs/{state['run_id']}/reviews/final-progress.json"

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
        _load_progress(runner, progress_ref, FinalReviewProgress) if state.get("resume") else None
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
                f"Work/runs/{state['run_id']}/edited-revisions/chief-author-r{next_revision}.json"
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
                        f"Work/runs/{state['run_id']}/edited-revisions/chief-r{review_round}.json"
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
                response for response in responses if response.action in {"disputed", "needs_input"}
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
        required_audit_sections = tuple(
            section_id
            for section_id in FINAL_AUDIT_SECTION_IDS
            if section_id != "4" or current.special_topic_plan is not None
        )
        audited_chapters = (
            "第一、三、四章"
            if current.special_topic_plan is not None
            else "第一、三章"
        )
        subject_ref = _write_model(
            runner,
            (f"Work/runs/{state['run_id']}/edited-revisions/chief-r{review_round}.json"),
            current,
        )
        _, canonical = runner._delivery_projection(state, current, claims)
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
        _require_validation_binding(
            runner,
            validation_report,
            subject_ref=(
                f"Work/runs/{state['run_id']}/validation/"
                f"report-chief-candidate-r{review_round}.md"
            ),
            subject_revision=review_round,
        )
        metadata = final_audit_metadata_view(current)
        audit_bodies = _final_audit_section_bodies(current)
        recheck_targets = (
            {
                section_id
                for finding in pending.values()
                for section_id in finding.target_section_ids
            }
            if phase == "recheck"
            else set()
        )
        review_input = FinalReviewInput(
            phase=phase,
            run_id=state["run_id"],
            subject_ref=subject_ref,
            subject_revision=review_round,
            subject_metadata=metadata if phase == "initial" else None,
            subject_metadata_sha256=(
                _model_sha256(metadata) if phase == "recheck" else None
            ),
            canonical_markdown=(
                _final_audit_markdown(strip_runtime_claim_markers(canonical))
                if phase == "initial"
                else None
            ),
            changed_section_bodies=(
                {
                    section_id: audit_bodies[section_id]
                    for section_id in sorted(recheck_targets)
                }
                if phase == "recheck"
                else {}
            ),
            unchanged_section_sha256=(
                {
                    section_id: _text_sha256(body)
                    for section_id, body in audit_bodies.items()
                    if section_id not in recheck_targets
                }
                if phase == "recheck"
                else {}
            ),
            required_section_ids=list(required_audit_sections),
            required_findings=list(pending.values()) if phase == "recheck" else [],
            revision_responses=responses if phase == "recheck" else [],
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
                f"独立审查当前成稿{audited_chapters}的保真、综合、可追溯、可执行和交付质量。"
                if phase == "initial"
                else (
                    "由原 final reviewer 逐项判断 required_findings 是否关闭并检查"
                    f"{audited_chapters}回归。"
                )
            ),
            input_refs=[input_ref],
            constraints=[
                f"只审查{audited_chapters}当前实际存在的小节及最终交付质量，不重做模块或 Cross 专业审查",
                "第二章由模块审查和运行时保真校验负责，不属于本阶段内容、覆盖范围或 finding target",
                "residual_risks 只记录无需内容修订的透明限制",
                "当前实际章节正文、必要表格或图片缺失属于 actionable finding，禁止塞入 residual_risks",
                (
                    f"首轮 checked_section_ids 必须精确覆盖{audited_chapters}的实际审计小节"
                    if phase == "initial"
                    else "verdicts 必须逐项且仅覆盖 required_findings；new_findings 只允许真实回归"
                ),
                *runner._user_supplement_constraints(
                    state,
                    stage="final_review",
                    target_ids={
                        *required_audit_sections,
                        *(claim.id for claim in claims),
                    },
                ),
            ],
            allowed_outputs=[output_kind],
            revision=review_round,
            prior_result_ref=finding_refs[-1] if finding_refs else None,
            artifact_delivery_modes={
                input_ref: "inline",
                **(
                    {finding_refs[-1]: "hash_retained"}
                    if finding_refs
                    else {}
                ),
            },
            input_contract_kind="final_review_input",
            input_contract_ref=input_ref,
            inline_context=runner._role_skill_context(state, "final-auditor"),
            allowed_tools=["submit_result"],
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
            if set(result.checked_section_ids) != set(required_audit_sections):
                raise ReviewLifecycleError(
                    "initial final review did not cover every chief-owned audit section"
                )
            _validate_final_findings(result.findings, set(required_audit_sections))
            finding_ref = _write_immutable_model(
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
            if set(result.checked_section_ids) != set(required_audit_sections):
                raise ReviewLifecycleError(
                    "final recheck did not cover every active chief-owned audit section"
                )
            _validate_verdicts(result.verdicts, set(pending))
            _validate_final_findings(result.new_findings, set(required_audit_sections))
            verdict_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-verdicts-r{review_round}.json",
                result,
            )
            verdict_refs.append(verdict_ref)
            escalated = [verdict for verdict in result.verdicts if verdict.verdict == "escalate"]
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
                or (verdict.verdict == "escalate" and verdict.finding_id not in main_accepts)
            }
            resolved_ids.update(
                verdict.finding_id
                for verdict in result.verdicts
                if verdict.verdict == "resolved" or verdict.finding_id in main_accepts
            )
            for finding in result.new_findings:
                if finding.id in pending or finding.id in resolved_ids:
                    raise ReviewLifecycleError(
                        f"new final finding reuses an existing id: {finding.id}"
                    )
                next_pending[finding.id] = finding
            if result.new_findings:
                new_ref = _write_immutable_model(
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
            completion_refs = [subject_ref, *finding_refs, *verdict_refs]
            completion = ReviewCompletionRecord(
                lifecycle="final",
                run_id=state["run_id"],
                reviewer_agent_id="chief-editor-auditor",
                reviewer_session_key=reviewer_session_key,
                subject_refs=[subject_ref],
                finding_refs=finding_refs,
                verdict_refs=verdict_refs,
                resolved_finding_ids=sorted(resolved_ids),
                artifact_sha256=_artifact_sha256(runner, completion_refs),
            )
            completion_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-completion.json",
                completion,
            )
            canonical_ref = validation_report.subject_ref
            snapshot_ref = _write_immutable_model(
                runner,
                f"Work/runs/{state['run_id']}/reviews/final-audit-snapshot.json",
                FinalAuditSnapshot(
                    run_id=state["run_id"],
                    subject_ref=subject_ref,
                    subject_revision=review_round,
                    subject_sha256=hashlib.sha256(
                        (runner.service.workspace / subject_ref).read_bytes()
                    ).hexdigest(),
                    canonical_markdown_ref=canonical_ref,
                    canonical_markdown_sha256=hashlib.sha256(
                        (runner.service.workspace / canonical_ref).read_bytes()
                    ).hexdigest(),
                    validation_report_ref=integrity_ref,
                    validation_report_sha256=hashlib.sha256(
                        (runner.service.workspace / integrity_ref).read_bytes()
                    ).hexdigest(),
                    completion_ref=completion_ref,
                    completion_sha256=hashlib.sha256(
                        (runner.service.workspace / completion_ref).read_bytes()
                    ).hexdigest(),
                ),
            )
            state["edited_report"] = current
            state["final_review_completion_ref"] = completion_ref
            state["final_audit_snapshot_ref"] = snapshot_ref
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
                response for response in responses if response.action in {"disputed", "needs_input"}
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
