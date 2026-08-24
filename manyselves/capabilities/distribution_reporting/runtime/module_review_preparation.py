"""Capability-owned preparation of one module's initial review turn.

The preparation boundary contains the deterministic subject, validation,
evidence, Knowledge, and TaskEnvelope projections consumed by the generic
Agent action.  It does not invoke an Agent, run a correction loop, or import a
Reporting Runner.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
    compose_module_markdown,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    ClaimRecord,
    ModuleSubmission,
    TaskEnvelope,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleReviewInput,
    ReviewClaimStatement,
    ReviewEvidenceExcerpt,
    ValidationFailure,
    ValidationReport,
    module_content_view,
)
from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
    DeclarativeModuleReviewPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    UserSupplement,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewPreparation,
    ModuleLocalRegressionContext,
    ModuleReviewPreflightProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.module_preflight_revision import (
    advance_module_review_preflight_progress,
)
from manyselves.capabilities.distribution_reporting.runtime.review_preflight import (
    evaluate_module_review_preflight,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import (
    SourceLedger,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
from manyselves.capabilities.distribution_reporting.runtime.template_skill_paths import (
    template_skill_ref,
)
from manyselves.capabilities.distribution_reporting.runtime.user_supplements import (
    request_user_supplements,
    user_supplement_constraints,
)


def _structure_report(
    store: ReportingStore,
    *,
    run_id: str,
    subject: ModuleSubmission,
    subject_ref: str,
    phase: str,
) -> tuple[str, ValidationReport]:
    validation_ref = (
        f"Work/runs/{run_id}/reviews/module-quality-"
        f"{subject.module_id}-r{subject.revision}-{phase}.json"
    )
    failures: list[ValidationFailure] = []
    canonical = compose_module_markdown(
        subject.module_id,
        subject.submodule_narratives,
    )
    if subject.markdown.strip() != canonical.strip():
        failures.append(
            ValidationFailure(
                check_id="module.canonical_markdown",
                target_path="submodule_narratives",
                message="rendered module Markdown differs from canonical narratives",
            )
        )
    report = ValidationReport(
        validation_protocol_version=2,
        run_id=run_id,
        subject_ref=subject_ref,
        subject_revision=subject.revision,
        validator="module-structure/v2",
        check_ids=["module.canonical_markdown"],
        failures=failures,
        passed=not failures,
    )
    store.write_json(validation_ref, report.model_dump(mode="json"))
    return validation_ref, report


def _review_knowledge(
    workspace: Path,
    state: Mapping[str, Any],
    module_id: str,
    scope: set[str],
) -> tuple[str | None, str]:
    refs = state.get("module_knowledge_refs", {})
    knowledge_ref = refs.get(module_id) if isinstance(refs, Mapping) else None
    if not knowledge_ref:
        return None, ""
    text = (workspace / str(knowledge_ref)).read_text(encoding="utf-8")
    selected: list[str] = []
    current_selected = True
    for line in text.splitlines():
        match = re.match(r"^##\s+(2\.[1-5](?:\.\d+)+)\b", line)
        if match:
            current_selected = match.group(1) in scope
        if current_selected:
            selected.append(line)
    return str(knowledge_ref), "\n".join(selected).strip()


def _review_evidence(
    workspace: Path,
    subject: ModuleSubmission,
    run_id: str,
    scope: set[str],
) -> list[ReviewEvidenceExcerpt]:
    ledger = SourceLedger(workspace, run_id)
    records = {record.id: record for record in ledger.records}
    selected_ids = sorted(
        {
            source_id
            for claim in subject.claims
            if claim.submodule_id in scope
            for source_id in claim.source_ids
            if source_id.startswith("E-")
        }
    )
    packet: list[ReviewEvidenceExcerpt] = []
    for evidence_id in selected_ids:
        record = records.get(evidence_id)
        content_ref = ledger.content_ref(evidence_id)
        if record is None or content_ref is None:
            raise ValueError(
                f"review subject cites unreadable current-run evidence: {evidence_id}"
            )
        packet.append(
            ReviewEvidenceExcerpt(
                evidence_id=evidence_id,
                title=record.title,
                locator=record.locator,
                content=(workspace / content_ref).read_text(encoding="utf-8"),
            )
        )
    return packet


def _claim_statements(
    claims: list[ClaimRecord],
    scope: set[str],
) -> list[ReviewClaimStatement]:
    return [
        ReviewClaimStatement(
            statement_ref=claim.id,
            submodule_id=claim.submodule_id,
            text=claim.text,
            statement_type=claim.claim_type,
            evidence_ids=claim.source_ids,
            confidence=claim.confidence,
            unresolved=claim.unresolved,
        )
        for claim in claims
        if claim.submodule_id in scope
    ]


def _template_skill_context(
    workspace: Path,
    state: Mapping[str, Any],
    module_id: str,
) -> str:
    skill_id = f"auditor-{module_id}"
    texts = state.get("template_skill_text", {})
    content = texts.get(skill_id, "") if isinstance(texts, Mapping) else ""
    if not content:
        path = workspace / template_skill_ref(skill_id)
        if path.is_file():
            content = path.read_text(encoding="utf-8")
    if not content:
        return ""
    return (
        f'<template_role_skill id="{skill_id}" delivery_mode="inline">\n'
        f"{content}\n"
        "</template_role_skill>"
    )


def prepare_module_local_regression_review(
    *,
    store: ReportingStore,
    workflow_id: str,
    run_id: str,
    current: ModuleSubmission,
    scope: set[str],
    review_round: int,
    regression_context: ModuleLocalRegressionContext,
    user_supplements: list[UserSupplement],
    previous_preflight_progress: ModuleReviewPreflightProgress | None = None,
) -> ModuleInitialReviewPreparation:
    """Prepare the original module Auditor's Cross-triggered local review."""

    module_id = current.module_id
    lifecycle_id = f"cross-r{review_round}"
    review_root = f"Work/runs/{run_id}/reviews/module/{lifecycle_id}/{module_id}"
    progress_ref = f"{review_root}/progress.json"
    reviewer_session_key = regression_context.prior_review_completion.reviewer_session_key
    subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r{current.revision}.json"
    if not (store.workspace / subject_ref).is_file():
        store.write_json(subject_ref, current.model_dump(mode="json"))

    _validation_ref, structure_report = _structure_report(
        store,
        run_id=run_id,
        subject=current,
        subject_ref=subject_ref,
        phase="review-r0",
    )
    preflight = evaluate_module_review_preflight(
        store.workspace,
        run_id=run_id,
        subject=current,
        subject_ref=subject_ref,
        upstream_report=structure_report,
    )
    preflight_ref = (
        f"{review_root}/preflight-subject-r{current.revision}-review-r0.json"
    )
    store.write_json(preflight_ref, preflight.report.model_dump(mode="json"))
    preflight_progress = (
        ModuleReviewPreflightProgress(current=current)
        if previous_preflight_progress is None
        else previous_preflight_progress.model_copy(update={"current": current})
    )
    if not preflight.report.passed:
        preflight_progress = advance_module_review_preflight_progress(
            current=current,
            report=preflight.report,
            previous=previous_preflight_progress,
            validation_ref=preflight_ref,
        )
        return ModuleInitialReviewPreparation(
            mode="preflight_revision",
            run_id=run_id,
            module_id=module_id,
            lifecycle_id=lifecycle_id,
            workflow_id=workflow_id,
            reviewer_session_key=reviewer_session_key,
            review_root=review_root,
            progress_ref=progress_ref,
            review_round=0,
            phase="local_regression",
            scope=sorted(preflight.target_submodule_ids),
            current=current,
            subject_ref=subject_ref,
            validation_ref=preflight_ref,
            validation_target_submodule_ids=sorted(preflight.target_submodule_ids),
            preflight_progress=preflight_progress,
            regression_context=regression_context,
        )

    state: dict[str, Any] = {}
    knowledge_path = (
        store.workspace
        / f"Work/runs/{run_id}/context/module-{module_id}-knowledge.md"
    )
    if knowledge_path.is_file():
        state["module_knowledge_refs"] = {
            module_id: str(knowledge_path.relative_to(store.workspace))
        }
    knowledge_ref, _knowledge_context = _review_knowledge(
        store.workspace,
        state,
        module_id,
        scope,
    )
    review_input = ModuleReviewInput(
        phase="local_regression",
        run_id=run_id,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        review_round=0,
        subject_ref=subject_ref,
        subject_revision=current.revision,
        subject=module_content_view(current, scope),
        claim_statements=_claim_statements(current.claims, scope),
        prior_claim_statements=[],
        unchanged_submodule_sha256={},
        unchanged_statement_sha256={},
        knowledge_ref=knowledge_ref,
        knowledge_context="",
        evidence=_review_evidence(store.workspace, current, run_id, scope),
        required_submodule_ids=sorted(scope),
        required_findings=[],
        revision_responses=[],
        prior_review_completion_ref=regression_context.prior_review_completion_ref,
        prior_review_completion=regression_context.prior_review_completion,
        baseline_subject_ref=regression_context.baseline_subject_ref,
        trigger_cross_findings=regression_context.trigger_cross_findings,
        trigger_revision_responses=regression_context.trigger_revision_responses,
        revision_diff_ref=regression_context.revision_diff_ref,
        revision_diff=regression_context.revision_diff,
        validation_report_ref=preflight_ref,
        validation_report=preflight.report,
    )
    input_ref = f"{review_root}/input-r0.json"
    store.write_json(input_ref, review_input.model_dump(mode="json"))
    envelope = TaskEnvelope(
        task_id=f"module-{module_id}-{lifecycle_id}-review-r0",
        run_id=run_id,
        agent_id="evidence-auditor",
        objective=(
            f"由模块 {module_id} 的原审查者仅检查 Cross 回改范围、diff、Claim 与证据回归。"
        ),
        input_refs=[input_ref],
        constraints=[
            "coverage 记录实际检查范围，不是批准状态",
            "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
            "不得拆成独立小节级审查任务或会话",
            "finding 首次提出后不可改写；复审不得复述旧 finding",
            "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
            "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
            "首轮必须覆盖 input 中全部 required_submodule_ids",
            "这是原模块审查者的 local_regression，不得重新审查未修改小节",
            "只根据 prior completion、Cross finding/作者响应、revision diff、目标 Claim/证据和当前 validation 提出真实回归 finding",
            *user_supplement_constraints(
                user_supplements,
                stage="module_review",
                target_ids={
                    module_id,
                    *scope,
                    *(claim.id for claim in current.claims),
                },
            ),
        ],
        allowed_outputs=["module_review_finding_submission"],
        revision=0,
        artifact_delivery_modes={input_ref: "inline"},
        target_submodule_ids=sorted(scope),
        input_contract_kind="module_review_input",
        input_contract_ref=input_ref,
        inline_context=_template_skill_context(
            store.workspace,
            state,
            module_id,
        ),
        allowed_tools=["submit_result"],
    )
    return ModuleInitialReviewPreparation(
        mode="invoke_agent",
        run_id=run_id,
        module_id=module_id,
        lifecycle_id=lifecycle_id,
        workflow_id=workflow_id,
        reviewer_session_key=reviewer_session_key,
        review_root=review_root,
        progress_ref=progress_ref,
        review_round=0,
        phase="local_regression",
        scope=sorted(scope),
        current=current,
        subject_ref=subject_ref,
        review_input_ref=input_ref,
        review_input=review_input,
        envelope=envelope,
        validation_ref=preflight_ref,
        validation_target_submodule_ids=[],
        preflight_progress=preflight_progress,
        regression_context=regression_context,
    )


def prepare_current_module_review(
    value: DeclarativeModuleRuntimeLaneContext,
    *,
    store: ReportingStore,
) -> DeclarativeModuleRuntimeLaneContext:
    """Prepare the exact initial module Auditor boundary from typed state."""

    context = (
        value
        if isinstance(value, DeclarativeModuleRuntimeLaneContext)
        else DeclarativeModuleRuntimeLaneContext.model_validate(value)
    )
    if context.status != "authored" or context.module is None:
        return context

    state = context.reporting_state
    run_id = str(state["run_id"])
    module = ModuleSubmission.model_validate(context.module)
    module_id = context.module_id
    scope = set(REPORT_TAXONOMY[module_id].submodules)
    review_root = f"Work/runs/{run_id}/reviews/module/initial/{module_id}"
    progress_ref = f"{review_root}/progress.json"
    subject_ref = f"Work/runs/{run_id}/modules/{module_id}-r{module.revision}.json"
    subject_path = store.workspace / subject_ref
    if not subject_path.is_file():
        store.write_json(subject_ref, module.model_dump(mode="json"))

    validation_ref, structure_report = _structure_report(
        store,
        run_id=run_id,
        subject=module,
        subject_ref=subject_ref,
        phase="review-r0",
    )
    preflight = evaluate_module_review_preflight(
        store.workspace,
        run_id=run_id,
        subject=module,
        subject_ref=subject_ref,
        upstream_report=structure_report,
    )
    preflight_ref = (
        f"{review_root}/preflight-subject-r{module.revision}-review-r0.json"
    )
    store.write_json(preflight_ref, preflight.report.model_dump(mode="json"))
    reviewer_session_key = f"module-auditor-{module_id}"
    previous_preflight_progress = (
        context.review.prepared.preflight_progress
        if context.review is not None
        else None
    )
    preflight_progress = (
        ModuleReviewPreflightProgress(current=module)
        if previous_preflight_progress is None
        else previous_preflight_progress.model_copy(update={"current": module})
    )
    if not preflight.report.passed:
        preflight_progress = advance_module_review_preflight_progress(
            current=module,
            report=preflight.report,
            previous=previous_preflight_progress,
            validation_ref=preflight_ref,
        )
        prepared = ModuleInitialReviewPreparation(
            mode="preflight_revision",
            run_id=run_id,
            module_id=module_id,
            lifecycle_id="initial",
            workflow_id=context.workflow_id,
            reviewer_session_key=reviewer_session_key,
            review_root=review_root,
            progress_ref=progress_ref,
            review_round=0,
            phase="initial",
            scope=sorted(preflight.target_submodule_ids),
            current=module,
            subject_ref=subject_ref,
            validation_ref=preflight_ref,
            validation_target_submodule_ids=sorted(preflight.target_submodule_ids),
            preflight_progress=preflight_progress,
        )
    else:
        knowledge_ref, knowledge_context = _review_knowledge(
            store.workspace,
            state,
            module_id,
            scope,
        )
        review_input = ModuleReviewInput(
            phase="initial",
            run_id=run_id,
            module_id=module_id,
            lifecycle_id="initial",
            review_round=0,
            subject_ref=subject_ref,
            subject_revision=module.revision,
            subject=module_content_view(module, scope),
            claim_statements=_claim_statements(module.claims, scope),
            knowledge_ref=knowledge_ref,
            knowledge_context=knowledge_context,
            evidence=_review_evidence(store.workspace, module, run_id, scope),
            required_submodule_ids=sorted(scope),
            validation_report_ref=preflight_ref,
            validation_report=preflight.report,
        )
        input_ref = f"{review_root}/input-r0.json"
        store.write_json(input_ref, review_input.model_dump(mode="json"))
        envelope = TaskEnvelope(
            task_id=f"module-{module_id}-initial-review-r0",
            run_id=run_id,
            agent_id="evidence-auditor",
            objective=f"审查模块 {module_id} 的当前正文、Claim 与证据边界。",
            input_refs=[input_ref],
            constraints=[
                "coverage 记录实际检查范围，不是批准状态",
                "一次返回整个模块检查范围的 findings/verdicts；小节 id 只用于定位问题，"
                "不得拆成独立小节级审查任务或会话",
                "finding 首次提出后不可改写；复审不得复述旧 finding",
                "finding id 由运行时按 lifecycle 和 review round 分配，审查员不得提交或猜测 id",
                "advisory 与 blocking 都必须获得作者响应和 reviewer verdict",
                "首轮必须覆盖 input 中全部 required_submodule_ids",
                *user_supplement_constraints(
                    request_user_supplements(state.get("request")),
                    stage="module_review",
                    target_ids={module_id, *scope},
                ),
            ],
            allowed_outputs=["module_review_finding_submission"],
            revision=0,
            artifact_delivery_modes={input_ref: "inline"},
            target_submodule_ids=sorted(scope),
            input_contract_kind="module_review_input",
            input_contract_ref=input_ref,
            inline_context=_template_skill_context(
                store.workspace,
                state,
                module_id,
            ),
            allowed_tools=["submit_result"],
        )
        prepared = ModuleInitialReviewPreparation(
            mode="invoke_agent",
            run_id=run_id,
            module_id=module_id,
            lifecycle_id="initial",
            workflow_id=context.workflow_id,
            reviewer_session_key=reviewer_session_key,
            review_root=review_root,
            progress_ref=progress_ref,
            review_round=0,
            phase="initial",
            scope=sorted(scope),
            current=module,
            subject_ref=subject_ref,
            review_input_ref=input_ref,
            review_input=review_input,
            envelope=envelope,
            validation_ref=preflight_ref,
            validation_target_submodule_ids=[],
            preflight_progress=preflight_progress,
        )

    review = DeclarativeModuleReviewPreparation(
        envelope=prepared.envelope,
        reviewer_session_key=reviewer_session_key,
        prepared=prepared,
    )
    return context.model_copy(
        deep=True,
        update={
            "status": (
                "preflight_revision_pending"
                if prepared.mode == "preflight_revision"
                else "review_ready"
            ),
            "module": prepared.current,
            "review": review,
        },
    )


__all__ = [
    "prepare_current_module_review",
    "prepare_module_local_regression_review",
]
