"""Typed, model-visible stage inputs for the reporting review lifecycle."""

from __future__ import annotations

import json
import re
from copy import deepcopy
from typing import Any, Literal

from pydantic import Field, TypeAdapter, model_validator

from .agentic_models import (
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
)
from .submission_contracts import FIELD_GUIDANCE
from .taxonomy import REPORT_TAXONOMY


class ModuleContentView(StrictModel):
    """Model-visible module content without runtime-only identifiers."""

    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    revision: int = Field(ge=0)
    submodule_narratives: dict[str, str]
    evidence_ids_by_submodule: dict[str, list[str]] = Field(
        description="Registered E-* evidence ids already bound to each visible submodule."
    )
    unresolved_questions: list[str] = Field(default_factory=list)


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
        max_length=4_000,
        description="Bounded normalized evidence content needed to audit the cited prose.",
    )


def strip_runtime_claim_markers(text: str) -> str:
    return re.sub(r"\s*\[\[CLAIM:C-[^\]\s]+\]\]", "", text)


def module_content_view(subject: ModuleSubmission) -> ModuleContentView:
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
    }
    narratives = {
        submodule_id: strip_runtime_claim_markers(narrative).rstrip()
        for submodule_id, narrative in subject.submodule_narratives.items()
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
                source_id
                for source_id in table.source_ids
                if source_id.startswith("E-")
            ],
        }
        for table in subject.tables
    ]
    return EditedReportSubmissionInput.model_validate(payload)


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
    required_part_ids: list[
        Literal["skill", "analysis", "synthesis", "visual", "rubric"]
    ] = Field(
        description="Exact durable result parts required before final submission."
    )

    @model_validator(mode="after")
    def exact_parts(self) -> "TemplateDistillationInput":
        expected = {"skill", "analysis", "synthesis", "visual", "rubric"}
        if set(self.required_part_ids) != expected or len(self.required_part_ids) != 5:
            raise ValueError("template distillation requires exactly five named parts")
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
        description="Module-specific bounded knowledge context.",
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
        if (
            set(self.required_submodule_ids) != expected
            or len(self.required_submodule_ids) != len(expected)
        ):
            raise ValueError(
                "module authoring input requires every fixed submodule exactly once"
            )
        if not set(self.saved_part_ids).issubset(expected):
            raise ValueError("existing result parts lie outside the module scope")
        if not set(self.rewrite_part_ids).issubset(expected):
            raise ValueError("correction rewrite parts lie outside the module scope")
        return self


class ModuleReviewInput(StrictModel):
    kind: Literal["module_review_input"] = "module_review_input"
    phase: Literal["initial", "recheck"] = Field(
        description="initial creates findings; recheck returns verdicts for required findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Only module whose local content is in review."
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
    validation_report_ref: str = Field(
        min_length=1,
        description=(
            "Independent machine validation artifact. It may reject structure "
            "but never decides semantic findings."
        ),
    )
    validation_report: "ValidationReport" = Field(
        description=(
            "Exact typed machine validation result for this subject, including "
            "non-binding observations."
        )
    )

    @model_validator(mode="after")
    def phase_fields_match(self) -> "ModuleReviewInput":
        if self.subject.module_id != self.module_id:
            raise ValueError("module review subject belongs to a different module")
        if self.subject.revision != self.subject_revision:
            raise ValueError("module review subject_revision does not match subject")
        required_evidence_ids = {
            evidence_id
            for values in self.subject.evidence_ids_by_submodule.values()
            for evidence_id in values
        }
        supplied_evidence_ids = [item.evidence_id for item in self.evidence]
        if (
            len(supplied_evidence_ids) != len(set(supplied_evidence_ids))
            or set(supplied_evidence_ids) != required_evidence_ids
        ):
            raise ValueError(
                "module review evidence must contain every bound E-* id exactly once"
            )
        if not self.validation_report.passed:
            raise ValueError(
                "module semantic review cannot start from failed machine validation"
            )
        if self.phase == "initial" and (
            self.required_findings or self.revision_responses
        ):
            raise ValueError("initial module review cannot contain prior findings or responses")
        if self.phase == "recheck":
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError(
                    "module recheck requires exactly one author response per finding"
                )
        return self


class CrossReviewInput(StrictModel):
    kind: Literal["cross_review_input"] = "cross_review_input"
    phase: Literal["initial", "recheck"] = Field(
        description="initial creates Cross findings; recheck closes required Cross findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    module_refs: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(description="Workflow-owned refs for the exact five reviewed module subjects.")
    module_revisions: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], int
    ] = Field(description="Workflow-owned revision numbers corresponding to module_refs.")
    modules: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView
    ] = Field(
        description=(
            "Initial: complete five-module subjects. Recheck: only modules changed by "
            "the current Cross revision wave."
        )
    )
    changed_module_ids: list[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    ] = Field(
        description="Initial: all five modules. Recheck: only current-wave revised owners."
    )
    unchanged_module_sha256: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(
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
    local_regression_review_refs: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(
        default_factory=dict,
        description=(
            "Independent module-local regression completion refs. They do not close Cross findings."
        ),
    )
    prior_synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description="Previously supported synthesis inputs available during recheck.",
    )
    machine_validation_refs: list[str] = Field(
        default_factory=list,
        description="Independent explicit-predicate validation reports for revised modules.",
    )
    machine_validation_reports: list["ValidationReport"] = Field(
        default_factory=list,
        description="Typed reports corresponding one-for-one to machine_validation_refs.",
    )

    @model_validator(mode="after")
    def five_subjects_and_phase_fields_match(self) -> "CrossReviewInput":
        expected = {"2.1", "2.2", "2.3", "2.4", "2.5"}
        if (
            set(self.module_refs) != expected
            or set(self.module_revisions) != expected
        ):
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
            or self.machine_validation_refs
            or self.machine_validation_reports
            or self.unchanged_module_sha256
        ):
            raise ValueError("initial cross review cannot contain recheck state")
        if self.phase == "initial" and (
            set(self.modules) != expected or changed != expected
        ):
            raise ValueError("initial cross review requires all five complete modules")
        if self.phase == "recheck":
            if not changed or set(self.modules) != changed:
                raise ValueError(
                    "cross recheck full subjects must equal changed_module_ids"
                )
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
            if not required or responses != required:
                raise ValueError("cross recheck requires one owner response per finding")
            if len(self.machine_validation_refs) != len(
                self.machine_validation_reports
            ):
                raise ValueError(
                    "cross recheck validation refs and reports must correspond"
                )
            if any(not report.passed for report in self.machine_validation_reports):
                raise ValueError(
                    "cross recheck cannot start from failed machine validation"
                )
        return self


class FinalReviewInput(StrictModel):
    kind: Literal["final_review_input"] = "final_review_input"
    phase: Literal["initial", "recheck"] = Field(
        description="initial creates final findings; recheck closes required final findings."
    )
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    subject_ref: str = Field(
        min_length=1,
        description="Exact current edited-report artifact reviewed in this pass.",
    )
    subject_revision: int = Field(
        ge=0,
        description="Workflow-owned chief-editor revision number.",
    )
    subject: EditedReportSubmissionInput = Field(
        description="Complete current final-report content without runtime-only bindings."
    )
    canonical_markdown: str = Field(
        min_length=1,
        description="Deterministically rendered Markdown corresponding to subject.",
    )
    required_section_ids: list[str] = Field(
        min_length=1,
        description="Fixed final-report sections that the reviewer must cover.",
    )
    required_findings: list[FinalReviewFinding] = Field(
        default_factory=list,
        description="Immutable prior final findings requiring one verdict each on recheck.",
    )
    revision_responses: list[RevisionResponse] = Field(
        default_factory=list,
        description="Chief-editor responses to required_findings.",
    )
    cross_synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description="Cross-reviewer-supported synthesis inputs available to final review.",
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
        if not self.validation_report.passed:
            raise ValueError(
                "final semantic review cannot start from failed machine validation"
            )
        if self.phase == "initial" and (
            self.required_findings or self.revision_responses
        ):
            raise ValueError("initial final review cannot contain prior findings or responses")
        if self.phase == "recheck":
            required = {finding.id for finding in self.required_findings}
            responses = {response.finding_id for response in self.revision_responses}
            if not required or responses != required:
                raise ValueError("final recheck requires one chief response per finding")
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
        description="Complete module prose and per-submodule E evidence baseline."
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
        description="Optional failed machine-validation artifact from the prior patch.",
    )
    validation_report: "ValidationReport | None" = Field(
        default=None,
        description=(
            "Exact independent predicate failures to correct; these are not reviewer findings."
        ),
    )

    @model_validator(mode="after")
    def assigned_findings_are_nonempty_and_in_scope(self) -> "ModuleRevisionInput":
        if self.subject.module_id != self.module_id:
            raise ValueError("module revision baseline belongs to a different module")
        if (
            not self.module_findings
            and not self.cross_findings
            and not self.requested_changes
        ):
            raise ValueError(
                "module revision input requires a finding or requested change"
            )
        if bool(self.validation_report_ref) != bool(self.validation_report):
            raise ValueError(
                "validation_report_ref and validation_report must be provided together"
            )
        if self.validation_report and self.validation_report.passed:
            raise ValueError("a passed validation report cannot trigger another revision")
        allowed = set(self.target_submodule_ids)
        for finding in self.module_findings:
            if finding.target_submodule_id not in allowed:
                raise ValueError("module finding lies outside revision targets")
        for finding in self.cross_findings:
            if finding.owner_module_id != self.module_id:
                raise ValueError("cross finding belongs to another owner module")
            if not set(finding.target_submodule_ids).issubset(allowed):
                raise ValueError("cross finding lies outside revision targets")
        for change in self.requested_changes:
            if not set(change.target_submodule_ids).issubset(allowed):
                raise ValueError("requested change lies outside revision targets")
        return self


class ChiefRevisionInput(StrictModel):
    kind: Literal["chief_revision_input"] = "chief_revision_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    subject_ref: str = Field(min_length=1, description="Exact edited report being revised.")
    subject: EditedReportSubmissionInput = Field(
        description="Complete chief-editor content baseline without runtime-only bindings."
    )
    target_section_ids: list[str] = Field(
        min_length=1,
        description="Only final-report sections the chief editor may change.",
    )
    findings: list[FinalReviewFinding] = Field(
        min_length=1,
        description="Immutable final-review findings assigned to the chief editor.",
    )


class ChiefEditorInput(StrictModel):
    kind: Literal["chief_editor_input"] = "chief_editor_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    approved_module_markers: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(
        description="Exact marker tokens the chief submits for deterministic prose insertion."
    )
    modules: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView
    ] = Field(description="Five module subjects closed by module review.")
    cross_synthesis_inputs: list[CrossSynthesisInput] = Field(
        description="Cross-reviewer-supported relationships available for chief synthesis."
    )
    cross_review_completion_ref: str = Field(
        min_length=1,
        description="Current-run record proving the Cross lifecycle completed.",
    )

    @model_validator(mode="after")
    def five_modules_and_markers_match(self) -> "ChiefEditorInput":
        expected = {"2.1", "2.2", "2.3", "2.4", "2.5"}
        if set(self.modules) != expected or set(self.approved_module_markers) != expected:
            raise ValueError("chief editor input requires exactly five modules and markers")
        for module_id, subject in self.modules.items():
            if subject.module_id != module_id:
                raise ValueError("chief editor module binding is inconsistent")
            if self.approved_module_markers[module_id] != (
                f"[[APPROVED_MODULE:{module_id}]]"
            ):
                raise ValueError("chief editor marker does not match its module")
        return self


class AggregateEditorInput(StrictModel):
    kind: Literal["aggregate_editor_input"] = "aggregate_editor_input"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    source_format: Literal["structured_module", "markdown"] = Field(
        description="Whether modules are typed subjects or validated standalone Markdown."
    )
    approved_module_markers: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(
        description="Exact marker tokens used for deterministic module-prose insertion."
    )
    structured_modules: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], ModuleContentView
    ] = Field(
        default_factory=dict,
        description="Exactly five typed modules when source_format=structured_module.",
    )
    markdown_modules: dict[
        Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str
    ] = Field(
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
            raise ValueError(
                "markdown aggregate input requires exactly five markdown_modules"
            )
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
            raise ValueError("workflow exception requires one associated author response per finding")
        if self.trigger == "reviewer_escalation":
            verdict_ids = {
                verdict.finding_id
                for verdict in self.verdicts
                if verdict.verdict == "escalate"
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
    check_id: str = Field(min_length=1, description="Stable deterministic check id.")
    finding_id: str | None = Field(
        default=None,
        description="Finding whose explicit machine predicate produced this failure.",
    )
    target_path: str = Field(
        min_length=1,
        description="Exact structured path inspected by the deterministic check.",
    )
    message: str = Field(
        min_length=1,
        description="Observed predicate mismatch without semantic reviewer judgment.",
    )


class ValidationReport(StrictModel):
    kind: Literal["validation_report"] = "validation_report"
    run_id: str = Field(min_length=1, description="Immutable current report run id.")
    subject_ref: str = Field(min_length=1, description="Exact validated subject artifact.")
    validator: str = Field(
        min_length=1,
        description="Deterministic validator implementation and version identifier.",
    )
    check_ids: list[str] = Field(
        description="All explicit deterministic checks executed for this subject."
    )
    failures: list[ValidationFailure] = Field(
        default_factory=list,
        description="Predicate failures; empty means machine validation passed.",
    )
    observations: list[str] = Field(
        default_factory=list,
        description=(
            "Non-binding deterministic signals for reviewer attention; they never "
            "change passed and never act as semantic findings or verdicts."
        ),
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
    verdict_refs: list[str] = Field(
        description="Reviewer verdict artifacts closing every finding."
    )
    resolved_finding_ids: list[str] = Field(
        description="Every finding id closed by reviewer verdict or empty initial findings."
    )
    artifact_sha256: dict[str, str] = Field(
        min_length=1,
        description=(
            "SHA-256 for every subject, finding, and verdict ref so completion cannot "
            "silently survive later artifact mutation."
        ),
    )

    @model_validator(mode="after")
    def hashes_cover_every_reference(self) -> "ReviewCompletionRecord":
        expected = set(self.subject_refs) | set(self.finding_refs) | set(self.verdict_refs)
        if set(self.artifact_sha256) != expected:
            raise ValueError("review completion hashes must cover every referenced artifact")
        if any(
            not re.fullmatch(r"[0-9a-f]{64}", value)
            for value in self.artifact_sha256.values()
        ):
            raise ValueError("review completion hashes must be SHA-256 hex")
        return self
INPUT_CONTRACT_TYPES = {
    "template_distillation_input": TemplateDistillationInput,
    "module_authoring_input": ModuleAuthoringInput,
    "module_review_input": ModuleReviewInput,
    "cross_review_input": CrossReviewInput,
    "final_review_input": FinalReviewInput,
    "module_revision_input": ModuleRevisionInput,
    "chief_revision_input": ChiefRevisionInput,
    "chief_editor_input": ChiefEditorInput,
    "aggregate_editor_input": AggregateEditorInput,
    "workflow_exception_input": WorkflowExceptionInput,
}

INPUT_CONTRACT_SUMMARIES = {
    "template_distillation_input": "One exact template snapshot and five required durable output parts.",
    "module_authoring_input": "One fixed module scope with role-labelled current-run evidence inputs.",
    "module_review_input": "One exact module subject plus phase-specific immutable review state.",
    "cross_review_input": "Five exact module subjects plus phase-specific Cross closure state.",
    "final_review_input": "One exact edited report plus phase-specific final-review state.",
    "module_revision_input": "One exact module baseline and only the findings assigned to its author.",
    "chief_revision_input": "One exact edited-report baseline and immutable final findings.",
    "chief_editor_input": "Five module-review-complete subjects and Cross-supported synthesis inputs.",
    "aggregate_editor_input": "Five validated existing module bodies with an explicit source format.",
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
        "id": "M-2.1-001",
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
        "machine_checks": [],
    }


def _example_final_finding() -> dict[str, Any]:
    return {
        "id": "F-001",
        "target_section_ids": ["3.1.3"],
        "category": "synthesis",
        "impact": "blocking",
        "observation": "跨领域关联风险只罗列模块名称，没有形成已支持的因果或依赖链。",
        "evidence_refs": [
            "Work/runs/report-example/edited-revisions/chief-r0.json"
        ],
        "required_change": "在 3.1.3 中形成有证据边界的因果链、行动依赖和联合验证。",
        "reviewer_checks": ["综合内容不改变模块事实且形成可执行的联合判断"],
    }


def _example_edited_report() -> dict[str, Any]:
    return {
        "kind": "edited_report_submission",
        "title": "示例报告",
        "assessment_background": "说明评估范围、证据基础和适用边界。",
        "findings_overview": "归纳主要发现及其管理含义。",
        "regional_executive_summary": "按真实责任边界归纳行动。",
        "module_narratives": {
            module_id: f"[[APPROVED_MODULE:{module_id}]]"
            for module_id in REPORT_TAXONOMY
        },
        "cross_module_analysis": "说明已支持的跨模块关系和联合验证。",
        "risk_panorama": "按共同根因和传播能力组织风险。",
        "dimension_risk_analysis": "比较五个维度的风险和管理含义。",
        "data_gap_analysis": "说明缺口、判断影响和补证优先级。",
        "improvement_action_plan": "列出责任、依赖、行动、验收和剩余风险。",
        "new_factory_planning": "新建规划专项分析。",
        "capacity_expansion_plan": "增容决策专项分析。",
        "daily_power_management": "日常用电管理专项分析。",
        "emergency_compliance_management": "应急与合规专项分析。",
        "tables": [],
        "photo_ids": [],
        "unresolved_editorial_issues": [],
        "revision_responses": [],
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
_EXAMPLE_MARKERS = {
    module_id: f"[[APPROVED_MODULE:{module_id}]]"
    for module_id in REPORT_TAXONOMY
}

INPUT_CONTRACT_EXAMPLES: dict[str, dict[str, Any]] = {
    "template_distillation_input": {
        "kind": "template_distillation_input",
        "run_id": "report-example",
        "template_ref": "Work/runs/report-example/templates/template-for-skill.docx",
        "inspect_max_chars": 100000,
        "required_part_ids": ["skill", "analysis", "synthesis", "visual", "rubric"],
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
        "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
        "subject_revision": 0,
        "subject": _EXAMPLE_MODULES["2.1"],
        "evidence": [],
        "required_submodule_ids": list(REPORT_TAXONOMY["2.1"].submodules),
        "required_findings": [],
        "revision_responses": [],
        "validation_report_ref": (
            "Work/runs/report-example/reviews/module-quality-2.1-r0.json"
        ),
        "validation_report": {
            "kind": "validation_report",
            "run_id": "report-example",
            "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
            "validator": "module-structure/v1",
            "check_ids": ["module.canonical_markdown"],
            "failures": [],
            "observations": [],
            "passed": True,
        },
    },
    "cross_review_input": {
        "kind": "cross_review_input",
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
        "machine_validation_refs": [],
        "machine_validation_reports": [],
    },
    "final_review_input": {
        "kind": "final_review_input",
        "phase": "initial",
        "run_id": "report-example",
        "subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "subject_revision": 0,
        "subject": _example_edited_report(),
        "canonical_markdown": "# 示例报告\n\n完整成稿正文。",
        "required_section_ids": [
            "1.1",
            "1.2",
            "1.3",
            "2.1",
            "2.2",
            "2.3",
            "2.4",
            "2.5",
            "3.1.1",
            "3.1.2",
            "3.1.3",
            "3.1.4",
            "3.2",
            "4.1",
            "4.2",
            "4.3",
            "4.4",
        ],
        "required_findings": [],
        "revision_responses": [],
        "cross_synthesis_inputs": [],
        "validation_report_ref": (
            "Work/runs/report-example/reviews/report-integrity-r0.json"
        ),
        "validation_report": {
            "kind": "validation_report",
            "run_id": "report-example",
            "subject_ref": "Work/runs/report-example/validation/report-r0.md",
            "validator": "final-report-structure/v1",
            "check_ids": ["final_report.fixed_sections_and_markdown"],
            "failures": [],
            "observations": [],
            "passed": True,
        },
    },
    "module_revision_input": {
        "kind": "module_revision_input",
        "run_id": "report-example",
        "module_id": "2.1",
        "subject_ref": _EXAMPLE_MODULE_REFS["2.1"],
        "subject": _EXAMPLE_MODULES["2.1"],
        "target_submodule_ids": [
            next(iter(REPORT_TAXONOMY["2.1"].submodules))
        ],
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
        "subject": _example_edited_report(),
        "target_section_ids": ["3.1.3"],
        "findings": [_example_final_finding()],
    },
    "chief_editor_input": {
        "kind": "chief_editor_input",
        "run_id": "report-example",
        "approved_module_markers": _EXAMPLE_MARKERS,
        "modules": _EXAMPLE_MODULES,
        "cross_synthesis_inputs": [],
        "cross_review_completion_ref": (
            "Work/runs/report-example/reviews/cross-completion.json"
        ),
    },
    "aggregate_editor_input": {
        "kind": "aggregate_editor_input",
        "run_id": "report-example",
        "source_format": "structured_module",
        "approved_module_markers": _EXAMPLE_MARKERS,
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
        "finding_refs": [
            "Work/runs/report-example/reviews/cross-findings-r0.json"
        ],
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
        lines.append(
            f"- {name} ({requirement}): {str(prop.get('description') or '').strip()}"
        )
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
