"""Typed, model-visible stage inputs for the reporting review lifecycle."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from .agentic_models import (
    ClaimRecord,
    ChapterScopedFinalReviewFinding,
    CrossDecisionPack,
    ChiefChapterLaneRevisionSubmission,
    FINAL_AUDIT_SECTION_IDS,
    CrossReviewFinding,
    CrossSynthesisInput,
    EditedReportSubmission,
    EditedReportSubmissionInput,
    FinalReviewFinding,
    ModuleReviewFinding,
    ModuleSubmission,
    ResolutionVerdict,
    RevisionResponse,
    StrictModel,
    TEMPLATE_ROLE_SKILL_IDS,
    TEMPLATE_SKILL_EXCLUSION_CATEGORIES,
    TEMPLATE_SKILL_TRANSFER_CATEGORIES,
    TemplateRoleSkillId,
    TableSubmissionInput,
)
from .models import (
    CHAPTER1_SECTION_IDS,
    CHAPTER3_SECTION_IDS,
    CHAPTER_IDS,
    SpecialTopicPlan,
    chapter_section_ids,
)
from .submission_contracts import FIELD_GUIDANCE
from .taxonomy import REPORT_TAXONOMY, resolve_submodule


class ModuleContentView(StrictModel):
    """Model-visible module content without runtime-only identifiers."""

    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    revision: int = Field(ge=0)
    submodule_narratives: dict[str, str]
    evidence_ids_by_submodule: dict[str, list[str]] = Field(
        description="Registered E-* evidence ids already bound to each visible submodule."
    )
    unresolved_questions: list[str] = Field(default_factory=list)


class CrossDecisionPackView(StrictModel):
    """Model-visible, lossless Cross decision boundary.

    The runtime uses current-run typed references and business identity.  Hash
    metadata is neither exposed here nor consulted by completion/recovery.
    """

    version: int = Field(default=1, ge=1)
    run_id: str = Field(min_length=1)
    module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(
        min_length=5,
        max_length=5,
    )
    cross_review_completion_ref: str = Field(min_length=1)
    synthesis_inputs: list[CrossSynthesisInput] = Field(default_factory=list)
    artifact_refs: list[str] = Field(
        default_factory=list,
        description="Current-run Cross completion refs retained for business identity.",
    )

    @model_validator(mode="after")
    def view_matches_closed_pack(self) -> "CrossDecisionPackView":
        expected_refs = {self.cross_review_completion_ref}
        if not self.artifact_refs:
            self.artifact_refs = sorted(expected_refs)
        if sorted(set(self.artifact_refs)) != sorted(expected_refs):
            raise ValueError(
                "Cross decision view artifact_refs must exactly cover Cross completion"
            )
        # Reuse the complete pack's business checks without synthesizing hash fields.
        payload = self.model_dump(mode="python")
        payload.pop("artifact_refs", None)
        CrossDecisionPack.model_validate(payload)
        return self


class ReviewEvidenceExcerpt(StrictModel):
    """One bounded, current-run E-* source made directly visible to a reviewer."""

    evidence_id: str = Field(
        pattern=r"^E-",
        description="Registered current-run project evidence id.",
    )
    title: str = Field(min_length=1, description="Source title from the current-run ledger.")
    locator: str = Field(
        min_length=1,
        description="Reproducible source locator from the current-run ledger.",
    )
    content: str = Field(
        min_length=1,
        description=(
            "Complete normalized current-run evidence content needed to audit the cited "
            "prose. Runtime cost optimization must not truncate this authoritative input."
        ),
    )


class ReviewClaimStatement(StrictModel):
    """Review-safe Claim semantics without the runtime authoring protocol."""

    statement_ref: str = Field(
        min_length=1,
        description="Opaque stable reference for one current subject statement.",
    )
    submodule_id: str = Field(
        min_length=1,
        description="Reviewed submodule containing the statement.",
    )
    text: str = Field(
        min_length=1,
        description="Exact statement whose support and boundary are under review.",
    )
    statement_type: Literal[
        "project_fact",
        "technical_interpretation",
        "risk_judgment",
        "recommendation",
    ] = Field(description="Semantic purpose of the reviewed statement.")
    evidence_ids: list[str] = Field(
        default_factory=list,
        description="Registered sources currently supporting the statement.",
    )
    confidence: float = Field(
        ge=0.0,
        le=1.0,
        description="Current author confidence in the statement.",
    )
    unresolved: bool = Field(
        description="Whether the statement still carries an explicit unresolved boundary.",
    )


class ModuleRevisionDiff(StrictModel):
    """Bounded deterministic diff between the prior and current module subjects."""

    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Only module changed by this bounded diff."
    )
    from_revision: int = Field(
        ge=0,
        description="Previously approved module revision.",
    )
    to_revision: int = Field(
        ge=1,
        description="Current module revision under local regression review.",
    )
    changed_submodule_narratives: list[str] = Field(
        default_factory=list,
        description="Module-local prose sections changed between the two revisions.",
    )
    changed_statement_refs: list[str] = Field(
        default_factory=list,
        description="Opaque reviewed statements added, removed, or changed.",
    )
    evidence_ids_added: list[str] = Field(
        default_factory=list,
        description="Registered evidence newly bound by the revision.",
    )
    evidence_ids_removed: list[str] = Field(
        default_factory=list,
        description="Registered evidence no longer bound by the revision.",
    )

    @model_validator(mode="after")
    def revision_advances(self) -> "ModuleRevisionDiff":
        if self.to_revision <= self.from_revision:
            raise ValueError("module revision diff must advance the subject revision")
        return self


def strip_runtime_claim_markers(text: str) -> str:
    return re.sub(r"\s*\[\[CLAIM:C-[^\]\s]+\]\]", "", text)


def module_content_view(
    subject: ModuleSubmission,
    submodule_ids: set[str] | None = None,
) -> ModuleContentView:
    selected = (
        set(subject.submodule_narratives)
        if submodule_ids is None
        else set(submodule_ids)
    )
    evidence_ids_by_submodule = {
        submodule_id: sorted(
            {
                source_id
                for claim in subject.claims
                if claim.submodule_id == submodule_id
                for source_id in claim.source_ids
                if source_id.startswith("E-")
            }
        )
        for submodule_id in subject.submodule_narratives
        if submodule_id in selected
    }
    narratives = {
        submodule_id: strip_runtime_claim_markers(narrative).rstrip()
        for submodule_id, narrative in subject.submodule_narratives.items()
        if submodule_id in selected
    }
    return ModuleContentView(
        module_id=subject.module_id,
        revision=subject.revision,
        submodule_narratives=narratives,
        evidence_ids_by_submodule=evidence_ids_by_submodule,
        unresolved_questions=subject.unresolved_questions,
    )


def edited_report_content_view(
    subject: EditedReportSubmission,
) -> EditedReportSubmissionInput:
    payload = subject.model_dump(mode="python")
    payload.pop("protected_claim_ids", None)
    payload.pop("special_topic_plan", None)
    payload.pop("synthesis_dispositions", None)
    payload.pop("synthesis_tables", None)
    payload["module_narratives"] = {
        module_id: strip_runtime_claim_markers(narrative).rstrip()
        for module_id, narrative in subject.module_narratives.items()
    }
    payload["tables"] = [
        {
            "title": table.title,
            "headers": table.headers,
            "rows": table.rows,
            "evidence_ids": [
                source_id for source_id in table.source_ids if source_id.startswith("E-")
            ],
        }
        for table in subject.tables
    ]
    return EditedReportSubmissionInput.model_validate(payload)


class FinalAuditSubjectView(StrictModel):
    """Chief-owned Chapters 1 and 3 plus optional Chapter 4, without Chapter 2 prose."""

    title: str = Field(min_length=1)
    assessment_background: str = Field(min_length=1)
    findings_overview: str = Field(min_length=1)
    regional_executive_summary: str = Field(min_length=1)
    risk_panorama: str = Field(min_length=1)
    dimension_risk_analysis: str = Field(min_length=1)
    data_gap_analysis: str = Field(min_length=1)
    improvement_action_plan: str = Field(min_length=1)
    special_topic_plan: SpecialTopicPlan | None = None
    special_topic_analysis: str | None = Field(default=None, min_length=1)
    tables: list[TableSubmissionInput] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    unresolved_editorial_issues: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def optional_special_topic_fields_match(self) -> "FinalAuditSubjectView":
        if (self.special_topic_plan is None) != (self.special_topic_analysis is None):
            raise ValueError(
                "special_topic_plan and special_topic_analysis must either both be present "
                "or both be absent"
            )
        if self.special_topic_plan is not None and self.special_topic_analysis is not None:
            self.special_topic_plan.validate_analysis(self.special_topic_analysis)
        return self


def final_audit_content_view(
    subject: EditedReportSubmission,
) -> FinalAuditSubjectView:
    payload = subject.model_dump(mode="python")
    for field in (
        "kind",
        "module_narratives",
        "protected_claim_ids",
        "synthesis_dispositions",
        "synthesis_tables",
        "revision_responses",
    ):
        payload.pop(field, None)
    payload["tables"] = edited_report_content_view(subject).tables
    return FinalAuditSubjectView.model_validate(payload)


class FinalAuditMetadataView(StrictModel):
    """Non-prose final-audit metadata not represented in canonical Markdown."""

    photo_ids: list[str] = Field(default_factory=list)
    unresolved_editorial_issues: list[str] = Field(default_factory=list)


def final_audit_metadata_view(
    subject: EditedReportSubmission,
) -> FinalAuditMetadataView:
    content = final_audit_content_view(subject)
    return FinalAuditMetadataView(
        photo_ids=content.photo_ids,
        unresolved_editorial_issues=content.unresolved_editorial_issues,
    )


def _validate_chapter_lane_scope(
    chapter_id: str,
    section_ids: list[str],
    special_topic_plan: SpecialTopicPlan | None,
) -> None:
    """Validate one lane's complete active scope without copying the report."""

    if chapter_id not in CHAPTER_IDS:
        raise ValueError(f"unsupported chapter lane: {chapter_id}")
    if len(section_ids) != len(set(section_ids)) or not section_ids:
        raise ValueError("chapter lane section_ids must be non-empty and unique")
    if chapter_id == "1":
        expected = set(CHAPTER1_SECTION_IDS)
        if set(section_ids) != expected:
            raise ValueError(f"Chapter 1 lane must cover exactly {sorted(expected)}")
    elif chapter_id == "3":
        expected = set(CHAPTER3_SECTION_IDS)
        if set(section_ids) != expected:
            raise ValueError(f"Chapter 3 lane must cover exactly {sorted(expected)}")
    else:
        if special_topic_plan is None:
            raise ValueError("Chapter 4 lane requires special_topic_plan")
        expected = set(chapter_section_ids("4", special_topic_plan))
        if set(section_ids) != expected:
            raise ValueError(
                "Chapter 4 lane section_ids must exactly match special_topic_plan"
            )


class ChiefChapterLaneInput(StrictModel):
    """Initial/revision Chief task carrying only one chapter's prose scope."""

    kind: Literal["chief_chapter_lane_input"] = "chief_chapter_lane_input"
    phase: Literal["initial", "revision"] = "initial"
    run_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    chapter_id: Literal["1", "3", "4"]
    section_ids: list[str] = Field(min_length=1)
    section_bodies: dict[str, str] = Field(default_factory=dict)
    source_context: dict[str, str] = Field(
        default_factory=dict,
        description="Lane-local source projections; never the complete edited report.",
    )
    source_refs: list[str] = Field(default_factory=list)
    assigned_findings: list[ChapterScopedFinalReviewFinding] = Field(default_factory=list)
    special_topic_plan: SpecialTopicPlan | None = None
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def lane_is_complete_and_local(self) -> "ChiefChapterLaneInput":
        _validate_chapter_lane_scope(self.chapter_id, self.section_ids, self.special_topic_plan)
        if self.phase == "revision" and set(self.section_bodies) != set(self.section_ids):
            raise ValueError("Chief revision lane section_bodies must exactly match section_ids")
        if any(not body.strip() for body in self.section_bodies.values()):
            raise ValueError("Chief lane section_bodies must not be blank")
        for finding in self.assigned_findings:
            if not set(finding.target_section_ids).issubset(self.section_ids):
                raise ValueError("Chief lane findings must stay in lane section_ids")
        if self.phase == "initial" and (self.assigned_findings or not (self.source_context or self.source_refs)):
            raise ValueError("initial Chief lane requires lane-local source context and no findings")
        if self.phase == "revision" and not self.assigned_findings:
            raise ValueError("revision Chief lane requires assigned findings")
        return self


class FinalChapterLaneInput(StrictModel):
    """Initial/recheck Final task carrying only one chapter's prose scope."""

    kind: Literal["final_chapter_lane_input"] = "final_chapter_lane_input"
    phase: Literal["initial", "recheck"] = "initial"
    run_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    chapter_id: Literal["1", "3", "4"]
    review_focus: list[str] = Field(
        default_factory=list,
        description="Chapter-specific semantic review questions for this Final lane.",
    )
    section_ids: list[str] = Field(min_length=1)
    section_bodies: dict[str, str] = Field(min_length=1)
    unchanged_section_sha256: dict[str, str] = Field(
        default_factory=dict,
        description="Recheck-only hashes for unchanged sections retained in identity history.",
    )
    required_findings: list[ChapterScopedFinalReviewFinding] = Field(default_factory=list)
    revision_responses: list[RevisionResponse] = Field(default_factory=list)
    special_topic_plan: SpecialTopicPlan | None = None
    revision: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def lane_is_complete_and_local(self) -> "FinalChapterLaneInput":
        _validate_chapter_lane_scope(self.chapter_id, self.section_ids, self.special_topic_plan)
        if not self.review_focus:
            raise ValueError("parallel final review lane requires a chapter review focus")
        body_ids = set(self.section_bodies)
        hash_ids = set(self.unchanged_section_sha256)
        if body_ids.intersection(hash_ids):
            raise ValueError("Final lane bodies and unchanged hashes must not overlap")
        if self.phase == "initial" and (body_ids != set(self.section_ids) or hash_ids):
            raise ValueError("initial Final lane requires every section body and no hashes")
        if self.phase == "recheck" and body_ids.union(hash_ids) != set(self.section_ids):
            raise ValueError("Final recheck bodies and hashes must cover section_ids exactly")
        if self.phase == "recheck" and not body_ids:
            raise ValueError("Final recheck requires at least one changed section body")
        if any(not body.strip() for body in self.section_bodies.values()):
            raise ValueError("Final lane section_bodies must not be blank")
        if any(not re.fullmatch(r"[0-9a-f]{64}", digest) for digest in self.unchanged_section_sha256.values()):
            raise ValueError("Final lane unchanged section hashes must be SHA-256")
        finding_ids = {finding.id for finding in self.required_findings}
        response_ids = {response.finding_id for response in self.revision_responses}
        if not response_ids.issubset(finding_ids):
            raise ValueError("Final lane responses must reference required lane findings")
        for finding in self.required_findings:
            if not set(finding.target_section_ids).issubset(self.section_ids):
                raise ValueError("Final lane findings must stay in lane section_ids")
        if self.phase == "initial" and (self.required_findings or self.revision_responses):
            raise ValueError("initial Final lane cannot carry recheck findings or responses")
        if self.phase == "recheck" and not self.required_findings:
            raise ValueError("Final recheck lane requires required findings")
        return self


class TemplateDistillationInput(StrictModel):
    kind: Literal["template_distillation_input"] = "template_distillation_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    template_ref: str = Field(
        min_length=1,
        description="Only template snapshot the distiller may inspect in this task.",
    )
    inspect_max_chars: int = Field(
        ge=1,
        description="Exact maximum character budget for the one allowed document inspection.",
    )
    required_part_ids: list[TemplateRoleSkillId] = Field(
        description="Exact fourteen role-, module-, and Chief-chapter Skill parts required before submission."
    )
    boundary_policy_version: Literal[1] = Field(
        default=1,
        description=(
            "Exact reusable-guidance boundary policy version the submission must declare; "
            "fact-free worked examples remain part of their corresponding Skill methods."
        ),
    )
    allowed_transfer_categories: list[
        Literal[
            "analysis_method",
            "synthesis_method",
            "visual_method",
            "quality_check",
        ]
    ] = Field(
        default_factory=lambda: sorted(TEMPLATE_SKILL_TRANSFER_CATEGORIES),
        description=(
            "Only template-content categories permitted in the reusable Skill. Each method "
            "category includes its fact-free structural, positive, and negative examples."
        ),
    )
    required_exclusion_categories: list[
        Literal[
            "domain_knowledge",
            "domain_standard_or_threshold",
            "project_fact_or_number",
            "customer_identity",
            "project_finding_or_risk",
            "project_conclusion_or_recommendation",
            "evidence_or_claim_identifier",
        ]
    ] = Field(
        default_factory=lambda: sorted(TEMPLATE_SKILL_EXCLUSION_CATEGORIES),
        description="Every non-method category the distiller must explicitly exclude.",
    )

    @model_validator(mode="after")
    def exact_parts(self) -> "TemplateDistillationInput":
        expected = set(TEMPLATE_ROLE_SKILL_IDS)
        if set(self.required_part_ids) != expected or len(self.required_part_ids) != len(expected):
            raise ValueError("template distillation requires exactly fourteen role Skill parts")
        if (
            set(self.allowed_transfer_categories)
            != TEMPLATE_SKILL_TRANSFER_CATEGORIES
            or len(self.allowed_transfer_categories)
            != len(TEMPLATE_SKILL_TRANSFER_CATEGORIES)
        ):
            raise ValueError("template distillation transfer categories are incomplete")
        if (
            set(self.required_exclusion_categories)
            != TEMPLATE_SKILL_EXCLUSION_CATEGORIES
            or len(self.required_exclusion_categories)
            != len(TEMPLATE_SKILL_EXCLUSION_CATEGORIES)
        ):
            raise ValueError("template distillation exclusion categories are incomplete")
        return self


class ModuleAuthoringInput(StrictModel):
    kind: Literal["module_authoring_input"] = "module_authoring_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Only responsibility module the specialist may author."
    )
    revision: int = Field(
        ge=0,
        description="Workflow-assigned initial subject revision.",
    )
    required_submodule_ids: list[str] = Field(
        min_length=1,
        description="Exact fixed submodules requiring complete narratives.",
    )
    coverage_ref: str = Field(
        min_length=1,
        description="Deterministic evidence-coverage matrix for this run.",
    )
    evidence_ref: str = Field(
        min_length=1,
        description="Current-run normalized project evidence ledger.",
    )
    manifest_ref: str = Field(
        min_length=1,
        description="Current-run project file manifest.",
    )
    knowledge_ref: str = Field(
        min_length=1,
        description=(
            "Provenance path for the module-specific Knowledge text already embedded in "
            "inline_context. It is not delivered by reference and must not be reopened."
        ),
    )
    collaboration_bundle_ref: str | None = Field(
        default=None,
        description=(
            "Current-run Barrier 2 bundle for this module. New three-wave authoring "
            "tasks must consume this compact typed artifact instead of blocking on "
            "live peer queries; the nullable default preserves legacy/recovery fixtures."
        ),
    )
    saved_part_ids: list[str] = Field(
        default_factory=list,
        description="Already persisted submodule part ids; no filesystem refs are exposed.",
    )
    rewrite_part_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Saved parts that must be rewritten under the current evidence-binding contract."
        ),
    )

    @model_validator(mode="after")
    def exact_module_scope(self) -> "ModuleAuthoringInput":
        expected = set(REPORT_TAXONOMY[self.module_id].submodules)
        if set(self.required_submodule_ids) != expected or len(self.required_submodule_ids) != len(
            expected
        ):
            raise ValueError("module authoring input requires every fixed submodule exactly once")
        if not set(self.saved_part_ids).issubset(expected):
            raise ValueError("existing result parts lie outside the module scope")
        if not set(self.rewrite_part_ids).issubset(expected):
            raise ValueError("correction rewrite parts lie outside the module scope")
        return self


class ModuleReviewInput(StrictModel):
    kind: Literal["module_review_input"] = "module_review_input"
    review_protocol_version: Literal[2] = Field(
        default=2,
        description="Version 2 binds one stable reviewer identity to each module.",
    )
    phase: Literal["initial", "local_regression", "recheck"] = Field(
        description=(
            "initial creates findings; local_regression checks a Cross-triggered scoped "
            "diff; recheck returns verdicts for required findings."
        )
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Only module whose local content is in review."
    )
    lifecycle_id: str = Field(
        pattern=r"^[a-z0-9-]+$",
        description=(
            "Workflow-owned immutable module-review lifecycle, such as initial or cross-r0."
        ),
    )
    review_round: int = Field(
        ge=0,
        description="Workflow-owned semantic review round within lifecycle_id.",
    )
    subject_ref: str = Field(
        min_length=1,
        description="Exact current module artifact reviewed in this pass.",
    )
    subject_revision: int = Field(
        ge=0,
        description="Workflow-owned revision number of subject_ref.",
    )
    subject: ModuleContentView = Field(
        description="Current module prose and per-submodule E evidence, without internal ids."
    )
    claim_statements: list[ReviewClaimStatement] = Field(
        default_factory=list,
        description=(
            "Review-safe Claim semantics bound to the exact required_submodule_ids "
            "without exposing the runtime authoring protocol."
        ),
    )
    prior_claim_statements: list[ReviewClaimStatement] = Field(
        default_factory=list,
        description=(
            "Recheck-only prior semantics for Claims added, removed, or changed since "
            "the last paid review. Unchanged statements are retained only by hash."
        ),
    )
    unchanged_submodule_sha256: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Recheck-only hashes for required submodule narratives unchanged since "
            "the last paid review."
        ),
    )
    unchanged_statement_sha256: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Recheck-only hashes for unchanged Claim statements in the required scope."
        ),
    )
    knowledge_ref: str | None = Field(
        default=None,
        description=(
            "Provenance path for the current-run module Knowledge artifact from which the "
            "inline bounded slice was made. Initial review carries the body; later tasks "
            "retain only this provenance because the auditor identity is persistent."
        ),
    )
    knowledge_context: str = Field(
        default="",
        description=(
            "Initial-review selected taxonomy sections from the immutable Knowledge snapshot; "
            "later persistent-identity tasks leave this empty unless Knowledge changed."
        ),
    )
    evidence: list[ReviewEvidenceExcerpt] = Field(
        description=(
            "Complete bounded packet for every E-* id bound to the reviewed subject. "
            "The reviewer must use this packet rather than reopen or research sources."
        )
    )
    required_submodule_ids: list[str] = Field(
        min_length=1,
        description="Fixed module-local scope that must be covered in this pass.",
    )
    required_findings: list[ModuleReviewFinding] = Field(
        default_factory=list,
        description=(
            "Immutable prior findings requiring one verdict each during recheck. "
            "Always empty in an initial review."
        ),
    )
    revision_responses: list[RevisionResponse] = Field(
        default_factory=list,
        description="Author responses to required_findings; never reviewer verdicts.",
    )
    prior_review_completion_ref: str | None = Field(
        default=None,
        description="Completed prior local review reused by a local_regression pass.",
    )
    prior_review_completion: "ReviewCompletionRecord | None" = Field(
        default=None,
        description="Typed prior completion proving the same module reviewer history.",
    )
    baseline_subject_ref: str | None = Field(
        default=None,
        description=(
            "Prior module subject used by local_regression or the last subject actually "
            "seen by the paid reviewer before a recheck."
        ),
    )
    trigger_cross_findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description="Cross findings that authorized the scoped module revision.",
    )
    trigger_revision_responses: list[RevisionResponse] = Field(
        default_factory=list,
        description="Owner responses to trigger_cross_findings.",
    )
    revision_diff_ref: str | None = Field(
        default=None,
        description="Deterministic bounded revision-diff artifact.",
    )
    revision_diff: ModuleRevisionDiff | None = Field(
        default=None,
        description="Exact typed diff from baseline_subject_ref to the current subject.",
    )
    validation_report_ref: str = Field(
        min_length=1,
        description=(
            "Independent structural validation artifact. It may reject structure "
            "but never decides semantic findings."
        ),
    )
    validation_report: "ValidationReport" = Field(
        description=(
            "Exact typed structural validation result for this subject."
        )
    )

    @model_validator(mode="after")
    def phase_fields_match(self) -> "ModuleReviewInput":
        scope = set(self.required_submodule_ids)
        if len(scope) != len(self.required_submodule_ids):
            raise ValueError("module review required_submodule_ids must be unique")
        if self.phase == "initial" and self.review_round != 0:
            raise ValueError("initial module review must use review_round zero")
        if self.phase == "local_regression" and self.review_round != 0:
            raise ValueError("local_regression module review must use review_round zero")
        if self.phase == "recheck" and self.review_round == 0:
            raise ValueError("module recheck requires a positive review_round")
        if self.subject.module_id != self.module_id:
            raise ValueError("module review subject belongs to a different module")
        if self.subject.revision != self.subject_revision:
            raise ValueError("module review subject_revision does not match subject")
        if (
            set(self.subject.submodule_narratives) - scope
            or set(self.subject.evidence_ids_by_submodule) - scope
        ):
            raise ValueError("module review subject lies outside required_submodule_ids")
        if self.knowledge_context.strip() and not self.knowledge_ref:
            raise ValueError(
                "module review inline Knowledge requires its provenance ref"
            )
        if self.phase == "initial" and bool(self.knowledge_ref) != bool(
            self.knowledge_context.strip()
        ):
            raise ValueError(
                "initial module review must provide Knowledge ref and body together"
            )
        subject_evidence_ids = {
            evidence_id
            for values in self.subject.evidence_ids_by_submodule.values()
            for evidence_id in values
        }
        supplied_evidence_ids = [item.evidence_id for item in self.evidence]
        if any(
            statement.submodule_id not in scope
            for statement in [*self.claim_statements, *self.prior_claim_statements]
        ):
            raise ValueError("module review statements must stay inside required_submodule_ids")
        current_statement_refs = [
            statement.statement_ref for statement in self.claim_statements
        ]
        prior_statement_refs = [
            statement.statement_ref for statement in self.prior_claim_statements
        ]
        if len(current_statement_refs) != len(set(current_statement_refs)):
            raise ValueError("module review current statement refs must be unique")
        if len(prior_statement_refs) != len(set(prior_statement_refs)):
            raise ValueError("module review prior statement refs must be unique")
        current_claim_evidence_ids = {
            evidence_id
            for statement in self.claim_statements
            for evidence_id in statement.evidence_ids
            if evidence_id.startswith("E-")
        }
        prior_claim_evidence_ids = {
            evidence_id
            for statement in self.prior_claim_statements
            for evidence_id in statement.evidence_ids
            if evidence_id.startswith("E-")
        }
        finding_evidence_ids = {
            evidence_ref
            for finding in self.required_findings
            for evidence_ref in finding.evidence_refs
            if evidence_ref.startswith("E-")
        }
        required_evidence_ids = (
            current_claim_evidence_ids
            | prior_claim_evidence_ids
            | finding_evidence_ids
            if self.phase == "recheck"
            else subject_evidence_ids
        )
        if (
            len(supplied_evidence_ids) != len(set(supplied_evidence_ids))
            or set(supplied_evidence_ids) != required_evidence_ids
        ):
            raise ValueError("module review evidence packet does not match its active semantics")
        if self.phase != "recheck" and current_claim_evidence_ids != subject_evidence_ids:
            raise ValueError("module review claims and subject evidence bindings differ")
        if not self.validation_report.passed:
            raise ValueError("module semantic review cannot start from failed structural validation")
        if (
            self.validation_report.validation_protocol_version < 2
            or self.validation_report.subject_ref != self.subject_ref
            or self.validation_report.subject_revision != self.subject_revision
        ):
            raise ValueError("module review validation is stale or belongs to another subject")
        if self.phase == "initial" and (self.required_findings or self.revision_responses):
            raise ValueError("initial module review cannot contain prior findings or responses")
        local_regression_values = (
            self.prior_review_completion_ref,
            self.prior_review_completion,
            self.trigger_cross_findings,
            self.trigger_revision_responses,
        )
        if self.phase != "local_regression" and any(local_regression_values):
            raise ValueError("only local_regression may contain Cross revision context")
        delta_values = (
            self.baseline_subject_ref,
            self.revision_diff_ref,
            self.revision_diff,
        )
        if self.phase == "initial" and any(delta_values):
            raise ValueError("initial module review cannot contain revision delta state")
        if self.phase != "recheck" and (
            self.prior_claim_statements
            or self.unchanged_submodule_sha256
            or self.unchanged_statement_sha256
        ):
            raise ValueError("only module recheck may contain compact delta state")
        if self.phase == "local_regression":
            if any(
                value is None
                for value in (
                    self.prior_review_completion_ref,
                    self.prior_review_completion,
                    self.baseline_subject_ref,
                    self.revision_diff_ref,
                    self.revision_diff,
                )
            ):
                raise ValueError("local_regression requires prior completion and revision diff")
            if not self.trigger_cross_findings:
                raise ValueError("local_regression requires its triggering Cross findings")
            trigger_ids = {finding.id for finding in self.trigger_cross_findings}
            response_ids = {
                response.finding_id for response in self.trigger_revision_responses
            }
            if trigger_ids != response_ids:
                raise ValueError("local_regression requires one owner response per Cross finding")
            if any(
                finding.owner_module_id != self.module_id
                or not set(finding.target_submodule_ids).issubset(
                    set(self.required_submodule_ids)
                )
                for finding in self.trigger_cross_findings
            ):
                raise ValueError("local_regression Cross findings lie outside its module scope")
            assert self.prior_review_completion is not None
            assert self.baseline_subject_ref is not None
            if (
                self.prior_review_completion.lifecycle != "module"
                or self.prior_review_completion.run_id != self.run_id
                or self.prior_review_completion.reviewer_agent_id
                != "evidence-auditor"
                or self.baseline_subject_ref
                not in self.prior_review_completion.subject_refs
            ):
                raise ValueError("local_regression prior completion does not bind its baseline")
            stable_reviewer = f"module-auditor-{self.module_id}"
            if (
                self.prior_review_completion.reviewer_session_key
                != stable_reviewer
                and not self.prior_review_completion.reviewer_session_key.startswith(
                    f"{stable_reviewer}-"
                )
            ):
                raise ValueError("local_regression prior completion belongs to another module")
            assert self.revision_diff is not None
            if (
                self.revision_diff.module_id != self.module_id
                or self.revision_diff.to_revision != self.subject_revision
                or not set(self.revision_diff.changed_submodule_narratives).issubset(
                    set(self.required_submodule_ids)
                )
            ):
                raise ValueError("local_regression diff does not bind its current scope")
        if self.phase == "recheck":
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError("module recheck requires exactly one author response per finding")
            if any(
                value is None
                for value in (
                    self.baseline_subject_ref,
                    self.revision_diff_ref,
                    self.revision_diff,
                )
            ):
                raise ValueError("module recheck requires its last-reviewed baseline and diff")
            assert self.revision_diff is not None
            if (
                self.revision_diff.module_id != self.module_id
                or self.revision_diff.to_revision != self.subject_revision
                or not set(self.revision_diff.changed_submodule_narratives).issubset(scope)
            ):
                raise ValueError("module recheck diff does not bind its current scope")
            changed_narratives = set(
                self.revision_diff.changed_submodule_narratives
            )
            if set(self.subject.submodule_narratives) != changed_narratives:
                raise ValueError("module recheck subject must contain only changed narratives")
            unchanged_submodules = scope - changed_narratives
            if set(self.unchanged_submodule_sha256) != unchanged_submodules:
                raise ValueError("module recheck must hash every unchanged required narrative")
            if any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.unchanged_submodule_sha256.values()
            ):
                raise ValueError("module recheck narrative hashes must be SHA-256")
            changed_statements = set(self.revision_diff.changed_statement_refs)
            visible_changed_statements = set(current_statement_refs) | set(
                prior_statement_refs
            )
            if visible_changed_statements != changed_statements:
                raise ValueError(
                    "module recheck must expose current and prior semantics for every "
                    "changed statement"
                )
            if set(self.unchanged_statement_sha256) & changed_statements:
                raise ValueError("changed statements cannot also be retained by hash")
            if any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.unchanged_statement_sha256.values()
            ):
                raise ValueError("module recheck statement hashes must be SHA-256")
            expected_subject_evidence = {
                submodule_id: sorted(
                    {
                        evidence_id
                        for statement in self.claim_statements
                        if statement.submodule_id == submodule_id
                        for evidence_id in statement.evidence_ids
                        if evidence_id.startswith("E-")
                    }
                )
                for submodule_id in (
                    changed_narratives
                    | {statement.submodule_id for statement in self.claim_statements}
                )
            }
            if self.subject.evidence_ids_by_submodule != expected_subject_evidence:
                raise ValueError(
                    "module recheck subject evidence must describe only current changed semantics"
                )
        return self

    @property
    def finding_id_prefix(self) -> str:
        return f"M-{self.module_id}-{self.lifecycle_id}-r{self.review_round}-"


class CrossReviewInput(StrictModel):
    kind: Literal["cross_review_input"] = "cross_review_input"
    owner_module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description=(
            "Responsibility module for this specialized Cross lane. Every finding and "
            "writeback target created by the lane must belong to this module."
        )
    )
    review_focus: list[str] = Field(
        default_factory=list,
        description=(
            "Module-specific interface questions for this Cross lane. These specialize "
            "discovery and do not grant module-authoring responsibility."
        ),
    )
    phase: Literal["initial", "recheck"] = Field(
        description="initial creates Cross findings; recheck closes required Cross findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_refs: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        description="Workflow-owned refs for the exact five reviewed module subjects."
    )
    module_revisions: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], int] = Field(
        description="Workflow-owned revision numbers corresponding to module_refs."
    )
    modules: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView] = Field(
        description=(
            "Initial: complete five-module subjects. Recheck: only modules changed by "
            "the current Cross revision wave."
        )
    )
    changed_module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(
        description="Initial: all five modules. Recheck: only current-wave revised owners."
    )
    unchanged_module_sha256: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        default_factory=dict,
        description=(
            "Recheck fingerprints for unchanged subjects retained in the same reviewer "
            "session without resending their full prose."
        ),
    )
    required_findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description="Immutable prior Cross findings requiring one verdict each on recheck.",
    )
    revision_responses_by_module: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], list[RevisionResponse]
    ] = Field(
        default_factory=dict,
        description="Owner-author responses grouped by responsibility module.",
    )
    local_regression_review_refs: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        default_factory=dict,
        description=(
            "Independent module-local regression completion refs. They do not close Cross findings."
        ),
    )
    prior_synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description="Previously supported synthesis inputs available during recheck.",
    )
    @model_validator(mode="after")
    def five_subjects_and_phase_fields_match(self) -> "CrossReviewInput":
        expected = {"2.1", "2.2", "2.3", "2.4", "2.5"}
        if set(self.module_refs) != expected or set(self.module_revisions) != expected:
            raise ValueError("cross review metadata requires exactly modules 2.1 through 2.5")
        for module_id, subject in self.modules.items():
            if (
                subject.module_id != module_id
                or subject.revision != self.module_revisions[module_id]
            ):
                raise ValueError("cross review module binding is inconsistent")
        changed = set(self.changed_module_ids)
        if len(changed) != len(self.changed_module_ids):
            raise ValueError("changed_module_ids must be unique")
        if self.phase == "initial" and (
            self.required_findings
            or self.revision_responses_by_module
            or self.local_regression_review_refs
            or self.unchanged_module_sha256
        ):
            raise ValueError("initial cross review cannot contain recheck state")
        if self.phase == "initial" and (set(self.modules) != expected or changed != expected):
            raise ValueError("initial cross review requires all five complete modules")
        if self.phase == "recheck":
            if any(
                finding.owner_module_id != self.owner_module_id
                for finding in self.required_findings
            ):
                raise ValueError(
                    "cross lane required findings must belong to owner_module_id"
                )
            if not changed or set(self.modules) != changed:
                raise ValueError("cross recheck full subjects must equal changed_module_ids")
            unchanged = expected - changed
            if set(self.unchanged_module_sha256) != unchanged:
                raise ValueError(
                    "cross recheck must fingerprint every unchanged module exactly once"
                )
            if any(
                not re.fullmatch(r"[0-9a-f]{64}", value)
                for value in self.unchanged_module_sha256.values()
            ):
                raise ValueError("unchanged module fingerprints must be SHA-256 hex")
            required = {finding.id for finding in self.required_findings}
            responses = {
                response.finding_id
                for values in self.revision_responses_by_module.values()
                for response in values
            }
            if responses != required:
                raise ValueError("cross recheck requires one owner response per finding")
        return self


class CrossOwnerRelatedModuleView(StrictModel):
    """Compact read-only relation view supplied to one Cross owner."""

    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Related module represented by this read-only compact view."
    )
    revision: int = Field(
        ge=0,
        description="Persisted revision of the related module subject."
    )
    subject_ref: str = Field(
        min_length=1,
        description="Immutable artifact ref for the related module subject."
    )
    subject_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 hash binding the related module subject artifact."
    )
    submodule_ids: list[str] = Field(
        min_length=1,
        description="Fixed submodule ids visible in the related compact view."
    )
    claims: list[ClaimRecord] = Field(
        default_factory=list,
        description=(
            "Compact claim summaries carrying text, type, E-* sources, confidence, "
            "and unresolved status for related-module reasoning."
        ),
    )
    evidence_ids_by_submodule: dict[str, list[str]] = Field(
        default_factory=dict,
        description="Registered E-* evidence ids bound to each related submodule."
    )
    unresolved_questions: list[str] = Field(
        default_factory=list,
        description="Open questions recorded by the related module author."
    )

    @model_validator(mode="after")
    def claims_match_module(self) -> "CrossOwnerRelatedModuleView":
        if any(claim.module_id != self.module_id for claim in self.claims):
            raise ValueError("related compact claims must remain in their module scope")
        if any(claim.submodule_id not in self.submodule_ids for claim in self.claims):
            raise ValueError("related compact claims must target visible submodules")
        return self


class CrossOwnerInput(StrictModel):
    """Typed input for one fixed Cross-owner reviewer.

    The owner receives its complete module view.  Every other module is present
    only as a compact, hash-bound relation view; those modules are read-only and
    cannot become revision targets for this lane.
    """

    kind: Literal["cross_owner_input"] = "cross_owner_input"
    phase: Literal["initial", "recheck"] = Field(
        description="Whether this is the owner's initial review or its bound recheck."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    review_round: int = Field(
        ge=0,
        description="Cross-owner review round represented by this input."
    )
    owner_module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Only module this owner may review and revise."
    )
    review_focus: list[str] = Field(
        min_length=1,
        description=(
            "Owner-module-specific Cross interface questions. They specialize discovery "
            "without granting authoring responsibility or injecting a professional Skill."
        ),
    )
    owner_subject_ref: str = Field(
        min_length=1,
        description="Immutable artifact ref for the owner's complete module subject."
    )
    owner_subject_revision: int = Field(
        ge=0,
        description="Revision of the owner's complete module subject."
    )
    owner_subject: ModuleContentView = Field(
        description="Complete current view of the module owned by this reviewer."
    )
    related_module_refs: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(description="Immutable refs for the other four read-only module subjects.")
    related_module_revisions: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], int
    ] = Field(description="Revisions for the other four read-only module subjects.")
    related_module_sha256: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(description="SHA-256 hashes for the other four read-only module subjects.")
    related_module_views: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], CrossOwnerRelatedModuleView
    ] = Field(description="Compact, hash-bound views for the other four modules.")
    required_findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description="Owner-scoped findings that the recheck must resolve."
    )
    revision_responses: list[RevisionResponse] = Field(
        default_factory=list,
        description="Owner responses corresponding one-for-one to required findings."
    )
    prior_synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description="Cross synthesis entries carried into the owner's recheck."
    )
    local_regression_review_ref: str | None = Field(
        default=None,
        description="Immutable owner-module Auditor completion used by recheck."
    )
    @model_validator(mode="after")
    def exact_owner_and_relation_scope(self) -> "CrossOwnerInput":
        if not self.review_focus:
            raise ValueError("Cross owner lane requires an owner-specific review focus")
        expected_related = set(REPORT_TAXONOMY) - {self.owner_module_id}
        if self.owner_subject.module_id != self.owner_module_id:
            raise ValueError("Cross owner subject belongs to another module")
        if self.owner_subject.revision != self.owner_subject_revision:
            raise ValueError("Cross owner subject revision does not match owner metadata")
        if set(self.related_module_refs) != expected_related:
            raise ValueError("Cross owner input must include exactly the other four modules")
        if set(self.related_module_revisions) != expected_related:
            raise ValueError("Cross owner relation revisions must cover the other four modules")
        if set(self.related_module_sha256) != expected_related:
            raise ValueError("Cross owner relation hashes must cover the other four modules")
        if set(self.related_module_views) != expected_related:
            raise ValueError("Cross owner compact relation views must cover the other four modules")
        for module_id in expected_related:
            view = self.related_module_views[module_id]
            if (
                view.module_id != module_id
                or view.revision != self.related_module_revisions[module_id]
                or view.subject_ref != self.related_module_refs[module_id]
                or view.subject_sha256 != self.related_module_sha256[module_id]
            ):
                raise ValueError("Cross owner relation view/hash binding is inconsistent")
        for finding in self.required_findings:
            if finding.owner_module_id != self.owner_module_id:
                raise ValueError("Cross owner required finding lies outside owner scope")
        if self.phase == "initial":
            if self.review_round != 0:
                raise ValueError("initial Cross owner review must use round zero")
            if self.required_findings or self.revision_responses:
                raise ValueError("initial Cross owner input cannot contain recheck state")
            if self.local_regression_review_ref:
                raise ValueError("initial Cross owner input cannot contain regression refs")
        else:
            if self.review_round <= 0:
                raise ValueError("Cross owner recheck requires a positive review round")
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError("Cross owner recheck requires one response per finding")
            if not self.local_regression_review_ref:
                raise ValueError("Cross owner recheck requires local regression completion")
        return self


class FinalReviewInput(StrictModel):
    kind: Literal["final_review_input"] = "final_review_input"
    phase: Literal["initial", "recheck"] = Field(
        description="initial creates final findings; recheck closes required final findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    cross_decision: CrossDecisionPackView = Field(
        description=(
            "Complete terminal Cross decision view. It is the only semantic source for "
            "cross-module synthesis, IF closures, and XMR verdicts in final review."
        )
    )
    cross_decision_pack_ref: str = Field(
        min_length=1,
        description="Current-run immutable CrossDecisionPack artifact reference.",
    )
    residual_risks: list[str] = Field(
        default_factory=list,
        description="Transparent non-corrective Cross/final limitations retained for the reader.",
    )
    subject_ref: str = Field(
        min_length=1,
        description="Exact current edited-report artifact reviewed in this pass.",
    )
    subject_revision: int = Field(
        ge=0,
        description="Workflow-owned chief-editor revision number.",
    )
    subject_metadata: FinalAuditMetadataView | None = Field(
        default=None,
        description=(
            "Initial-pass non-prose metadata absent from canonical_markdown. Recheck retains "
            "this unchanged metadata by hash instead of resending it; approved Chapter 2 "
            "prose is never duplicated here."
        )
    )
    subject_metadata_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Recheck hash of unchanged initial-pass subject_metadata.",
    )
    canonical_markdown: str | None = Field(
        default=None,
        min_length=1,
        description="Initial-pass deterministic Markdown corresponding to subject.",
    )
    changed_section_bodies: dict[str, str] = Field(
        default_factory=dict,
        description="Recheck-only bodies for exactly the sections assigned by open findings.",
    )
    unchanged_section_sha256: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Recheck-only content hashes for all chief-owned sections not changed in this round."
        ),
    )
    required_section_ids: list[str] = Field(
        min_length=1,
        description=(
            "Active final-report sections that the reviewer must cover. Chapter 4 is "
            "included only when subject.special_topic_plan is present."
        ),
    )
    required_findings: list[FinalReviewFinding] = Field(
        default_factory=list,
        description="Immutable prior final findings requiring one verdict each on recheck.",
    )
    revision_responses: list[RevisionResponse] = Field(
        default_factory=list,
        description="Chief-editor responses to required_findings.",
    )
    validation_report_ref: str = Field(
        min_length=1,
        description="Independent structural validation report; never a semantic verdict.",
    )
    validation_report: "ValidationReport" = Field(
        description="Exact typed structural validation result for the current edited report."
    )

    @model_validator(mode="after")
    def phase_fields_match(self) -> "FinalReviewInput":
        base_sections = set(FINAL_AUDIT_SECTION_IDS)
        active_sections = set(self.required_section_ids)
        if (
            len(self.required_section_ids) != len(set(self.required_section_ids))
            or active_sections != base_sections
        ):
            raise ValueError(
                "final review must cover exactly the seven summary/conclusion sections; "
                "Chapter 2 and Chapter 4 are not semantic final-review targets"
            )
        if self.cross_decision.run_id != self.run_id:
            raise ValueError("final review CrossDecisionPack belongs to another run")
        expected_pack_ref = f"Work/runs/{self.run_id}/"
        if not self.cross_decision_pack_ref.startswith(expected_pack_ref):
            raise ValueError("final review CrossDecisionPack ref belongs to another run")
        if not self.validation_report.passed:
            raise ValueError("final review requires a passed structural validation report")
        if (
            self.validation_report.validation_protocol_version < 2
            or self.validation_report.subject_revision != self.subject_revision
        ):
            raise ValueError("final review validation identity is stale")
        audit_sections = active_sections
        changed_sections = set(self.changed_section_bodies)
        unchanged_sections = set(self.unchanged_section_sha256)
        if self.phase == "initial":
            if self.required_findings or self.revision_responses:
                raise ValueError("initial final review cannot contain prior findings or responses")
            if self.canonical_markdown is None or self.subject_metadata is None:
                raise ValueError("initial final review requires full prose and metadata")
            if re.search(r"(?m)^#{1,6}\s+2(?:\.|\s)|^#{1,6}\s+4(?:\.|\s)", self.canonical_markdown):
                raise ValueError(
                    "final review canonical Markdown cannot contain Chapter 2 or Chapter 4"
                )
            if (
                self.subject_metadata_sha256 is not None
                or changed_sections
                or unchanged_sections
            ):
                raise ValueError("initial final review cannot contain recheck delta fields")
        if self.phase == "recheck":
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError("final recheck requires one chief response per finding")
            targets = {
                section_id
                for finding in self.required_findings
                for section_id in finding.target_section_ids
            }
            if self.canonical_markdown is not None or self.subject_metadata is not None:
                raise ValueError("final recheck must not resend full prose or metadata")
            if self.subject_metadata_sha256 is None:
                raise ValueError("final recheck requires the retained metadata hash")
            if changed_sections != targets:
                raise ValueError("final recheck delta must contain exactly finding targets")
            if unchanged_sections != audit_sections - targets:
                raise ValueError("final recheck must hash every unchanged audit section")
            if any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.unchanged_section_sha256.values()
            ):
                raise ValueError("final recheck unchanged section hashes must be SHA-256")
        return self


class AggregateFinalReviewInput(StrictModel):
    """Independent final-review adapter for ``aggregate_existing``.

    Aggregate reports intentionally have no Cross lifecycle.  This contract
    carries the same seven-section audit delta as :class:`FinalReviewInput`
    while making the independent mode and null ``cross_context`` explicit.
    It must never be populated with a fabricated CrossDecisionPack.
    """

    kind: Literal["aggregate_final_review_input"] = "aggregate_final_review_input"
    mode: Literal["aggregate_existing"] = Field(
        default="aggregate_existing",
        description="Explicit independent aggregate_existing route with no Cross lifecycle.",
    )
    cross_context: None = Field(
        default=None,
        description="Aggregate final review has no CrossDecisionPack.",
    )
    phase: Literal["initial", "recheck"] = Field(
        description="Initial creates findings; recheck closes assigned findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    residual_risks: list[str] = Field(
        default_factory=list,
        description="Transparent aggregate limitations retained for the reader.",
    )
    subject_ref: str = Field(
        min_length=1, description="Exact current edited-report artifact under review."
    )
    subject_revision: int = Field(
        ge=0, description="Workflow-owned chief-editor revision number."
    )
    subject_metadata: FinalAuditMetadataView | None = Field(
        default=None,
        description="Initial-pass non-prose metadata retained outside canonical Markdown.",
    )
    subject_metadata_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
        description="Recheck hash retaining unchanged initial-pass metadata.",
    )
    canonical_markdown: str | None = Field(
        default=None, min_length=1, description="Initial-pass seven-section audit Markdown."
    )
    changed_section_bodies: dict[str, str] = Field(
        default_factory=dict,
        description="Recheck bodies for exactly the assigned finding targets.",
    )
    unchanged_section_sha256: dict[str, str] = Field(
        default_factory=dict,
        description="Recheck SHA-256 values for every unchanged audit section.",
    )
    required_section_ids: list[str] = Field(
        min_length=1, description="Exactly the seven 1.x and 3.x audit section ids."
    )
    required_findings: list[FinalReviewFinding] = Field(
        default_factory=list, description="Immutable prior findings requiring verdicts."
    )
    revision_responses: list[RevisionResponse] = Field(
        default_factory=list, description="Chief responses to required findings."
    )
    validation_report_ref: str = Field(
        min_length=1, description="Current-run deterministic structural validation artifact."
    )
    validation_report: "ValidationReport" = Field(
        description="Passed validation report bound to the exact subject revision."
    )

    @model_validator(mode="after")
    def phase_fields_match(self) -> "AggregateFinalReviewInput":
        base_sections = set(FINAL_AUDIT_SECTION_IDS)
        active_sections = set(self.required_section_ids)
        if (
            len(self.required_section_ids) != len(set(self.required_section_ids))
            or active_sections != base_sections
        ):
            raise ValueError(
                "aggregate final review must cover exactly the seven summary/conclusion sections; "
                "Chapter 2 and Chapter 4 are not semantic final-review targets"
            )
        if not self.validation_report.passed:
            raise ValueError("aggregate final review requires a passed structural validation report")
        if (
            self.validation_report.validation_protocol_version < 2
            or self.validation_report.subject_revision != self.subject_revision
        ):
            raise ValueError(
                "aggregate final review validation is stale or belongs to another revision"
            )
        audit_sections = active_sections
        changed_sections = set(self.changed_section_bodies)
        unchanged_sections = set(self.unchanged_section_sha256)
        if self.phase == "initial":
            if self.required_findings or self.revision_responses:
                raise ValueError(
                    "initial aggregate final review cannot contain prior findings or responses"
                )
            if self.canonical_markdown is None or self.subject_metadata is None:
                raise ValueError(
                    "initial aggregate final review requires full prose and metadata"
                )
            if re.search(r"(?m)^#{1,6}\s+2(?:\.|\s)|^#{1,6}\s+4(?:\.|\s)", self.canonical_markdown):
                raise ValueError(
                    "aggregate final review canonical Markdown cannot contain Chapter 2 or Chapter 4"
                )
            if self.subject_metadata_sha256 is not None or changed_sections or unchanged_sections:
                raise ValueError(
                    "initial aggregate final review cannot contain recheck delta fields"
                )
        else:
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError(
                    "aggregate final recheck requires one chief response per finding"
                )
            targets = {
                section_id
                for finding in self.required_findings
                for section_id in finding.target_section_ids
            }
            if self.canonical_markdown is not None or self.subject_metadata is not None:
                raise ValueError(
                    "aggregate final recheck must not resend full prose or metadata"
                )
            if self.subject_metadata_sha256 is None:
                raise ValueError(
                    "aggregate final recheck requires the retained metadata hash"
                )
            if changed_sections != targets:
                raise ValueError(
                    "aggregate final recheck delta must contain exactly finding targets"
                )
            if unchanged_sections != audit_sections - targets:
                raise ValueError(
                    "aggregate final recheck must hash every unchanged audit section"
                )
            if any(
                re.fullmatch(r"[0-9a-f]{64}", digest) is None
                for digest in self.unchanged_section_sha256.values()
            ):
                raise ValueError(
                    "aggregate final recheck unchanged section hashes must be SHA-256"
                )
        return self


class RequestedModuleChange(StrictModel):
    id: str = Field(
        min_length=1,
        description="Stable workflow-created id for one explicit user revision request.",
    )
    instruction: str = Field(
        min_length=1,
        description="Verbatim user-requested module change; never rewritten as a reviewer finding.",
    )
    target_submodule_ids: list[str] = Field(
        min_length=1,
        description="Fixed module-local submodules authorized for this requested change.",
    )


class ModuleRevisionInput(StrictModel):
    kind: Literal["module_revision_input"] = "module_revision_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Responsibility module being revised."
    )
    subject_ref: str = Field(min_length=1, description="Exact module revision being patched.")
    subject: ModuleContentView = Field(
        description=(
            "Only assigned submodule prose and per-submodule E evidence baseline. "
            "Unassigned content remains workflow-owned and is not resent."
        )
    )
    target_submodule_ids: list[str] = Field(
        min_length=1,
        description="Only fixed submodules the explicit patch may replace.",
    )
    module_findings: list[ModuleReviewFinding] = Field(
        default_factory=list,
        description="Assigned module-local findings owned by this author.",
    )
    cross_findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description="Assigned Cross findings owned by this module author.",
    )
    requested_changes: list[RequestedModuleChange] = Field(
        default_factory=list,
        description="Explicit user-requested changes assigned to this module author.",
    )
    validation_report_ref: str | None = Field(
        default=None,
        description="Optional failed structural-validation artifact from the prior patch.",
    )
    validation_report: "ValidationReport | None" = Field(
        default=None,
        description=(
            "Exact independent structural failures to correct; these are not reviewer findings."
        ),
    )

    @model_validator(mode="after")
    def assigned_findings_are_nonempty_and_in_scope(self) -> "ModuleRevisionInput":
        if self.subject.module_id != self.module_id:
            raise ValueError("module revision baseline belongs to a different module")
        targets = set(self.target_submodule_ids)
        if (
            set(self.subject.submodule_narratives) != targets
            or set(self.subject.evidence_ids_by_submodule) != targets
        ):
            raise ValueError(
                "module revision subject must contain exactly the assigned target submodules"
            )
        if bool(self.validation_report_ref) != bool(self.validation_report):
            raise ValueError(
                "validation_report_ref and validation_report must be provided together"
            )
        if self.validation_report and self.validation_report.passed:
            raise ValueError("a passed validation report cannot trigger another revision")
        if (
            not self.module_findings
            and not self.cross_findings
            and not self.requested_changes
            and self.validation_report is None
        ):
            raise ValueError(
                "module revision input requires a finding, requested change, or "
                "failed structural validation"
            )
        if self.validation_report and (
            self.validation_report.validation_protocol_version < 2
            or self.validation_report.subject_ref != self.subject_ref
            or self.validation_report.subject_revision != self.subject.revision
        ):
            raise ValueError("module revision validation belongs to another subject")
        allowed = targets
        for finding in self.module_findings:
            if finding.target_submodule_id not in allowed:
                raise ValueError("module finding lies outside revision targets")
        for finding in self.cross_findings:
            if finding.owner_module_id != self.module_id:
                raise ValueError("cross finding belongs to another owner module")
            if not set(finding.target_submodule_ids).intersection(allowed):
                raise ValueError("cross finding lies outside revision targets")
        for change in self.requested_changes:
            if not set(change.target_submodule_ids).intersection(allowed):
                raise ValueError("requested change lies outside revision targets")
        return self


class ChiefRevisionInput(StrictModel):
    kind: Literal["chief_revision_input"] = "chief_revision_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    subject_ref: str = Field(min_length=1, description="Exact edited report being revised.")
    target_section_bodies: dict[str, str] = Field(
        description=(
            "Exact current prose for target_section_ids in chief-owned Chapters 1 and 3 "
            "plus optional Chapter 4; immutable Chapter 2 and unassigned bodies are not repeated."
        )
    )
    consistency_context: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Bounded chief-owned sections needed to check summary and synthesis consistency. "
            "These sections are read-only and disjoint from target_section_ids."
        ),
    )
    revision: int = Field(
        ge=1,
        description="Workflow-owned chief revision number for this exact patch.",
    )
    target_section_ids: list[str] = Field(
        min_length=1,
        description="Only final-report sections the chief editor may change.",
    )
    findings: list[FinalReviewFinding] = Field(
        min_length=1,
        description="Immutable final-review findings assigned to the chief editor.",
    )
    @model_validator(mode="after")
    def findings_and_targets_match(self) -> "ChiefRevisionInput":
        invalid = sorted(set(self.target_section_ids) - set(FINAL_AUDIT_SECTION_IDS))
        if invalid:
            raise ValueError(f"chief revision contains non-chief target sections: {invalid}")
        finding_targets = {
            target for finding in self.findings for target in finding.target_section_ids
        }
        if set(self.target_section_ids) != finding_targets:
            raise ValueError("chief revision targets must exactly match assigned final findings")
        if set(self.target_section_bodies) != set(self.target_section_ids):
            raise ValueError("chief revision target bodies must exactly match target sections")
        invalid_context = sorted(
            set(self.consistency_context) - set(FINAL_AUDIT_SECTION_IDS)
        )
        if invalid_context:
            raise ValueError(
                f"chief revision context contains non-chief sections: {invalid_context}"
            )
        overlap = sorted(
            set(self.consistency_context).intersection(self.target_section_ids)
        )
        if overlap:
            raise ValueError(
                f"chief revision context duplicates target sections: {overlap}"
            )
        return self


class ChiefEditorInput(StrictModel):
    kind: Literal["chief_editor_input"] = "chief_editor_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    cross_decision: CrossDecisionPackView | None = Field(
        default=None,
        description=(
            "Terminal Cross decision view. Required for a normal full-report Chief edit; "
            "legacy aggregate callers may omit it and use cross_review_completion_ref."
        ),
    )
    cross_decision_pack_ref: str | None = Field(
        default=None,
        description="Current-run immutable CrossDecisionPack artifact reference.",
    )
    approved_module_markers: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        default_factory=dict,
        description="Exact marker tokens the chief submits for deterministic prose insertion."
    )
    modules: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView] = Field(
        description="Five module subjects closed by module review."
    )
    # Compatibility-only alias for pre-pack checkpoints.  New full-report
    # callers must provide cross_decision + pack ref together.
    cross_review_completion_ref: str | None = Field(
        default=None,
        description="Legacy Cross completion ref retained only for old checkpoints.",
    )
    special_topic_plan: SpecialTopicPlan | None = Field(
        default=None,
        description=(
            "Optional immutable Chapter 4 plan. None means Inputs has no non-empty "
            "standalone special-topic Markdown and the report must omit Chapter 4."
        )
    )

    @model_validator(mode="after")
    def five_modules_and_markers_match(self) -> "ChiefEditorInput":
        expected = {"2.1", "2.2", "2.3", "2.4", "2.5"}
        if set(self.modules) != expected:
            raise ValueError("chief editor input requires exactly five modules")
        if self.approved_module_markers and set(self.approved_module_markers) != expected:
            raise ValueError("chief editor markers must cover exactly five modules when supplied")
        for module_id, subject in self.modules.items():
            if subject.module_id != module_id:
                raise ValueError("chief editor module binding is inconsistent")
            if self.approved_module_markers and self.approved_module_markers[module_id] != (
                f"[[APPROVED_MODULE:{module_id}]]"
            ):
                raise ValueError("chief editor marker does not match its module")
        supplied_pack_fields = (
            self.cross_decision,
            self.cross_decision_pack_ref,
        )
        if any(value is not None for value in supplied_pack_fields):
            if not all(value is not None for value in supplied_pack_fields):
                raise ValueError(
                    "cross_decision and cross_decision_pack_ref must be supplied together"
                )
            assert self.cross_decision is not None
            assert self.cross_decision_pack_ref is not None
            if self.cross_decision.run_id != self.run_id:
                raise ValueError("chief CrossDecisionPack belongs to another run")
            if not self.cross_decision_pack_ref.startswith(f"Work/runs/{self.run_id}/"):
                raise ValueError("chief CrossDecisionPack ref belongs to another run")
            if set(self.cross_decision.module_ids) != expected:
                raise ValueError("chief CrossDecisionPack must bind exactly five modules")
        elif not self.cross_review_completion_ref:
            raise ValueError(
                "chief editor input requires a terminal CrossDecisionPack (or a legacy completion ref)"
            )
        return self


class AggregateEditorInput(StrictModel):
    kind: Literal["aggregate_editor_input"] = "aggregate_editor_input"
    mode: Literal["aggregate_existing"] = Field(
        default="aggregate_existing",
        description="Explicit independent aggregate route; it does not run Cross review.",
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    cross_context: None = Field(
        default=None,
        description="Aggregate route has no CrossDecisionPack and must remain null.",
    )
    source_format: Literal["structured_module", "markdown"] = Field(
        description="Whether modules are typed subjects or validated standalone Markdown."
    )
    approved_module_markers: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        description="Exact marker tokens used for deterministic module-prose insertion."
    )
    special_topic_plan: SpecialTopicPlan | None = Field(
        default=None,
        description=(
            "Optional immutable Chapter 4 plan parsed from Inputs. None requires the "
            "aggregate report to omit Chapter 4."
        ),
    )
    structured_modules: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView] = Field(
        default_factory=dict,
        description="Exactly five typed modules when source_format=structured_module.",
    )
    markdown_modules: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str] = Field(
        default_factory=dict,
        description="Exactly five validated Markdown bodies when source_format=markdown.",
    )

    @model_validator(mode="after")
    def source_payload_matches_format(self) -> "AggregateEditorInput":
        expected = {"2.1", "2.2", "2.3", "2.4", "2.5"}
        if self.source_format == "structured_module":
            if set(self.structured_modules) != expected or self.markdown_modules:
                raise ValueError(
                    "structured aggregate input requires exactly five structured_modules"
                )
        elif set(self.markdown_modules) != expected or self.structured_modules:
            raise ValueError("markdown aggregate input requires exactly five markdown_modules")
        return self


class WorkflowExceptionInput(StrictModel):
    kind: Literal["workflow_exception_input"] = "workflow_exception_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    scope: Literal["module", "cross", "final"] = Field(
        description="Review lifecycle that raised the exception."
    )
    trigger: Literal["reviewer_escalation", "author_response"] = Field(
        description="Whether the original reviewer or the responsible author triggered Main.",
    )
    finding_ids: list[str] = Field(
        min_length=1,
        description="Exact immutable finding ids Main must decide in this exception.",
    )
    subject_refs: list[str] = Field(
        min_length=1,
        description="Current subjects Main must inspect before deciding.",
    )
    finding_refs: list[str] = Field(
        min_length=1,
        description="Artifacts containing the immutable escalated findings.",
    )
    verdicts: list[ResolutionVerdict] = Field(
        default_factory=list,
        description="Reviewer escalate verdicts; empty for an author-triggered exception.",
    )
    revision_responses: list[RevisionResponse] = Field(
        min_length=1,
        description="Author responses associated with every exception finding.",
    )

    @model_validator(mode="after")
    def trigger_payload_is_exact(self) -> "WorkflowExceptionInput":
        finding_ids = set(self.finding_ids)
        response_ids = {
            response.finding_id
            for response in self.revision_responses
            if response.finding_id in finding_ids
        }
        if response_ids != finding_ids:
            raise ValueError(
                "workflow exception requires one associated author response per finding"
            )
        if self.trigger == "reviewer_escalation":
            verdict_ids = {
                verdict.finding_id for verdict in self.verdicts if verdict.verdict == "escalate"
            }
            if verdict_ids != finding_ids:
                raise ValueError("reviewer exception ids must equal escalate verdict ids")
        elif self.verdicts:
            raise ValueError("author-response exception must not contain reviewer verdicts")
        if self.trigger == "author_response":
            exceptional = {
                response.finding_id
                for response in self.revision_responses
                if response.action in {"disputed", "needs_input"}
            }
            if exceptional != finding_ids:
                raise ValueError(
                    "author-response exception ids must equal disputed or needs_input responses"
                )
        return self


class ValidationFailure(StrictModel):
    check_id: str = Field(min_length=1, description="Stable structural check id.")
    finding_id: str | None = Field(
        default=None,
        description="Finding associated with this structural failure, when applicable.",
    )
    target_path: str = Field(
        min_length=1,
        description="Exact structured path inspected by the structural check.",
    )
    message: str = Field(
        min_length=1,
        description="Observed structural mismatch without semantic reviewer judgment.",
    )


class ValidationReport(StrictModel):
    kind: Literal["validation_report"] = "validation_report"
    validation_protocol_version: int = Field(
        default=1,
        ge=1,
        description="Validation binding protocol; missing legacy values are version 1.",
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    subject_ref: str = Field(min_length=1, description="Exact validated subject artifact.")
    subject_revision: int | None = Field(
        default=None,
        ge=0,
        description="Workflow revision of the exact validated subject when revisioned.",
    )
    validator: str = Field(
        min_length=1,
        description="Deterministic validator implementation and version identifier.",
    )
    check_ids: list[str] = Field(
        description="All explicit structural checks executed for this subject."
    )
    failures: list[ValidationFailure] = Field(
        default_factory=list,
        description="Structural failures; empty means structural validation passed.",
    )
    passed: bool = Field(
        description="Derived by the validator from failures; never a semantic approval."
    )

    @model_validator(mode="after")
    def passed_matches_failures(self) -> "ValidationReport":
        if self.passed != (not self.failures):
            raise ValueError("validation report passed must equal not failures")
        return self


class ReviewCompletionRecord(StrictModel):
    kind: Literal["review_completion_record"] = "review_completion_record"
    review_protocol_version: int = Field(
        default=1,
        ge=1,
        description="Review protocol that produced this completion; missing legacy values are v1.",
    )
    lifecycle: Literal["module", "cross", "final"] = Field(
        description="Review lifecycle completed by this record."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    reviewer_agent_id: str = Field(
        min_length=1,
        description="Reviewer identity that performed the initial review and any rechecks.",
    )
    reviewer_session_key: str = Field(
        min_length=1,
        description="Stable workflow session binding for the original reviewer.",
    )
    subject_refs: list[str] = Field(
        min_length=1,
        description="Exact subject artifacts accepted by the completed lifecycle.",
    )
    finding_refs: list[str] = Field(
        description="Initial and regression finding artifacts considered by completion."
    )
    verdict_refs: list[str] = Field(description="Reviewer verdict artifacts closing every finding.")
    resolved_finding_ids: list[str] = Field(
        description="Every finding id closed by reviewer verdict or empty initial findings."
    )

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_digest_metadata(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value.pop("artifact_sha256", None)
        return value


class FinalAuditSnapshot(StrictModel):
    """Typed bridge proving audit and delivery use one current-run subject."""

    kind: Literal["final_audit_snapshot"] = "final_audit_snapshot"
    run_id: str = Field(min_length=1)
    subject_ref: str = Field(min_length=1)
    subject_revision: int = Field(ge=0)
    canonical_markdown_ref: str = Field(min_length=1)
    validation_report_ref: str = Field(min_length=1)
    completion_ref: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def discard_legacy_digest_metadata(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            for field in (
                "subject_sha256",
                "canonical_markdown_sha256",
                "validation_report_sha256",
                "completion_sha256",
            ):
                value.pop(field, None)
        return value


INPUT_CONTRACT_TYPES = {
    "template_distillation_input": TemplateDistillationInput,
    "module_authoring_input": ModuleAuthoringInput,
    "module_review_input": ModuleReviewInput,
    "cross_review_input": CrossReviewInput,
    "cross_owner_input": CrossOwnerInput,
    "final_review_input": FinalReviewInput,
    "aggregate_final_review_input": AggregateFinalReviewInput,
    "module_revision_input": ModuleRevisionInput,
    "chief_revision_input": ChiefRevisionInput,
    "chief_chapter_lane_input": ChiefChapterLaneInput,
    "final_chapter_lane_input": FinalChapterLaneInput,
    "chief_editor_input": ChiefEditorInput,
    "aggregate_editor_input": AggregateEditorInput,
    "workflow_exception_input": WorkflowExceptionInput,
}

INPUT_CONTRACT_SUMMARIES = {
    "template_distillation_input": "One exact template snapshot and five required durable output parts.",
    "module_authoring_input": "One fixed module scope with role-labelled current-run evidence inputs.",
    "module_review_input": "One exact module subject plus phase-specific immutable review state.",
    "cross_review_input": "Five exact module subjects plus phase-specific Cross closure state.",
    "cross_owner_input": "One complete owner module plus four compact read-only relation views.",
    "final_review_input": "One exact edited report plus a typed terminal CrossDecisionPack and seven-section final-review state.",
    "aggregate_final_review_input": "Independent aggregate_existing final review with null cross_context and seven-section audit state; no CrossDecisionPack.",
    "module_revision_input": "One exact module baseline and only the findings assigned to its author.",
    "chief_revision_input": "One exact edited-report baseline and immutable final findings.",
    "chief_chapter_lane_input": "One Chief chapter lane with only its assigned section bodies and findings.",
    "final_chapter_lane_input": "One Final chapter lane with only its assigned section bodies and findings.",
    "chief_editor_input": "Five module-review-complete subjects and a typed terminal CrossDecisionPack view.",
    "aggregate_editor_input": "Independent aggregate_existing mode: five validated module bodies and null cross_context, with no fabricated CrossDecisionPack.",
    "workflow_exception_input": "Only explicit author or reviewer exception findings and their immutable artifacts for Main.",
}


def _example_module(module_id: str = "2.1") -> dict[str, Any]:
    return {
        "kind": "module_submission",
        "module_id": module_id,
        "submodule_narratives": {
            submodule_id: f"### {submodule_id} 示例\n\n当前结构化输入中的示例正文。"
            for submodule_id in REPORT_TAXONOMY[module_id].submodules
        },
        "claims": [],
        "source_ids": [],
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }


def _example_module_finding() -> dict[str, Any]:
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    return {
        "id": "M-2.1-initial-r0-001",
        "target_submodule_id": target,
        "category": "analysis_depth",
        "impact": "advisory",
        "observation": "当前正文没有说明判断如何连接到建议的验收方法，行动闭环不清楚。",
        "evidence_refs": ["Work/runs/report-example/modules/2.1-r0.json"],
        "required_change": "在目标小节补充与当前判断一致的行动责任和可验证验收方法。",
        "reviewer_checks": ["核对建议与判断之间的逻辑及验收方法是否可执行"],
    }


def _example_cross_finding() -> dict[str, Any]:
    target = next(iter(REPORT_TAXONOMY["2.1"].submodules))
    return {
        "id": "X-001",
        "owner_module_id": "2.1",
        "target_submodule_ids": [target],
        "related_module_ids": ["2.2"],
        "category": "dependencies",
        "impact": "blocking",
        "observation": "模块 2.1 的行动没有说明其对模块 2.2 前置条件的依赖，实施顺序可能冲突。",
        "evidence_refs": [
            "Work/runs/report-example/modules/2.1-r0.json",
            "Work/runs/report-example/modules/2.2-r0.json",
        ],
        "required_change": "在责任小节写明依赖对象、作用机制、实施顺序和联合验收方式。",
        "reviewer_checks": ["责任模块正文明确连接对象、依赖顺序与联合验收"],
    }


def _example_final_finding() -> dict[str, Any]:
    return {
        "id": "F-001",
        "target_section_ids": ["3.2"],
        "target_changes": [
            {
                "target_section_id": "3.2",
                "required_change": ("在行动计划中补充跨模块责任接口、依赖顺序、验收指标和剩余风险边界。"),
                "reviewer_checks": ["行动责任、依赖顺序、验收指标和剩余风险均可核对"],
            }
        ],
        "category": "special_topic",
        "impact": "blocking",
        "observation": "当前行动计划没有清楚说明跨模块责任接口、依赖顺序和联合验收边界。",
        "evidence_refs": ["Work/runs/report-example/edited-revisions/chief-r0.json"],
    }


def _example_special_topic_plan() -> dict[str, Any]:
    return {
        "source_ref": "Inputs/专项问题分析.md",
        "source_sha256": "0" * 64,
        "sections": [
            {
                "section_id": "4.1",
                "title": "动态专项问题",
                "requirement": "结合项目证据说明判断边界、可选方案、实施条件和验证方法。",
            }
        ],
    }


def _example_edited_report() -> dict[str, Any]:
    return {
        "kind": "edited_report_submission",
        "title": "示例报告",
        "assessment_background": "说明评估范围、证据基础和适用边界。",
        "findings_overview": "归纳主要发现及其管理含义。",
        "regional_executive_summary": "按真实责任边界归纳行动。",
        "module_narratives": {
            module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY
        },
        "risk_panorama": "归纳当前模块成果支持的主要风险及其判断依据。",
        "dimension_risk_analysis": "比较五个维度的风险和管理含义。",
        "data_gap_analysis": "说明缺口、判断影响和补证优先级。",
        "improvement_action_plan": "列出责任、依赖、行动、验收和剩余风险。",
        "special_topic_plan": _example_special_topic_plan(),
        "special_topic_analysis": (
            "### 4.1 动态专项问题\n\n"
            "结合项目证据边界分析可选方案、实施条件和验证方法，并明确通用知识并非客户事实。"
        ),
        "tables": [],
        "photo_ids": [],
        "unresolved_editorial_issues": [],
        "revision_responses": [],
    }


def _example_final_audit_subject() -> dict[str, Any]:
    payload = _example_edited_report()
    payload.pop("kind")
    payload.pop("module_narratives")
    payload.pop("revision_responses")
    return payload


def _example_final_audit_metadata() -> dict[str, Any]:
    subject = _example_final_audit_subject()
    return {
        "unresolved_editorial_issues": subject.get(
            "unresolved_editorial_issues", []
        ),
    }


_EXAMPLE_MODULES = {
    module_id: module_content_view(
        ModuleSubmission.model_validate(_example_module(module_id))
    ).model_dump(mode="json")
    for module_id in REPORT_TAXONOMY
}
_EXAMPLE_MODULE_REFS = {
    module_id: f"Work/runs/report-example/modules/{module_id}-r0.json"
    for module_id in REPORT_TAXONOMY
}
_EXAMPLE_MARKERS = {module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY}
_EXAMPLE_CROSS_COMPLETION_REF = "Work/runs/report-example/reviews/cross-completion.json"
_EXAMPLE_CROSS_DECISION = {
    "version": 1,
    "run_id": "report-example",
    "module_ids": list(REPORT_TAXONOMY),
    "cross_review_completion_ref": _EXAMPLE_CROSS_COMPLETION_REF,
    "synthesis_inputs": [],
    "artifact_refs": [_EXAMPLE_CROSS_COMPLETION_REF],
}

INPUT_CONTRACT_EXAMPLES: dict[str, dict[str, Any]] = {
    "template_distillation_input": {
        "kind": "template_distillation_input",
        "run_id": "report-example",
        "template_ref": "Work/runs/report-example/templates/template-for-skill.docx",
        "inspect_max_chars": 100000,
        "required_part_ids": list(TEMPLATE_ROLE_SKILL_IDS),
    },
    "module_authoring_input": {
        "kind": "module_authoring_input",
        "run_id": "report-example",
        "module_id": "2.1",
        "revision": 0,
        "required_submodule_ids": list(REPORT_TAXONOMY["2.1"].submodules),
        "coverage_ref": "Work/runs/report-example/preparation/coverage.json",
        "evidence_ref": "Work/runs/report-example/preparation/evidence.jsonl",
        "manifest_ref": "Work/runs/report-example/preparation/manifest.json",
        "knowledge_ref": "Work/runs/report-example/knowledge/module-2.1.md",
        "saved_part_ids": [],
        "rewrite_part_ids": [],
    },
    "module_review_input": {
        "kind": "module_review_input",
        "phase": "initial",
        "run_id": "report-example",
        "module_id": "2.1",
        "lifecycle_id": "initial",
        "review_round": 0,
        "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
        "subject_revision": 0,
        "subject": _EXAMPLE_MODULES["2.1"],
        "evidence": [],
        "required_submodule_ids": list(REPORT_TAXONOMY["2.1"].submodules),
        "required_findings": [],
        "revision_responses": [],
        "validation_report_ref": ("Work/runs/report-example/reviews/module-quality-2.1-r0.json"),
        "validation_report": {
            "kind": "validation_report",
            "validation_protocol_version": 2,
            "run_id": "report-example",
            "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
            "subject_revision": 0,
            "validator": "module-structure/v2",
            "check_ids": ["module.canonical_markdown"],
            "failures": [],
            "passed": True,
        },
    },
    "cross_review_input": {
        "kind": "cross_review_input",
        "owner_module_id": "2.1",
        "review_focus": [
            "核对其他模块是否改变 2.1 的风险判断、行动前提或联合验收。"
        ],
        "phase": "initial",
        "run_id": "report-example",
        "module_refs": _EXAMPLE_MODULE_REFS,
        "module_revisions": {module_id: 0 for module_id in REPORT_TAXONOMY},
        "modules": _EXAMPLE_MODULES,
        "changed_module_ids": list(REPORT_TAXONOMY),
        "unchanged_module_sha256": {},
        "required_findings": [],
        "revision_responses_by_module": {},
        "local_regression_review_refs": {},
        "prior_synthesis_inputs": [],
    },
    "cross_owner_input": {
        "kind": "cross_owner_input",
        "phase": "initial",
        "run_id": "report-example",
        "review_round": 0,
        "owner_module_id": "2.1",
        "review_focus": ["检查 2.1 与其他模块的供电架构接口。"],
        "owner_subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
        "owner_subject_revision": 0,
        "owner_subject": _EXAMPLE_MODULES["2.1"],
        "related_module_refs": {
            module_id: _EXAMPLE_MODULE_REFS[module_id]
            for module_id in REPORT_TAXONOMY
            if module_id != "2.1"
        },
        "related_module_revisions": {
            module_id: 0 for module_id in REPORT_TAXONOMY if module_id != "2.1"
        },
        "related_module_sha256": {
            module_id: "0" * 64
            for module_id in REPORT_TAXONOMY
            if module_id != "2.1"
        },
        "related_module_views": {
            module_id: {
                "module_id": module_id,
                "revision": 0,
                "subject_ref": _EXAMPLE_MODULE_REFS[module_id],
                "subject_sha256": "0" * 64,
                "submodule_ids": list(REPORT_TAXONOMY[module_id].submodules),
                "claims": [],
                "evidence_ids_by_submodule": {},
                "unresolved_questions": [],
            }
            for module_id in REPORT_TAXONOMY
            if module_id != "2.1"
        },
        "required_findings": [],
        "revision_responses": [],
        "prior_synthesis_inputs": [],
    },
    "final_review_input": {
        "kind": "final_review_input",
        "phase": "initial",
        "run_id": "report-example",
        "cross_decision": _EXAMPLE_CROSS_DECISION,
        "cross_decision_pack_ref": "Work/runs/report-example/reviews/cross-decision-pack.json",
        "residual_risks": [],
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "subject_revision": 0,
        "subject_metadata": _example_final_audit_metadata(),
        "canonical_markdown": "# 示例报告\n\n完整成稿正文。",
        "required_section_ids": list(FINAL_AUDIT_SECTION_IDS),
        "required_findings": [],
        "revision_responses": [],
        "validation_report_ref": ("Work/runs/report-example/reviews/report-integrity-r0.json"),
        "validation_report": {
            "kind": "validation_report",
            "validation_protocol_version": 2,
            "run_id": "report-example",
            "subject_ref": "Work/runs/report-example/validation/report-r0.md",
            "subject_revision": 0,
            "validator": "final-report-structure/v2",
            "check_ids": ["final_report.fixed_sections_and_markdown"],
            "failures": [],
            "passed": True,
        },
    },
    "aggregate_final_review_input": {
        "kind": "aggregate_final_review_input",
        "mode": "aggregate_existing",
        "cross_context": None,
        "phase": "initial",
        "run_id": "report-example",
        "residual_risks": [],
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "subject_revision": 0,
        "subject_metadata": _example_final_audit_metadata(),
        "canonical_markdown": "# 示例报告\n\n完整成稿正文。",
        "required_section_ids": list(FINAL_AUDIT_SECTION_IDS),
        "required_findings": [],
        "revision_responses": [],
        "validation_report_ref": ("Work/runs/report-example/reviews/report-integrity-r0.json"),
        "validation_report": {
            "kind": "validation_report",
            "validation_protocol_version": 2,
            "run_id": "report-example",
            "subject_ref": "Work/runs/report-example/validation/report-r0.md",
            "subject_revision": 0,
            "validator": "final-report-structure/v2",
            "check_ids": ["final_report.fixed_sections_and_markdown"],
            "failures": [],
            "passed": True,
        },
    },
    "module_revision_input": {
        "kind": "module_revision_input",
        "run_id": "report-example",
        "module_id": "2.1",
        "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
        "subject": {
            **_EXAMPLE_MODULES["2.1"],
            "submodule_narratives": {
                next(iter(REPORT_TAXONOMY["2.1"].submodules)): (
                    _EXAMPLE_MODULES["2.1"]["submodule_narratives"][
                        next(iter(REPORT_TAXONOMY["2.1"].submodules))
                    ]
                )
            },
            "evidence_ids_by_submodule": {
                next(iter(REPORT_TAXONOMY["2.1"].submodules)): (
                    _EXAMPLE_MODULES["2.1"]["evidence_ids_by_submodule"][
                        next(iter(REPORT_TAXONOMY["2.1"].submodules))
                    ]
                )
            },
        },
        "target_submodule_ids": [next(iter(REPORT_TAXONOMY["2.1"].submodules))],
        "module_findings": [_example_module_finding()],
        "cross_findings": [],
        "requested_changes": [],
        "validation_report_ref": None,
        "validation_report": None,
    },
    "chief_revision_input": {
        "kind": "chief_revision_input",
        "run_id": "report-example",
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "target_section_bodies": {
            "3.2": _example_final_audit_subject()["improvement_action_plan"],
        },
        "consistency_context": {
            "1.2": _example_final_audit_subject()["findings_overview"],
            "3.1.1": _example_final_audit_subject()["risk_panorama"],
            "3.1.3": _example_final_audit_subject()["data_gap_analysis"],
        },
        "revision": 1,
        "target_section_ids": ["3.2"],
        "findings": [_example_final_finding()],
    },
    "chief_chapter_lane_input": {
        "kind": "chief_chapter_lane_input",
        "phase": "initial",
        "run_id": "report-example",
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "chapter_id": "1",
        "section_ids": list(CHAPTER1_SECTION_IDS),
        "section_bodies": {
            section_id: f"章节 {section_id} 的当前正文。"
            for section_id in CHAPTER1_SECTION_IDS
        },
        "source_context": {"cross_summary": "仅供 Chapter 1 lane 使用的证据边界摘要。"},
        "source_refs": ["Work/runs/report-example/preparation/evidence.jsonl"],
        "assigned_findings": [],
        "revision": 0,
    },
    "final_chapter_lane_input": {
        "kind": "final_chapter_lane_input",
        "phase": "initial",
        "run_id": "report-example",
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "chapter_id": "3",
        "review_focus": ["检查风险全景、数据缺口与行动依赖是否闭合。"],
        "section_ids": list(CHAPTER3_SECTION_IDS),
        "section_bodies": {
            section_id: f"章节 {section_id} 的当前正文。"
            for section_id in CHAPTER3_SECTION_IDS
        },
        "required_findings": [],
        "revision_responses": [],
        "revision": 0,
    },
    "chief_editor_input": {
        "kind": "chief_editor_input",
        "run_id": "report-example",
        "cross_decision": _EXAMPLE_CROSS_DECISION,
        "cross_decision_pack_ref": "Work/runs/report-example/reviews/cross-decision-pack.json",
        "approved_module_markers": _EXAMPLE_MARKERS,
        "modules": _EXAMPLE_MODULES,
        "cross_review_completion_ref": ("Work/runs/report-example/reviews/cross-completion.json"),
        "special_topic_plan": _example_special_topic_plan(),
    },
    "aggregate_editor_input": {
        "kind": "aggregate_editor_input",
        "mode": "aggregate_existing",
        "run_id": "report-example",
        "cross_context": None,
        "source_format": "structured_module",
        "approved_module_markers": _EXAMPLE_MARKERS,
        "special_topic_plan": _example_special_topic_plan(),
        "structured_modules": _EXAMPLE_MODULES,
        "markdown_modules": {},
    },
    "workflow_exception_input": {
        "kind": "workflow_exception_input",
        "run_id": "report-example",
        "scope": "cross",
        "trigger": "reviewer_escalation",
        "finding_ids": ["X-001"],
        "subject_refs": list(_EXAMPLE_MODULE_REFS.values()),
        "finding_refs": ["Work/runs/report-example/reviews/cross-findings-r0.json"],
        "verdicts": [
            {
                "finding_id": "X-001",
                "verdict": "escalate",
                "reason": "作者对依赖关系提出有依据的异议，需要 Main 判断是否涉及用户决策。",
                "evidence_refs": [],
            }
        ],
        "revision_responses": [
            {
                "finding_id": "X-001",
                "action": "disputed",
                "summary": "当前证据显示两个行动并无先后依赖，因此未修改正文并请求复核。",
                "changed_target_ids": [],
            }
        ],
    },
}


def _enrich_properties(node: Any) -> None:
    if not isinstance(node, dict):
        return
    for name, prop in node.get("properties", {}).items():
        if isinstance(prop, dict) and not prop.get("description"):
            guidance = FIELD_GUIDANCE.get(name)
            if guidance:
                prop["description"] = guidance
    for value in node.get("$defs", {}).values():
        _enrich_properties(value)


def input_contract_schema(kind: str) -> dict[str, Any]:
    try:
        model = INPUT_CONTRACT_TYPES[kind]
    except KeyError as exc:
        raise KeyError(f"unknown input contract kind: {kind}") from exc
    schema = deepcopy(TypeAdapter(model).json_schema())
    schema["description"] = INPUT_CONTRACT_SUMMARIES[kind]
    schema["examples"] = [deepcopy(INPUT_CONTRACT_EXAMPLES[kind])]
    _enrich_properties(schema)
    return schema


def render_input_contract(kind: str) -> str:
    schema = input_contract_schema(kind)
    lines = [
        f"input_contract_kind: {kind}",
        f"purpose: {schema['description']}",
        "The artifact named by input_contract_ref must validate against this schema.",
    ]
    for name, prop in schema.get("properties", {}).items():
        requirement = "required" if name in schema.get("required", []) else "optional"
        lines.append(f"- {name} ({requirement}): {str(prop.get('description') or '').strip()}")
    lines.extend(
        [
            "valid_example:",
            json.dumps(
                schema["examples"][0],
                ensure_ascii=False,
                separators=(",", ":"),
            ),
        ]
    )
    return "\n".join(lines)


def write_contract_manifest() -> str:
    """Stable JSON summary useful in tests and run handoffs."""

    return json.dumps(
        {
            kind: {
                "purpose": INPUT_CONTRACT_SUMMARIES[kind],
                "schema": input_contract_schema(kind),
            }
            for kind in INPUT_CONTRACT_TYPES
        },
        ensure_ascii=False,
        sort_keys=True,
    )
