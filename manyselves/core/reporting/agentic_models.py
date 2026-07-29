import re
from enum import StrEnum
from typing import Annotated, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from .models import (
    REPORT_FINAL_AUDIT_SECTION_IDS,
    REPORT_FINAL_SECTION_IDS,
    CoverageMatrix,
    EvidenceItem,
    PhotoAsset,
    SpecialTopicPlan,
)
from .taxonomy import REPORT_TAXONOMY, compose_module_markdown, resolve_submodule


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceKind(StrEnum):
    PROJECT_EVIDENCE = "project_evidence"
    LOCAL_REFERENCE = "local_reference"
    WEB = "web"


SOURCE_PREFIX = {
    SourceKind.PROJECT_EVIDENCE: "E-",
    SourceKind.LOCAL_REFERENCE: "R-",
    SourceKind.WEB: "W-",
}

SynthesisTableType = Literal[
    "risk_cluster_matrix",
    "action_dependency_matrix",
    "joint_acceptance_matrix",
]


class TaskEnvelope(StrictModel):
    task_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    agent_id: str = Field(min_length=1)
    objective: str = Field(min_length=1)
    input_refs: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    allowed_outputs: list[str] = Field(default_factory=list)
    allowed_tools: list[str] = Field(default_factory=list)
    revision: int = Field(default=0, ge=0)
    prior_result_ref: str | None = None
    context_summary_refs: list[str] = Field(default_factory=list)
    inline_context: str | None = Field(default=None, max_length=100_000)
    artifact_delivery_modes: dict[
        str, Literal["inline", "reference", "hash_retained"]
    ] = Field(
        default_factory=dict,
        description=(
            "One delivery mode per declared artifact. Inline artifacts are embedded in the "
            "task and cannot be reopened; reference artifacts are readable; hash_retained "
            "artifacts identify immutable prior state whose full finding/response contract "
            "and current content hashes are carried by the inline input; correctness must not "
            "depend on process-local conversation memory."
        ),
    )
    target_submodule_ids: list[str] = Field(default_factory=list)
    input_contract_kind: (
        Literal[
            "template_distillation_input",
            "module_authoring_input",
            "module_review_input",
            "cross_review_input",
            "final_review_input",
            "module_revision_input",
            "chief_revision_input",
            "chief_editor_input",
            "aggregate_editor_input",
            "workflow_exception_input",
        ]
        | None
    ) = Field(
        default=None,
        description=(
            "Semantic contract for the primary structured input artifact. When present, "
            "input_contract_ref must identify the artifact that validates against it."
        ),
    )
    input_contract_ref: str | None = Field(
        default=None,
        description="Artifact ref containing the structured input named by input_contract_kind.",
    )

    @model_validator(mode="after")
    def targets_use_fixed_taxonomy(self) -> "TaskEnvelope":
        for submodule_id in self.target_submodule_ids:
            definition = resolve_submodule(submodule_id)
            if self.agent_id.startswith("module-"):
                module_id = self.agent_id.removeprefix("module-").removesuffix("-specialist")
                if definition.module_id != module_id:
                    raise ValueError(
                        f"target submodule {submodule_id} does not belong to module {module_id}"
                    )
        if bool(self.input_contract_kind) != bool(self.input_contract_ref):
            raise ValueError("input_contract_kind and input_contract_ref must be provided together")
        if self.input_contract_ref and self.input_contract_ref not in self.input_refs:
            raise ValueError("input_contract_ref must also appear in input_refs")
        declared_refs = {
            *self.input_refs,
            *self.context_summary_refs,
            *([self.prior_result_ref] if self.prior_result_ref else []),
        }
        unexpected_modes = sorted(set(self.artifact_delivery_modes) - declared_refs)
        if unexpected_modes:
            raise ValueError(
                f"artifact delivery modes reference undeclared artifacts: {unexpected_modes}"
            )
        modes = dict(self.artifact_delivery_modes)
        for ref in self.input_refs:
            modes.setdefault(
                ref,
                "inline" if ref == self.input_contract_ref else "reference",
            )
        for ref in self.context_summary_refs:
            modes.setdefault(ref, "reference")
        if self.prior_result_ref:
            modes.setdefault(self.prior_result_ref, "reference")
        if self.input_contract_ref and modes[self.input_contract_ref] != "inline":
            raise ValueError("input contract is embedded and must use inline delivery")
        self.artifact_delivery_modes = modes
        return self


class PreparationResult(StrictModel):
    input_refs: list[str] = Field(min_length=1)
    evidence_items: list[EvidenceItem]
    coverage: CoverageMatrix
    photo_assets: list[PhotoAsset] = Field(default_factory=list)


class SourceRecord(StrictModel):
    id: str = Field(min_length=3)
    kind: SourceKind
    title: str = Field(min_length=1)
    locator: str = Field(min_length=1)
    publisher: str | None = None
    published_at: str | None = None
    accessed_at: str | None = None
    scope_note: str | None = None
    content_sha256: str | None = None

    @model_validator(mode="after")
    def id_matches_kind(self) -> "SourceRecord":
        if not self.id.startswith(SOURCE_PREFIX[self.kind]):
            raise ValueError("source id prefix does not match kind")
        return self


class ResearchNote(StrictModel):
    id: str = Field(min_length=1)
    task_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    synthesis: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    applicability: str = Field(min_length=1)


class ClaimRecord(StrictModel):
    id: str = Field(pattern=r"^C-")
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    submodule_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    claim_type: Literal[
        "project_fact", "technical_interpretation", "risk_judgment", "recommendation"
    ]
    source_ids: list[str] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    footnote_required: bool = True
    unresolved: bool = False

    @model_validator(mode="after")
    def project_fact_uses_evidence(self) -> "ClaimRecord":
        if resolve_submodule(self.submodule_id).module_id != self.module_id:
            raise ValueError(
                f"submodule {self.submodule_id} does not belong to module {self.module_id}"
            )
        if self.claim_type == "project_fact" and not any(
            source_id.startswith("E-") for source_id in self.source_ids
        ):
            raise ValueError("project_fact requires at least one E-* source")
        return self


class RevisionResponse(StrictModel):
    """One author-owned response to an immutable review finding."""

    finding_id: str = Field(
        min_length=1,
        description=(
            "Stable finding identifier copied exactly from the assigned review input. "
            "Do not invent, rename, or omit an assigned finding id."
        ),
    )
    action: Literal["implemented", "disputed", "needs_input"] = Field(
        description=(
            "implemented means the submitted revision addresses the finding; disputed "
            "means the author believes no content change is appropriate and explains why; "
            "needs_input means a concrete external fact or user decision is required."
        )
    )
    summary: str = Field(
        min_length=20,
        description=(
            "Concise author explanation of the actual change, the reason for disagreement, "
            "or the exact missing input. This is not a reviewer closure decision."
        ),
    )
    changed_target_ids: list[str] = Field(
        default_factory=list,
        description=(
            "Fixed submodule or final-report section ids actually changed for this finding. "
            "Use an empty list only for disputed or needs_input responses."
        ),
    )

    @model_validator(mode="after")
    def implemented_responses_name_changed_targets(self) -> "RevisionResponse":
        if self.action == "implemented" and not self.changed_target_ids:
            raise ValueError(
                "implemented revision response requires at least one changed target id"
            )
        if self.action != "implemented" and self.changed_target_ids:
            raise ValueError("only implemented revision responses may declare changed target ids")
        return self


class ModuleSubmission(StrictModel):
    kind: Literal["module_submission"] = "module_submission"
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    submodule_narratives: dict[str, str]
    claims: list[ClaimRecord]
    source_ids: list[str]
    unresolved_questions: list[str]
    revision: int = Field(ge=0)
    revision_responses: list[RevisionResponse] = Field(default_factory=list)

    @model_validator(mode="after")
    def uses_exact_fixed_submodules(self) -> "ModuleSubmission":
        expected = set(REPORT_TAXONOMY[self.module_id].submodules)
        actual = set(self.submodule_narratives)
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise ValueError(
                "module submission requires exact fixed submodules; "
                f"missing={missing}; extra={extra}"
            )
        if any(not narrative.strip() for narrative in self.submodule_narratives.values()):
            raise ValueError("submodule narratives cannot be empty")
        for submodule_id, narrative in self.submodule_narratives.items():
            lines = narrative.splitlines()
            first_content = next(
                (index for index, line in enumerate(lines) if line.strip()),
                None,
            )
            for index, line in enumerate(lines):
                if index == first_content and re.match(
                    rf"^#{{1,6}}\s+{re.escape(submodule_id)}(?:\.|\s|$)",
                    line.strip(),
                ):
                    continue
                if re.match(r"^#{1,6}\s+\d+(?:\.\d+)*\.?\s+", line.strip()):
                    raise ValueError(
                        f"submodule {submodule_id} body contains an extra numbered heading"
                    )
        wrong_claims = [claim.id for claim in self.claims if claim.module_id != self.module_id]
        if wrong_claims:
            raise ValueError(f"claims do not belong to module {self.module_id}: {wrong_claims}")
        claim_sources = {source_id for claim in self.claims for source_id in claim.source_ids}
        undeclared = sorted(claim_sources - set(self.source_ids))
        if undeclared:
            raise ValueError(
                f"module source_ids must include every Claim source; missing={undeclared}"
            )
        claim_by_id = {claim.id: claim for claim in self.claims}
        if len(claim_by_id) != len(self.claims):
            raise ValueError("module claims contain duplicate ids")
        marker_pattern = re.compile(r"\[\[CLAIM:(C-[^\]\s]+)\]\]")
        marker_locations: dict[str, list[str]] = {}
        for submodule_id, narrative in self.submodule_narratives.items():
            for claim_id in marker_pattern.findall(narrative):
                marker_locations.setdefault(claim_id, []).append(submodule_id)
        unknown_markers = sorted(set(marker_locations) - set(claim_by_id))
        if unknown_markers:
            raise ValueError(f"module narratives contain unknown Claim markers: {unknown_markers}")
        for claim in self.claims:
            locations = marker_locations.get(claim.id, [])
            requires_marker = claim.footnote_required and bool(claim.source_ids)
            if requires_marker and locations != [claim.submodule_id]:
                raise ValueError(
                    f"Claim {claim.id} marker must occur exactly once in "
                    f"submodule {claim.submodule_id}; got {locations}"
                )
            if not requires_marker and locations:
                raise ValueError(f"Claim {claim.id} does not require a citation marker")
        return self

    @property
    def markdown(self) -> str:
        """Deterministically render the typed submodule narratives for consumers."""

        return compose_module_markdown(self.module_id, self.submodule_narratives)


class ModuleRevisionSubmission(StrictModel):
    """Explicit author patch; the workflow applies it without inventing content."""

    kind: Literal["module_revision_submission"] = "module_revision_submission"
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Responsibility module being revised."
    )
    base_revision: int = Field(
        ge=0,
        description="Revision number of the exact module subject supplied to the author.",
    )
    revision: int = Field(
        ge=1,
        description="New revision number; it must equal base_revision plus one.",
    )
    submodule_narratives: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Complete replacement prose only for explicitly assigned fixed submodules. "
            "Unassigned submodules must be omitted, not repeated."
        ),
    )
    claims_upsert: list[ClaimRecord] = Field(
        default_factory=list,
        description=(
            "Complete replacement records for Claims added or changed inside assigned "
            "submodules. Unchanged Claims must be omitted."
        ),
    )
    claim_ids_remove: list[str] = Field(
        default_factory=list,
        description=(
            "Existing Claim ids inside assigned submodules that the author explicitly removes."
        ),
    )
    source_ids: list[str] = Field(
        description=("Complete source-id set for the resulting module after this patch is applied.")
    )
    unresolved_questions: list[str] = Field(
        default_factory=list,
        description="Complete unresolved-question set for the resulting module.",
    )
    revision_responses: list[RevisionResponse] = Field(
        min_length=1,
        description="Exactly one author response for every assigned finding id.",
    )

    @model_validator(mode="after")
    def patch_is_internally_consistent(self) -> "ModuleRevisionSubmission":
        if self.revision != self.base_revision + 1:
            raise ValueError("module revision must equal base_revision plus one")
        for submodule_id, narrative in self.submodule_narratives.items():
            if resolve_submodule(submodule_id).module_id != self.module_id:
                raise ValueError("module revision submodule belongs to a different module")
            if not narrative.strip():
                raise ValueError("module revision narrative cannot be empty")
        upsert_ids = [claim.id for claim in self.claims_upsert]
        if len(upsert_ids) != len(set(upsert_ids)):
            raise ValueError("module revision claims_upsert contains duplicate ids")
        if set(upsert_ids) & set(self.claim_ids_remove):
            raise ValueError("one Claim cannot be both upserted and removed")
        if len(self.claim_ids_remove) != len(set(self.claim_ids_remove)):
            raise ValueError("module revision claim_ids_remove contains duplicate ids")
        response_ids = [response.finding_id for response in self.revision_responses]
        if len(response_ids) != len(set(response_ids)):
            raise ValueError("module revision responses contain duplicate finding ids")
        implemented_targets = {
            target_id
            for response in self.revision_responses
            if response.action == "implemented"
            for target_id in response.changed_target_ids
        }
        if implemented_targets != set(self.submodule_narratives):
            raise ValueError(
                "module revision narratives must equal the targets declared by "
                "implemented revision responses"
            )
        return self


class ModuleDispatchPlan(StrictModel):
    """Deterministic internal dispatch state; never authored by a model."""

    module_tasks: list[TaskEnvelope] = Field(min_length=1)
    rationale: str = Field(min_length=1)


TEMPLATE_SKILL_TRANSFER_CATEGORIES = frozenset(
    {
        "analysis_method",
        "synthesis_method",
        "visual_method",
        "quality_check",
    }
)
TEMPLATE_SKILL_EXCLUSION_CATEGORIES = frozenset(
    {
        "domain_knowledge",
        "domain_standard_or_threshold",
        "project_fact_or_number",
        "customer_identity",
        "project_finding_or_risk",
        "project_conclusion_or_recommendation",
        "evidence_or_claim_identifier",
    }
)


class TemplateSkillBoundaryManifest(StrictModel):
    """Typed proof that template guidance was classified before Skill transfer.

    Each allowed method category includes its fact-free worked examples and
    output skeletons.  Those examples are teaching material inside the Skill,
    not a separate output-profile artifact.
    """

    policy_version: Literal[1] = Field(
        default=1,
        description=(
            "Version of the enforced reusable-guidance policy. Allowed methods include "
            "fact-free worked examples and output skeletons inside the Skill."
        ),
    )
    transferred_categories: list[
        Literal[
            "analysis_method",
            "synthesis_method",
            "visual_method",
            "quality_check",
        ]
    ] = Field(
        min_length=4,
        description=(
            "Exact reusable method categories transferred into the Template Skill; each "
            "category may and should carry fact-free positive, negative, or structural examples."
        ),
    )
    excluded_categories: list[
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
        min_length=7,
        description="Exact non-method categories excluded from every Template Skill file.",
    )
    boundary_statement: str = Field(
        min_length=80,
        max_length=1_500,
        description=(
            "Concise declaration that only reusable method was transferred and every "
            "excluded category must be sourced from Module Skill, Knowledge, or Evidence."
        ),
    )

    @model_validator(mode="after")
    def exact_boundary_categories(self) -> "TemplateSkillBoundaryManifest":
        if (
            set(self.transferred_categories) != TEMPLATE_SKILL_TRANSFER_CATEGORIES
            or len(self.transferred_categories) != len(TEMPLATE_SKILL_TRANSFER_CATEGORIES)
        ):
            raise ValueError(
                "template Skill must transfer exactly the four reusable guidance categories"
            )
        if (
            set(self.excluded_categories) != TEMPLATE_SKILL_EXCLUSION_CATEGORIES
            or len(self.excluded_categories) != len(TEMPLATE_SKILL_EXCLUSION_CATEGORIES)
        ):
            raise ValueError("template Skill must exclude every non-method content category")
        return self


class TemplateSkillSubmission(StrictModel):
    """A run-scoped writing Skill distilled semantically by Main from a template."""

    kind: Literal["template_skill_submission"] = "template_skill_submission"
    name: Literal["report-template-writing"] = "report-template-writing"
    description: str = Field(min_length=40, max_length=1024)
    skill_markdown: str = Field(min_length=300, max_length=20_000)
    analysis_language_reference: str = Field(min_length=120, max_length=16_000)
    synthesis_reference: str = Field(min_length=120, max_length=16_000)
    visual_organization_reference: str = Field(min_length=120, max_length=12_000)
    quality_rubric: str = Field(min_length=120, max_length=12_000)
    boundary_manifest: TemplateSkillBoundaryManifest = Field(
        description=(
            "Typed reusable-guidance transfer declaration; allowed methods retain their "
            "fact-free worked examples inside this Skill."
        )
    )

    @model_validator(mode="after")
    def skill_links_its_progressive_references(self) -> "TemplateSkillSubmission":
        lines = self.skill_markdown.splitlines()
        if not lines or lines[0].strip() != "---":
            raise ValueError("template Skill must start with YAML frontmatter")
        try:
            closing = next(
                index for index, line in enumerate(lines[1:], start=1) if line.strip() == "---"
            )
        except StopIteration as exc:
            raise ValueError("template Skill frontmatter is not closed") from exc
        try:
            metadata = yaml.safe_load("\n".join(lines[1:closing])) or {}
        except yaml.YAMLError as exc:
            raise ValueError("template Skill frontmatter is invalid YAML") from exc
        if not isinstance(metadata, dict) or set(metadata) != {"name", "description"}:
            raise ValueError("template Skill frontmatter must contain exactly name and description")
        if metadata["name"] != self.name:
            raise ValueError("template Skill frontmatter name must match submission name")
        if str(metadata["description"]).strip() != self.description.strip():
            raise ValueError(
                "template Skill frontmatter description must match submission description"
            )
        if not any(line.lstrip().startswith("# ") for line in lines[closing + 1 :]):
            raise ValueError("template Skill body must contain a top-level heading")
        required = {
            "references/analysis-language.md",
            "references/synthesis.md",
            "references/visual-organization.md",
            "references/quality-rubric.md",
        }
        missing = sorted(ref for ref in required if ref not in self.skill_markdown)
        if missing:
            raise ValueError(f"template Skill must link progressive references: {missing}")
        return self


# Review contracts deliberately separate immutable findings from author
# responses and reviewer verdicts.
class ModuleReviewCoverage(StrictModel):
    submodule_ids: list[str] = Field(
        min_length=1,
        description=(
            "Every fixed submodule actually reviewed in this pass. Initial review must "
            "cover the assigned module scope; recheck covers changed targets plus regression."
        ),
    )


class ModuleReviewFinding(StrictModel):
    id: str = Field(
        min_length=1,
        description=(
            "Reviewer-created stable finding id. It is immutable after first submission "
            "and is referenced by later author responses and reviewer verdicts."
        ),
    )
    target_submodule_id: str = Field(
        min_length=1,
        description=(
            "Single fixed submodule owned by the audited module where the author must act."
        ),
    )
    category: str = Field(
        min_length=1,
        description=(
            "Short stable defect category such as evidence_boundary, factual_accuracy, "
            "analysis_depth, action_closure, or editorial_quality."
        ),
    )
    impact: Literal["blocking", "advisory"] = Field(
        description=(
            "blocking means the module cannot be approved without a reviewer verdict; "
            "advisory still requires an explicit author response and reviewer disposition "
            "but does not assert immediate technical unsafety."
        )
    )
    observation: str = Field(
        min_length=20,
        description=(
            "Concrete description of what is wrong in the current subject. Quote or locate "
            "the relevant fact, claim, wording, or missing analysis; do not write a generic preference."
        ),
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        description=(
            "Artifact or source ids that let the author and later reviewer reproduce the observation."
        ),
    )
    required_change: str = Field(
        min_length=20,
        description=(
            "Observable content change required from the module author. It must stay within "
            "target_submodule_id and must not prescribe unsupported project facts."
        ),
    )
    reviewer_checks: list[str] = Field(
        min_length=1,
        description=(
            "Semantic checks the same reviewer will perform during recheck before resolving "
            "this finding. These are reviewer judgments, not machine-generated closure."
        ),
    )


class CrossReviewCoverageEntry(StrictModel):
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description="Module whose cross-module interfaces were reviewed."
    )
    checked_dimensions: list[
        Literal[
            "terminology",
            "facts",
            "risk_levels",
            "dependencies",
            "propagation",
            "joint_verification",
        ]
    ] = Field(
        min_length=1,
        description=(
            "Cross-review dimensions actually checked for this module. This records coverage "
            "only and does not assert that the module is integrated or approved."
        ),
    )


class MachineCheckSpec(StrictModel):
    kind: Literal[
        "forbidden_terms_absent",
        "required_terms_present",
        "field_equals",
    ] = Field(
        description=(
            "Explicit deterministic predicate that can reject an author revision before "
            "semantic recheck. It can never resolve a finding by itself."
        )
    )
    target_paths: list[str] = Field(
        min_length=1,
        description=(
            "Structured subject paths to inspect, for example submodule_narratives.2.3.1 "
            "or claims.2.3.1. Paths must be declared explicitly; the workflow must not infer them."
        ),
    )
    expected_values: list[str] = Field(
        min_length=1,
        description=(
            "Terms or exact values used by the declared predicate. Do not encode semantic "
            "quality judgments as machine checks."
        ),
    )


class CrossReviewFinding(StrictModel):
    id: str = Field(
        min_length=1,
        description=(
            "Cross-reviewer-created stable finding id. Only the same cross reviewer may "
            "later resolve or reopen it."
        ),
    )
    owner_module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"] = Field(
        description=(
            "Single responsibility module that must write the missing or corrected "
            "cross-module relationship into its own content."
        )
    )
    target_submodule_ids: list[str] = Field(
        min_length=1,
        description=(
            "Fixed submodules inside owner_module_id where the relationship must be written."
        ),
    )
    related_module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(
        min_length=1,
        description=(
            "Other modules whose facts, risks, dependencies, or verification interfaces "
            "participate in this finding; never include owner_module_id."
        ),
    )
    category: Literal[
        "terminology",
        "facts",
        "risk_levels",
        "dependencies",
        "propagation",
        "joint_verification",
    ] = Field(description="Cross-review dimension that produced the finding.")
    impact: Literal["blocking", "advisory"] = Field(
        description=(
            "blocking prevents cross-review completion; advisory still requires an explicit "
            "author response and cross-reviewer disposition."
        )
    )
    observation: str = Field(
        min_length=30,
        description=(
            "Concrete interface defect in the current modules, including the connected "
            "objects or premises and why the existing module-local text is insufficient."
        ),
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        description=(
            "Current module, Claim, or source references that reproduce the cross-module defect."
        ),
    )
    required_change: str = Field(
        min_length=30,
        description=(
            "Module-local writeback required from owner_module_id, including mechanism, "
            "effect on its risk or action, dependencies, and joint verification where applicable."
        ),
    )
    reviewer_checks: list[str] = Field(
        min_length=1,
        description=(
            "Semantic checks the same cross reviewer will apply after the module author revises."
        ),
    )
    machine_checks: list[MachineCheckSpec] = Field(
        default_factory=list,
        description=(
            "Optional explicit syntactic predicates. Passing them is only a prerequisite "
            "for cross recheck and never substitutes for reviewer_checks."
        ),
    )

    @model_validator(mode="after")
    def cross_targets_belong_to_owner(self) -> "CrossReviewFinding":
        if self.owner_module_id in self.related_module_ids:
            raise ValueError("cross finding cannot relate the owner module to itself")
        for submodule_id in self.target_submodule_ids:
            if resolve_submodule(submodule_id).module_id != self.owner_module_id:
                raise ValueError(
                    "cross finding target submodule belongs to a different owner module"
                )
        return self


class FinalReviewTargetChange(StrictModel):
    target_section_id: str = Field(
        min_length=1,
        description="One fixed chief-owned Chapter 1, 3, or 4 section to revise.",
    )
    required_change: str = Field(
        min_length=20,
        description="Observable chief-editor change required in this exact section.",
    )
    reviewer_checks: list[str] = Field(
        min_length=1,
        description=("Semantic checks the same final reviewer will apply to this exact section."),
    )

    @model_validator(mode="after")
    def target_is_chief_owned(self) -> "FinalReviewTargetChange":
        if self.target_section_id not in FINAL_AUDIT_SECTION_IDS:
            raise ValueError(
                f"final target change contains invalid section id: {self.target_section_id}"
            )
        return self


class FinalReviewFinding(StrictModel):
    id: str = Field(
        min_length=1,
        description=(
            "Final-reviewer-created stable finding id. It remains immutable across chief-editor revisions."
        ),
    )
    target_section_ids: list[str] = Field(
        min_length=1,
        description=(
            "Fixed final-report sections where the chief editor must act. Use only ids "
            "declared by the final-review input contract."
        ),
    )
    target_changes: list[FinalReviewTargetChange] = Field(
        min_length=1,
        description=(
            "One explicit chief-editor change contract per targeted Chapter 1, 3, or 4 section."
        ),
    )
    category: str = Field(
        min_length=1,
        description=(
            "Stable final-delivery defect category such as retention, synthesis, traceability, "
            "actionability, citation_integrity, or editorial_quality."
        ),
    )
    impact: Literal["blocking", "advisory"] = Field(
        description=(
            "blocking prevents delivery; advisory requires explicit response and reviewer "
            "disposition but does not itself claim that upstream technical facts are wrong."
        )
    )
    observation: str = Field(
        min_length=20,
        description=(
            "Concrete defect introduced or exposed by final editing, located in the current report."
        ),
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        description=(
            "Current edited-report or trusted upstream artifact refs that reproduce the defect."
        ),
    )
    @model_validator(mode="after")
    def final_targets_use_fixed_sections(self) -> "FinalReviewFinding":
        invalid = sorted(set(self.target_section_ids) - set(FINAL_AUDIT_SECTION_IDS))
        if invalid:
            raise ValueError(f"final finding contains invalid section ids: {invalid}")
        change_ids = [change.target_section_id for change in self.target_changes]
        if len(change_ids) != len(set(change_ids)):
            raise ValueError("final finding target changes must use unique section ids")
        if set(change_ids) != set(self.target_section_ids):
            raise ValueError("final finding target_section_ids must exactly match target_changes")
        return self


class ResolutionVerdict(StrictModel):
    finding_id: str = Field(
        min_length=1,
        description=(
            "Stable id copied from a required prior finding. Do not repeat or alter the finding contract."
        ),
    )
    verdict: Literal["resolved", "open", "escalate"] = Field(
        description=(
            "resolved means reviewer_checks are satisfied; open means more author revision "
            "is required; escalate means a genuine disagreement or external decision requires Main."
        )
    )
    reason: str = Field(
        min_length=20,
        description=(
            "Reviewer-owned explanation based on the current subject and the original reviewer_checks."
        ),
    )
    evidence_refs: list[str] = Field(
        default_factory=list,
        description=(
            "Current subject refs and precise evidence used for the verdict. Required for resolved."
        ),
    )

    @model_validator(mode="after")
    def resolved_verdict_has_current_evidence(self) -> "ResolutionVerdict":
        if self.verdict == "resolved" and not self.evidence_refs:
            raise ValueError("resolved verdict requires current evidence refs")
        return self


class CrossSynthesisInput(StrictModel):
    id: str = Field(
        min_length=1,
        description="Stable synthesis input id for chief-editor traceability.",
    )
    related_module_ids: list[Literal["2.1", "2.2", "2.3", "2.4", "2.5"]] = Field(
        min_length=2,
        description="Modules connected by this already-supported synthesis input.",
    )
    cluster_type: Literal[
        "risk_cluster",
        "global_propagation",
        "action_dependency",
        "monitoring_blind_spot",
        "recovery_capability",
    ] = Field(description="System-level relationship class used for portfolio completeness.")
    root_causes: list[str] = Field(
        min_length=1,
        description="Evidence-bounded common causes or preconditions shared by the modules.",
    )
    propagation_steps: list[str] = Field(
        min_length=2,
        description="Ordered mechanism or dependency steps, not a list of module conclusions.",
    )
    causal_chain: str = Field(
        min_length=30,
        description=(
            "Evidence-bounded causal or dependency chain that the chief editor must account for."
        ),
    )
    decision_implication: str = Field(
        min_length=20,
        description="Why the relationship changes management priority, sequencing, or residual risk.",
    )
    action_dependencies: list[str] = Field(
        min_length=1,
        description="Ordered or conditional implementation dependencies across modules.",
    )
    joint_actions: list[str] = Field(
        min_length=1,
        description="Cross-owner actions that must be implemented as one coordinated package.",
    )
    verification_method: str = Field(
        min_length=20,
        description="Joint acceptance or monitoring method for the relationship.",
    )
    acceptance_criteria: list[str] = Field(
        min_length=1,
        description="Observable joint acceptance criteria for the complete relationship.",
    )
    module_statement_refs: list[str] = Field(
        min_length=2,
        description=(
            "Existing module/submodule refs proving that every local link is already "
            "written back; otherwise the reviewer must create a Cross finding."
        ),
    )
    confidence_and_boundary: str = Field(
        min_length=20,
        description="Confidence, missing evidence, and limits on the supported inference.",
    )
    target_report_section_ids: list[Literal["3.1.1", "3.1.2", "3.2"]] = Field(
        min_length=1,
        description="Final synthesis sections that must account for this input.",
    )
    evidence_refs: list[str] = Field(
        min_length=1,
        description=(
            "Reviewed module/source refs supporting the synthesis input, including at "
            "least one registered current-run E-* project-evidence id."
        ),
    )

    @model_validator(mode="after")
    def traceable_modules_and_claims(self) -> "CrossSynthesisInput":
        if len(self.related_module_ids) != len(set(self.related_module_ids)):
            raise ValueError("related_module_ids must be unique")
        combined = "\n".join(
            [
                self.causal_chain,
                self.decision_implication,
                *self.root_causes,
                *self.propagation_steps,
                *self.action_dependencies,
                *self.joint_actions,
                *self.acceptance_criteria,
                *self.module_statement_refs,
            ]
        )
        mentioned = {
            match.group(1) for match in re.finditer(r"(?<!\d)(2\.[1-5])(?:\.\d+)*(?!\d)", combined)
        }
        undeclared = sorted(mentioned - set(self.related_module_ids))
        if undeclared:
            raise ValueError(f"synthesis text references undeclared related modules: {undeclared}")
        statement_modules = {
            match.group(1)
            for ref in self.module_statement_refs
            for match in re.finditer(r"(?<!\d)(2\.[1-5])(?:\.\d+)*(?!\d)", ref)
        }
        missing_statements = sorted(set(self.related_module_ids) - statement_modules)
        if missing_statements:
            raise ValueError(
                f"module_statement_refs must cover every related module: {missing_statements}"
            )
        if not any(ref.startswith("E-") for ref in self.evidence_refs):
            raise ValueError("Cross synthesis input requires at least one E-* evidence ref")
        return self


class ModuleReviewFindingSubmission(StrictModel):
    kind: Literal["module_review_finding_submission"] = "module_review_finding_submission"
    coverage: ModuleReviewCoverage = Field(
        description="Actual module-local review coverage for this pass."
    )
    findings: list[ModuleReviewFinding] = Field(
        default_factory=list,
        description=(
            "New immutable findings created from the current module. Empty means no module-local issue."
        ),
    )

    @model_validator(mode="after")
    def finding_ids_are_unique(self) -> "ModuleReviewFindingSubmission":
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("module review finding ids must be unique")
        return self


class ModuleReviewVerdictSubmission(StrictModel):
    kind: Literal["module_review_verdict_submission"] = "module_review_verdict_submission"
    coverage: ModuleReviewCoverage = Field(
        description="Changed-target and regression coverage for this recheck."
    )
    verdicts: list[ResolutionVerdict] = Field(
        description="Exactly one verdict for every required prior finding id."
    )
    new_findings: list[ModuleReviewFinding] = Field(
        default_factory=list,
        description=(
            "Only genuinely new regression findings introduced by the revision; do not "
            "restate prior findings here."
        ),
    )

    @model_validator(mode="after")
    def verdict_and_new_finding_ids_are_unique(self) -> "ModuleReviewVerdictSubmission":
        verdict_ids = [verdict.finding_id for verdict in self.verdicts]
        finding_ids = [finding.id for finding in self.new_findings]
        if len(verdict_ids) != len(set(verdict_ids)):
            raise ValueError("module review verdict ids must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("new module review finding ids must be unique")
        if set(verdict_ids) & set(finding_ids):
            raise ValueError("a prior finding id cannot also be submitted as a new finding")
        return self


class CrossReviewFindingSubmission(StrictModel):
    kind: Literal["cross_review_finding_submission"] = "cross_review_finding_submission"
    coverage: list[CrossReviewCoverageEntry] = Field(
        min_length=5,
        description=("Exactly one coverage entry for each of modules 2.1 through 2.5."),
    )
    findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description=(
            "Cross-module defects requiring explicit module-author response and later "
            "closure by this same cross reviewer."
        ),
    )
    synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description=(
            "Supported cross-module relationships for chief synthesis that do not require "
            "additional module-local writeback."
        ),
    )

    @model_validator(mode="after")
    def coverage_and_ids_are_complete_and_unique(self) -> "CrossReviewFindingSubmission":
        module_ids = [entry.module_id for entry in self.coverage]
        if len(module_ids) != 5 or set(module_ids) != set(REPORT_TAXONOMY):
            raise ValueError("cross review coverage must contain each module exactly once")
        finding_ids = [finding.id for finding in self.findings]
        synthesis_ids = [item.id for item in self.synthesis_inputs]
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("cross review finding ids must be unique")
        if len(synthesis_ids) != len(set(synthesis_ids)):
            raise ValueError("cross synthesis input ids must be unique")
        return self


class CrossReviewVerdictSubmission(StrictModel):
    kind: Literal["cross_review_verdict_submission"] = "cross_review_verdict_submission"
    coverage: list[CrossReviewCoverageEntry] = Field(
        min_length=5,
        description="Current delta and interface-regression coverage for all five modules.",
    )
    verdicts: list[ResolutionVerdict] = Field(
        description="Exactly one verdict for every required prior cross finding id."
    )
    new_findings: list[CrossReviewFinding] = Field(
        default_factory=list,
        description=("Only genuinely new cross-module regressions introduced by the revisions."),
    )
    synthesis_inputs: list[CrossSynthesisInput] = Field(
        default_factory=list,
        description="Updated supported inputs for the chief editor after recheck.",
    )

    @model_validator(mode="after")
    def coverage_and_ids_are_complete_and_unique(self) -> "CrossReviewVerdictSubmission":
        module_ids = [entry.module_id for entry in self.coverage]
        if len(module_ids) != 5 or set(module_ids) != set(REPORT_TAXONOMY):
            raise ValueError("cross recheck coverage must contain each module exactly once")
        verdict_ids = [verdict.finding_id for verdict in self.verdicts]
        finding_ids = [finding.id for finding in self.new_findings]
        if len(verdict_ids) != len(set(verdict_ids)):
            raise ValueError("cross review verdict ids must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("new cross review finding ids must be unique")
        if set(verdict_ids) & set(finding_ids):
            raise ValueError("a prior cross finding cannot also be submitted as new")
        return self


class FinalReviewFindingSubmission(StrictModel):
    kind: Literal["final_review_finding_submission"] = "final_review_finding_submission"
    checked_section_ids: list[str] = Field(
        min_length=1,
        description="Every fixed final-report section actually checked in this pass.",
    )
    findings: list[FinalReviewFinding] = Field(
        default_factory=list,
        description="New immutable final-delivery findings from the current edited report.",
    )
    residual_risks: list[str] = Field(
        default_factory=list,
        description=(
            "Transparent non-corrective limitations that should remain visible to the reader; "
            "do not use this field to hide actionable findings."
        ),
    )

    @model_validator(mode="after")
    def finding_ids_are_unique(self) -> "FinalReviewFindingSubmission":
        ids = [finding.id for finding in self.findings]
        if len(ids) != len(set(ids)):
            raise ValueError("final review finding ids must be unique")
        _validate_non_actionable_residual_risks(self.residual_risks)
        return self


class FinalReviewVerdictSubmission(StrictModel):
    kind: Literal["final_review_verdict_submission"] = "final_review_verdict_submission"
    checked_section_ids: list[str] = Field(
        min_length=1,
        description="Changed sections plus whole-report regression scope checked in this recheck.",
    )
    verdicts: list[ResolutionVerdict] = Field(
        description="Exactly one verdict for every required prior final-review finding id."
    )
    new_findings: list[FinalReviewFinding] = Field(
        default_factory=list,
        description="Only genuinely new final-report regressions introduced by the revision.",
    )
    residual_risks: list[str] = Field(
        default_factory=list,
        description="Updated transparent limitations that are not actionable findings.",
    )

    @model_validator(mode="after")
    def verdict_and_new_finding_ids_are_unique(self) -> "FinalReviewVerdictSubmission":
        verdict_ids = [verdict.finding_id for verdict in self.verdicts]
        finding_ids = [finding.id for finding in self.new_findings]
        if len(verdict_ids) != len(set(verdict_ids)):
            raise ValueError("final review verdict ids must be unique")
        if len(finding_ids) != len(set(finding_ids)):
            raise ValueError("new final review finding ids must be unique")
        if set(verdict_ids) & set(finding_ids):
            raise ValueError("a prior final finding cannot also be submitted as new")
        _validate_non_actionable_residual_risks(self.residual_risks)
        return self


def _validate_non_actionable_residual_risks(values: list[str]) -> None:
    report_defect_terms = (
        "报告缺少",
        "正文缺少",
        "未纳入报告",
        "未生成表格",
        "表格缺失",
        "未选择图片",
        "图片缺失",
    )
    actionable = [value for value in values if any(term in value for term in report_defect_terms)]
    if actionable:
        raise ValueError(
            "actionable report omissions must be final-review findings, not residual_risks: "
            f"{actionable}"
        )


FINAL_REPORT_SECTION_IDS = REPORT_FINAL_SECTION_IDS
FINAL_AUDIT_SECTION_IDS = REPORT_FINAL_AUDIT_SECTION_IDS

CROSS_REVIEW_DIMENSIONS = (
    "terminology",
    "facts",
    "risk_levels",
    "dependencies",
    "propagation",
    "joint_verification",
)


class WorkflowDecisionSubmission(StrictModel):
    """A lead decision for an explicit reviewer or author exception."""

    kind: Literal["workflow_decision_submission"] = "workflow_decision_submission"
    decision: Literal[
        "accept_dispute",
        "return_to_author",
        "request_user",
        "stop_incomplete",
    ]
    rationale: str = Field(min_length=1)
    finding_ids: list[str] = Field(
        min_length=1,
        description="Exact exception finding ids covered by this decision.",
    )


class TableSubmission(StrictModel):
    title: str = Field(min_length=1)
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)
    source_ids: list[str] = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def rows_match_headers(self) -> "TableSubmission":
        invalid = [index for index, row in enumerate(self.rows) if len(row) != len(self.headers)]
        if invalid:
            raise ValueError(f"table rows do not match header width: {invalid}")
        return self


class TableSubmissionInput(StrictModel):
    """Model-facing table contract; runtime owns internal Claim bindings."""

    title: str = Field(min_length=1)
    headers: list[str] = Field(min_length=1)
    rows: list[list[str]] = Field(default_factory=list)
    evidence_ids: list[str] = Field(
        min_length=1,
        description="Registered current-run E-* evidence supporting the table.",
    )

    @model_validator(mode="after")
    def valid_rows_and_evidence(self) -> "TableSubmissionInput":
        invalid = [index for index, row in enumerate(self.rows) if len(row) != len(self.headers)]
        if invalid:
            raise ValueError(f"table rows do not match header width: {invalid}")
        if len(self.evidence_ids) != len(set(self.evidence_ids)) or any(
            not item.startswith("E-") for item in self.evidence_ids
        ):
            raise ValueError("table evidence_ids must be unique E-* ids")
        return self


class EditedReportSubmission(StrictModel):
    kind: Literal["edited_report_submission"] = "edited_report_submission"
    title: str = Field(min_length=1)
    assessment_background: str = Field(min_length=1)
    findings_overview: str = Field(min_length=1)
    regional_executive_summary: str = Field(min_length=1)
    module_narratives: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str]
    risk_panorama: str = Field(min_length=1)
    dimension_risk_analysis: str = Field(min_length=1)
    data_gap_analysis: str = Field(min_length=1)
    improvement_action_plan: str = Field(min_length=1)
    special_topic_plan: SpecialTopicPlan | None = Field(
        default=None,
        description="Runtime-owned immutable Chapter 4 headings and requirements from Inputs."
    )
    special_topic_analysis: str | None = Field(default=None, min_length=1)
    protected_claim_ids: list[str] = Field(default_factory=list)
    tables: list[TableSubmission] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    unresolved_editorial_issues: list[str] = Field(default_factory=list)
    revision_responses: list[RevisionResponse] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def discard_deleted_cross_module_metadata(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            value.pop("synthesis_dispositions", None)
            value.pop("synthesis_tables", None)
        return value

    @model_validator(mode="after")
    def complete_modules(self) -> "EditedReportSubmission":
        if set(self.module_narratives) != {"2.1", "2.2", "2.3", "2.4", "2.5"}:
            raise ValueError("edited report requires exactly modules 2.1-2.5")
        body_fields = (
            "assessment_background",
            "findings_overview",
            "regional_executive_summary",
            "risk_panorama",
            "dimension_risk_analysis",
            "data_gap_analysis",
            "improvement_action_plan",
        )
        for field in body_fields:
            value = getattr(self, field)
            if re.search(r"^#{1,6}\s+\d+(?:\.\d+)*\.?\s+", value, flags=re.MULTILINE):
                raise ValueError(
                    f"{field} must contain section body only, without numbered headings"
                )
        if (self.special_topic_plan is None) != (self.special_topic_analysis is None):
            raise ValueError(
                "special_topic_plan and special_topic_analysis must either both be present "
                "or both be absent"
            )
        if self.special_topic_plan is not None and self.special_topic_analysis is not None:
            self.special_topic_plan.validate_analysis(self.special_topic_analysis)
        return self


class TextArtifactRef(StrictModel):
    """Task-scoped text parts materialized before domain validation."""

    artifact_refs: list[str] = Field(min_length=1)
    separator: str = Field(default="\n\n", max_length=20)


class ModuleSubmissionInput(StrictModel):
    """Small author-facing commit; runtime owns prose paths and Claim ids."""

    kind: Literal["module_submission"] = "module_submission"
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    unresolved_questions: list[str]
    revision: int = Field(ge=0)
    revision_responses: list[RevisionResponse] = Field(default_factory=list)


class ModuleRevisionSubmissionInput(StrictModel):
    """Small revision commit; changed prose and evidence live in result parts."""

    kind: Literal["module_revision_submission"] = "module_revision_submission"
    module_id: Literal["2.1", "2.2", "2.3", "2.4", "2.5"]
    base_revision: int = Field(ge=0)
    revision: int = Field(ge=1)
    unresolved_questions: list[str] = Field(default_factory=list)
    revision_responses: list[RevisionResponse] = Field(min_length=1)


class ChiefRevisionSubmission(StrictModel):
    """Explicit final-report patch assembled from current-task result parts."""

    kind: Literal["chief_revision_submission"] = "chief_revision_submission"
    base_subject_ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    section_bodies: dict[str, str] = Field(min_length=1)
    section_part_refs: dict[str, str] = Field(min_length=1)
    revision_responses: list[RevisionResponse] = Field(min_length=1)

    @model_validator(mode="after")
    def section_maps_match(self) -> "ChiefRevisionSubmission":
        if set(self.section_bodies) != set(self.section_part_refs):
            raise ValueError("chief revision section bodies and result-part refs must match")
        invalid = sorted(set(self.section_bodies) - set(FINAL_AUDIT_SECTION_IDS))
        if invalid:
            raise ValueError(f"chief revision contains non-chief section ids: {invalid}")
        return self


class ChiefRevisionSubmissionInput(StrictModel):
    """Small chief revision commit; changed prose lives in result parts."""

    kind: Literal["chief_revision_submission"] = "chief_revision_submission"
    base_subject_ref: str = Field(min_length=1)
    revision: int = Field(ge=1)
    revision_responses: list[RevisionResponse] = Field(min_length=1)


class TemplateSkillSubmissionInput(StrictModel):
    """Function-call input permitting durable references for five long Skill files."""

    kind: Literal["template_skill_submission"] = "template_skill_submission"
    name: Literal["report-template-writing"] = "report-template-writing"
    description: str = Field(min_length=40, max_length=1024)
    skill_markdown: str | TextArtifactRef
    analysis_language_reference: str | TextArtifactRef
    synthesis_reference: str | TextArtifactRef
    visual_organization_reference: str | TextArtifactRef
    quality_rubric: str | TextArtifactRef
    boundary_manifest: TemplateSkillBoundaryManifest = Field(
        description=(
            "Typed reusable-guidance transfer declaration; allowed methods retain their "
            "fact-free worked examples inside this Skill."
        )
    )


class EditedReportSubmissionInput(StrictModel):
    """Function-call input that permits durable references for edited report prose."""

    kind: Literal["edited_report_submission"] = "edited_report_submission"
    title: str = Field(min_length=1)
    assessment_background: str | TextArtifactRef
    findings_overview: str | TextArtifactRef
    regional_executive_summary: str | TextArtifactRef
    module_narratives: dict[Literal["2.1", "2.2", "2.3", "2.4", "2.5"], str | TextArtifactRef]
    risk_panorama: str | TextArtifactRef
    dimension_risk_analysis: str | TextArtifactRef
    data_gap_analysis: str | TextArtifactRef
    improvement_action_plan: str | TextArtifactRef
    special_topic_analysis: str | TextArtifactRef | None = None
    tables: list[TableSubmissionInput] = Field(default_factory=list)
    photo_ids: list[str] = Field(default_factory=list)
    unresolved_editorial_issues: list[str] = Field(default_factory=list)
    revision_responses: list[RevisionResponse] = Field(default_factory=list)


class SkillEvolutionSubmission(StrictModel):
    kind: Literal["skill_evolution_submission"] = "skill_evolution_submission"
    scope: Literal["product"] = "product"
    action: Literal[
        "feedback_recorded", "candidate_created", "evaluated", "published", "rolled_back"
    ]
    skill_id: str = Field(min_length=1)
    artifact_ids: list[str] = Field(min_length=1)
    summary: str = Field(min_length=1)


Submission = Annotated[
    ModuleSubmission
    | ModuleRevisionSubmission
    | TemplateSkillSubmission
    | ModuleReviewFindingSubmission
    | ModuleReviewVerdictSubmission
    | CrossReviewFindingSubmission
    | CrossReviewVerdictSubmission
    | FinalReviewFindingSubmission
    | FinalReviewVerdictSubmission
    | WorkflowDecisionSubmission
    | ChiefRevisionSubmission
    | EditedReportSubmission
    | SkillEvolutionSubmission,
    Field(discriminator="kind"),
]


SubmissionInput = Annotated[
    ModuleSubmissionInput
    | ModuleRevisionSubmissionInput
    | ChiefRevisionSubmissionInput
    | TemplateSkillSubmissionInput
    | ModuleReviewFindingSubmission
    | ModuleReviewVerdictSubmission
    | CrossReviewFindingSubmission
    | CrossReviewVerdictSubmission
    | FinalReviewFindingSubmission
    | FinalReviewVerdictSubmission
    | WorkflowDecisionSubmission
    | EditedReportSubmissionInput
    | SkillEvolutionSubmission,
    Field(discriminator="kind"),
]


SUBMISSION_INPUT_TYPES: dict[str, type[BaseModel]] = {
    "module_submission": ModuleSubmissionInput,
    "module_revision_submission": ModuleRevisionSubmissionInput,
    "chief_revision_submission": ChiefRevisionSubmissionInput,
    "template_skill_submission": TemplateSkillSubmissionInput,
    "module_review_finding_submission": ModuleReviewFindingSubmission,
    "module_review_verdict_submission": ModuleReviewVerdictSubmission,
    "cross_review_finding_submission": CrossReviewFindingSubmission,
    "cross_review_verdict_submission": CrossReviewVerdictSubmission,
    "final_review_finding_submission": FinalReviewFindingSubmission,
    "final_review_verdict_submission": FinalReviewVerdictSubmission,
    "workflow_decision_submission": WorkflowDecisionSubmission,
    "edited_report_submission": EditedReportSubmissionInput,
    "skill_evolution_submission": SkillEvolutionSubmission,
}


class AgentRunStatus(StrEnum):
    COMPLETED = "completed"
    BLOCKED = "blocked"
    INCOMPLETE = "incomplete"
    FAILED = "failed"


class AgentResult(StrictModel):
    task_id: str
    run_id: str
    agent_id: str
    session_id: str
    status: AgentRunStatus
    payload: Submission | None = None
    raw_output: str = ""
    reason: str | None = None

    @model_validator(mode="after")
    def completed_has_payload(self) -> "AgentResult":
        if self.status == AgentRunStatus.COMPLETED and self.payload is None:
            raise ValueError("completed result requires payload")
        return self


class CitationEntry(StrictModel):
    marker: int = Field(ge=1)
    claim_id: str
    source_ids: list[str] = Field(min_length=1)


class CitationPlan(StrictModel):
    entries: list[CitationEntry]
    evidence_index_markdown: str
