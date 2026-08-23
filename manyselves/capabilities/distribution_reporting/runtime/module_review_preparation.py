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
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleInitialReviewPreparation,
    ModuleReviewPreflightProgress,
)
from manyselves.capabilities.distribution_reporting.runtime.review_preflight import (
    evaluate_module_review_preflight,
)
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import (
    SourceLedger,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


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


def _supplement_constraints(
    state: Mapping[str, Any],
    *,
    module_id: str,
    scope: set[str],
) -> list[str]:
    request = state.get("request")
    supplements = getattr(request, "user_supplements", [])
    superseded = {
        item_id
        for supplement in supplements
        for item_id in supplement.supersedes
    }
    target_ids = {module_id, *scope}
    return [
        (
            f"用户补充 {item.id}（scope={item.scope}; "
            f"targets={','.join(item.target_ids) or 'run'}）是当前 run 的显式输入："
            f"{item.content}"
        )
        for item in supplements
        if item.id not in superseded
        and "module_review" in item.stages
        and (
            item.scope == "run"
            or bool(set(item.target_ids).intersection(target_ids))
        )
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
        path = workspace / "Work/report-template-role-skills" / skill_id / "SKILL.md"
        if path.is_file():
            content = path.read_text(encoding="utf-8")
    if not content:
        return ""
    return (
        f'<template_role_skill id="{skill_id}" delivery_mode="inline">\n'
        f"{content}\n"
        "</template_role_skill>"
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
    preflight_progress = ModuleReviewPreflightProgress(current=module)
    if not preflight.report.passed:
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
            preflight_progress=preflight_progress.model_copy(
                update={
                    "attempts": 1,
                    "failure_signatures": [
                        tuple(
                            sorted(
                                (
                                    failure.check_id,
                                    failure.target_path,
                                    failure.message,
                                )
                                for failure in preflight.report.failures
                            )
                        )
                    ],
                }
            ),
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
                *_supplement_constraints(
                    state,
                    module_id=module_id,
                    scope=scope,
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
]
