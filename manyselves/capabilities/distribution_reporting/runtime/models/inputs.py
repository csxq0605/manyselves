"""Capability-owned, model-visible inputs for the reporting review lifecycle."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import Field, model_validator

from manyselves.capabilities.distribution_reporting.domain.taxonomy import (
    REPORT_TAXONOMY,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    FINAL_AUDIT_SECTION_IDS,
    ClaimRecord,
    CrossDecisionPack,
    CrossReviewFinding,
    CrossSynthesisInput,
    FinalReviewFinding,
    ModuleReviewFinding,
    RevisionResponse,
    StrictModel,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    SpecialTopicPlan,
)

REPORT_INSTRUCTION_DESCRIPTION = (
    "Applicable user requirements for this report; later explicit changes override earlier "
    "conflicting requirements. Preserve requested content, evidence boundaries and valid "
    "material unrelated to the assigned finding, including within a changed subsection. "
    "Review must flag omissions or regressions of these requirements."
)


class ModuleContentView(StrictModel):
    """Model-visible module content without runtime-only identifiers."""

    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    revision: int = Field(ge=0)
    submodule_narratives: dict[str, str]
    evidence_ids_by_submodule: dict[str, list[str]] = Field(
        description="Registered E-* evidence ids already bound to each visible submodule."
    )
    unresolved_questions: list[str] = Field(default_factory=list)
    assessments: dict[str, dict] = Field(
        default_factory=dict,
        description="Section-scoped author judgments and runtime observation coverage. Missing records/anchors are explicit; presence does not certify correctness. Review records against evidence, rules and prose.",
    )


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
    deferred_advisories: list[CrossReviewFinding] = Field(default_factory=list)
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
    bound_submodule_ids: list[str] | None = Field(
        default=None,
        description=(
            "Current reviewed submodules explicitly binding this exact E ID. [] means no "
            "current binding; null means not computed for this persisted input. A binding "
            "does not prove the observation was judged or has a corresponding action."
        ),
    )
    shared: str | None = Field(
        default=None,
        description=(
            "Set when this E ID is one copy of a source observation routed to "
            "several subsections: '<observation>；负责=<owner>；另见=<other copies>'. "
            "Each place may judge from its own angle; the owner decides same-angle "
            "conflicts, and facts' calculation limits hold everywhere."
        ),
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
    origin: Literal["unspecified", "runtime_part_binding"] = Field(
        default="unspecified",
        description="A runtime_part_binding carries complete section prose and sources, not independently classified facts/judgments/actions.",
    )
    confidence: float | None = Field(
        ge=0.0,
        le=1.0,
        description="Stored statement confidence; null for runtime bindings whose source parsing confidence is not a judgment probability.",
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
    changed_assessment_submodules: list[str] = Field(default_factory=list, description="Judgment or expected-observation scope changed, even when section prose did not.")
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


class FinalAuditMetadataView(StrictModel):
    """Non-prose final-audit metadata not represented in canonical Markdown."""

    photo_ids: list[str] = Field(default_factory=list)
    unresolved_editorial_issues: list[str] = Field(default_factory=list)


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
    report_instruction: str = Field(default="", description=REPORT_INSTRUCTION_DESCRIPTION)
    input_changes: RevisionInputChanges | None = Field(
        default=None,
        description="Current input changes. Check revised claims against current facts; superseded IDs are historical evidence only.",
    )
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
            "the last paid review. Current statements also include unchanged finding-scope text."
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
            "inline bounded slice was made. Each task carries its own scoped body; "
            "persistent reviewer identity does not imply visible earlier context."
        ),
    )
    knowledge_context: str = Field(
        default="",
        description=(
            "Selected taxonomy sections from the immutable Knowledge snapshot for this "
            "task's scope, including recheck and local regression."
        ),
    )
    evidence: list[ReviewEvidenceExcerpt] = Field(
        description=(
            "Bounded packet covering cited E-* ids and uncited project facts in review scope. "
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
            or not required_evidence_ids.issubset(supplied_evidence_ids)
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
            if not changed_narratives.issubset(self.subject.submodule_narratives):
                raise ValueError("module recheck subject must contain every changed narrative")
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
            if not (
                changed_statements <= visible_changed_statements
                <= changed_statements | set(self.unchanged_statement_sha256)
            ):
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
                    set(self.subject.submodule_narratives)
                    | {statement.submodule_id for statement in self.claim_statements}
                )
            }
            if self.subject.evidence_ids_by_submodule != expected_subject_evidence:
                raise ValueError(
                    "module recheck subject evidence must describe current visible semantics"
                )
        return self

    @property
    def finding_id_prefix(self) -> str:
        return f"M-{self.module_id}-{self.lifecycle_id}-r{self.review_round}-"


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


class RevisionInputChanges(StrictModel):
    """Parsed current facts and retired evidence identities, never user-authored claims."""

    files: dict[str, list[str]] = Field(default_factory=dict)
    superseded_evidence_ids: list[str] = Field(default_factory=list)
    current_evidence: list[ReviewEvidenceExcerpt] = Field(default_factory=list)


class CrossOwnerInput(StrictModel):
    """Typed input for one fixed Cross-owner reviewer.

    The owner receives its complete module view.  Every other module is present
    only as a compact, revision-bound relation view; those modules are read-only and
    cannot become revision targets for this lane.
    """

    kind: Literal["cross_owner_input"] = "cross_owner_input"
    report_instruction: str = Field(default="", description=REPORT_INSTRUCTION_DESCRIPTION)
    input_changes: RevisionInputChanges | None = Field(
        default=None,
        description="Current parsed input changes. Check linked prose against these facts; superseded E IDs describe the old baseline only.",
    )
    phase: Literal["initial", "recheck", "repair"] = Field(
        description="Initial owner review, bound recheck, or author repair projected from a joint decision."
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
    owner_scope_submodule_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Bounded owner scope for this Cross execution lane, derived from the run taxonomy."
        ),
    )
    joint_dossiers: list[dict] = Field(
        default_factory=list,
        description=(
            "Per-object dossiers this owner takes part in: the object's facts and every "
            "module's judgments with prose excerpts. When present, review these objects "
            "only; owner scope and related views are cut to them, and related full text "
            "stays readable by reference."
        ),
    )
    related_module_refs: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(description="Immutable refs for the other four read-only module subjects.")
    related_module_revisions: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], int
    ] = Field(description="Revisions for the other four read-only module subjects.")
    related_module_views: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], CrossOwnerRelatedModuleView
    ] = Field(description="Compact, revision-bound views for the other four modules.")
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
        visible = list(self.owner_subject.submodule_narratives)
        if not self.owner_scope_submodule_ids:
            self.owner_scope_submodule_ids = visible
        if visible != self.owner_scope_submodule_ids:
            raise ValueError("Cross owner subject must contain exactly its bounded lane scope")
        if not set(visible).issubset(REPORT_TAXONOMY[self.owner_module_id].submodules):
            raise ValueError("Cross owner lane scope lies outside the run taxonomy")
        if set(self.related_module_refs) != expected_related:
            raise ValueError("Cross owner input must include exactly the other four modules")
        if set(self.related_module_revisions) != expected_related:
            raise ValueError("Cross owner relation revisions must cover the other four modules")
        if set(self.related_module_views) != expected_related:
            raise ValueError("Cross owner compact relation views must cover the other four modules")
        for module_id in expected_related:
            view = self.related_module_views[module_id]
            if (
                view.module_id != module_id
                or view.revision != self.related_module_revisions[module_id]
                or view.subject_ref != self.related_module_refs[module_id]
            ):
                raise ValueError("Cross owner relation view binding is inconsistent")
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
        elif self.phase == "recheck":
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
    report_instruction: str = Field(default="", description=REPORT_INSTRUCTION_DESCRIPTION)
    input_changes: RevisionInputChanges | None = Field(
        default=None,
        description="Current parsed input changes. Use applicable new evidence for the assigned edits and replace obsolete evidence bindings.",
    )
    evidence: list[ReviewEvidenceExcerpt] = Field(
        default_factory=list,
        description="Current project facts applicable to assigned subsections, including new or replaced inputs.",
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Responsibility module being revised."
    )
    subject_ref: str = Field(min_length=1, description="Exact module revision being patched.")
    subject: ModuleContentView = Field(
        description=(
            "Only assigned submodule prose and per-submodule E evidence baseline. "
            "Unassigned content remains workflow-owned and is not resent. Preserve valid "
            "content within each assigned subsection unless the requested change or current "
            "evidence specifically supersedes it; do not rewrite away unrelated material."
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
    verdict_refs: list[str] = Field(description="Reviewer verdict artifacts closing corrective findings.")
    resolved_finding_ids: list[str] = Field(
        description="Finding ids actually closed by reviewer verdict, excluding deferred advice."
    )
    deferred_advisory_ids: list[str] = Field(
        default_factory=list,
        description="Module/Cross v3 optional advice retained without revision or a resolved verdict.",
    )
    open_at_round_limit_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Corrective findings still open when the bounded rework rounds ran out. "
            "They are carried downstream as open issues, never reported as resolved."
        ),
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

