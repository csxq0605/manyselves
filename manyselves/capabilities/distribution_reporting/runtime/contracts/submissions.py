"""Capability-owned source of model-visible reporting submission contracts.

Pydantic supplies structural types.  This module adds the field semantics and
valid examples that a model needs to use those types without guessing.  The
same enriched schema is sent to the provider and rendered into the task prompt.
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from pydantic import TypeAdapter

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    SUBMISSION_INPUT_TYPES,
    TEMPLATE_ROLE_SKILL_IDS,
)

FIELD_GUIDANCE: dict[str, str] = {
    "action": "Stage-specific action selected from the declared enum; use exactly one allowed value.",
    "action_dependencies": "Ordered or conditional implementation dependencies supported by reviewed evidence.",
    "joint_actions": "Cross-owner actions that must be implemented as one coordinated package.",
    "acceptance_criteria": "Observable joint acceptance criteria for the complete relationship.",
    "agent_id": "Canonical responsibility Agent id assigned by the workflow.",
    "allowed_outputs": "Submission kinds this task is permitted to return.",
    "allowed_tools": "Tool names available to the assigned Agent for this task.",
    "artifact_ids": "Persisted artifact identifiers produced or affected by the action.",
    "assigned_findings": (
        "Immutable Final findings assigned only to this Chief chapter lane; every target "
        "must stay inside the lane section_ids."
    ),
    "assessment_background": "Final report section 1.1 body; facts must remain traceable to approved inputs.",
    "base_revision": "Exact prior module revision to which an explicit module patch applies.",
    "category": "Stable defect or review-dimension category defined by the active stage contract.",
    "causal_chain": "Evidence-bounded causal, dependency, or propagation chain connecting reviewed modules.",
    "cluster_type": "Evidence-based label for the supported cross-module relationship.",
    "root_causes": "Evidence-bounded common causes or preconditions shared across modules.",
    "propagation_steps": "Ordered mechanism or dependency steps across the related modules.",
    "module_statement_refs": "Existing module or submodule refs proving every local link is already written back.",
    "confidence_and_boundary": "Confidence, missing evidence, and inference limits for the relationship.",
    "target_report_section_ids": "Final synthesis sections that must account for a supported Cross input.",
    "changed_target_ids": "Fixed submodule or final-section ids actually changed for one finding response.",
    "checked_dimensions": "Cross-review dimensions actually checked; coverage is not an approval decision.",
    "checked_section_ids": "Final summary/conclusion sections actually checked: only 1.1, 1.2, 1.3, 3.1.1, 3.1.2, 3.1.3, and 3.2.",
    "claim_ids_remove": "Existing in-scope Claim ids explicitly removed by a module patch.",
    "claim_ids": "Runtime-owned internal Claim bindings; model-facing contracts never create or copy these ids.",
    "claim_type": "Semantic role of a Claim: project fact, interpretation, risk judgment, or recommendation.",
    "claims": (
        "Structured current-module Claims. A project_fact must cite at least one E-* "
        "source. Every Claim with footnote_required=true and non-empty source_ids must "
        "appear exactly once in its matching submodule narrative as "
        "[[CLAIM:<Claim.id>]]. Every Claim source must also be declared in source_ids."
    ),
    "claims_upsert": "Complete replacement records for in-scope Claims added or changed by a module patch.",
    "confidence": "Calibrated support level from 0 to 1 for the exact Claim wording.",
    "constraints": "Task-specific rules; they narrow work but do not redefine the submission schema.",
    "context_summary_refs": "Context-only summaries; never treat them as authoritative subject artifacts.",
    "coverage": "Structured record of what the reviewer actually checked in the current pass.",
    "coverage_ref": "Current-run immutable coverage artifact consumed by this exact authoring task.",
    "data_gap_analysis": "Final report section 3.1.3 body explaining evidence gaps, impact, and collection priority.",
    "decision": "Main exception decision for escalated findings only.",
    "decision_implication": "Why a supported cross-module relationship changes priority, sequencing, or residual risk.",
    "description": "Human-readable contract or Skill description matching the submitted content.",
    "discovery_summary": "Complete evidence-bounded discovery summary for the assigned scope.",
    "dispositions": "One answered or explicitly unresolved disposition for every exact inbox request.",
    "dimension_risk_analysis": "Final report section 3.1.2 body comparing risk dimensions and interactions.",
    "evidence_refs": "Existing current-run artifact or source refs that reproduce the submitted statement or verdict.",
    "evidence_ids": "Unique current-run E-* ids supporting the exact submitted scope.",
    "evidence_ref": "Current-run registered project-evidence artifact assigned to this task.",
    "finding_id": "Stable prior finding id copied exactly; do not restate or rename its contract.",
    "findings": "New immutable findings created from the current subject in this pass.",
    "findings_overview": "Final report section 1.2 body summarizing evidence-bounded assessment findings.",
    "footnote_required": "Whether final rendering must create a source footnote for this Claim.",
    "headers": "Ordered visible table column headings.",
    "id": "Stable identifier unique within the artifact type and current workflow.",
    "impact": "Whether the finding blocks completion or is advisory but still requires disposition.",
    "improvement_action_plan": "Final report section 3.2 body with owners, dependencies, acceptance, and residual risk.",
    "inline_context": "Bounded inline context; authoritative inputs must instead be role-labelled in the input contract.",
    "input_refs": "Current-run artifacts explicitly assigned as inputs; their roles come from the input contract.",
    "kind": "Required exact discriminator for the one submission type allowed by the active task.",
    "knowledge_ref": "Module Knowledge reference assigned to the exact task scope.",
    "module_id": "Fixed report module id from 2.1 through 2.5.",
    "manifest_ref": "Current-run immutable project manifest assigned to this task.",
    "module_narratives": "Map containing exactly one complete approved narrative for each fixed report module.",
    "module_tasks": "Exactly one typed task envelope for each requested specialist.",
    "cross_context": "Aggregate-existing mode must keep this field null; it must never fabricate Cross decisions.",
    "cross_decision": "Terminal typed CrossDecisionPack view with all five module ids and approved synthesis inputs.",
    "version": "Monotonic schema version for the persisted CrossDecisionPack contract.",
    "cross_review_completion_ref": "Current-run immutable completion record proving Cross review closed all module findings.",
    "cross_decision_pack_ref": "Current-run immutable CrossDecisionPack artifact reference consumed by Chief/Final.",
    "artifact_refs": "Exact current-run Cross completion refs retained for typed business identity.",
    "module_ids": "Exactly the five fixed report modules 2.1, 2.2, 2.3, 2.4, and 2.5.",
    "name": "Exact registered name required by the active artifact contract.",
    "new_findings": "Only genuinely new regression findings; never repeat required prior findings here.",
    "objective": "Concrete work objective for the assigned Agent and current stage.",
    "question": "Concrete cross-module question answerable by the exact target responsibility module.",
    "needed_for": "Why the interface response is needed by the requesting responsibility module.",
    "unresolved_reason": "Concrete evidence-bounded reason this interface request cannot be answered after the assigned checks.",
    "boundary": "Explicit transparent boundary an author must retain when an interface request is unresolved; it is not a block or user-escalation request.",
    "checked_evidence_ids": "E-* ids actually checked before declaring the unresolved interface boundary.",
    "queries_performed": "Bounded checks or queries performed before declaring an unresolved interface request.",
    "missing_fields": "Concrete fields or artifacts still missing after the unresolved checks.",
    "blocking": "Whether an unresolved response remains an explicit authoring or escalation boundary.",
    "observation": "Concrete current-subject defect, including its location and material consequence.",
    "owner_module_id": "The one fixed module owned by this Cross-owner lane; coverage, findings, and any writeback target must stay inside this owner scope.",
    "photo_ids": (
        "Runtime-owned current-run source-table photo ids; the workflow deterministically "
        "injects the complete set and the author should submit an empty list."
    ),
    "pending_correction_ref": (
        "Current-run correction state with the last raw submission candidate and exact "
        "validation errors for same-run recovery."
    ),
    "prior_result_ref": "Immediate prior subject or result version used as the revision baseline.",
    "protected_claim_ids": "Runtime-owned set of approved internal Claims; the chief editor never submits this field.",
    "rationale": "Evidence-based explanation for the plan or exception decision.",
    "narrative": "Complete reader-visible prose for this exact fixed taxonomy subsection.",
    "reason": "Reviewer-owned explanation for a verdict or workflow-owned failure result.",
    "regional_executive_summary": "Final report section 1.3 body grouped only by evidenced regions or responsibilities.",
    "related_module_ids": "Other fixed modules participating in the Cross relationship; exclude the owner module.",
    "required_change": "Observable author change required to address the finding within its target scope.",
    "residual_risks": "Transparent non-corrective limitations; never hide actionable findings here.",
    "reviewer_checks": "Semantic checks the same reviewer will apply before resolving the finding.",
    "revision": "Current subject revision number assigned by the workflow.",
    "revision_responses": "Exactly one author response for each assigned finding in a revision task.",
    "rewrite_part_ids": (
        "Existing durable prose parts explicitly authorized for rewrite by pending "
        "submission correction feedback."
    ),
    "risk_panorama": "Final report section 3.1.1 body organized by root causes and propagation capability.",
    "rows": "Ordered table rows; every row width must match headers.",
    "run_id": "Current immutable report run identifier.",
    "scope": "Exact product or workflow scope declared by the active submission contract.",
    "separator": "Short separator inserted between durable text parts during materialization.",
    "skill_id": "Canonical product Skill id affected by the evolution action.",
    "skills": (
        "Exactly fourteen complete template-derived Skills keyed by the assigned module-author, "
        "module-auditor, Chief chapter, and final-auditor identities. Cross is intentionally absent."
    ),
    "source_ids": (
        "Registered current-run source ids supporting the module, Claim, or table. "
        "For a project_fact Claim this list must contain at least one E-* id."
    ),
    "source_context": (
        "Bounded chapter-local upstream projection supplied by the workflow; it never "
        "contains the complete report or another chapter's prose."
    ),
    "source_refs": (
        "Current-run upstream artifacts authorized for this chapter lane; they do not "
        "expand its section scope."
    ),
    "submodule_id": "One fixed taxonomy submodule id.",
    "requester_submodule_id": "Legacy exact taxonomy subsection that owns the cross-module request; new requests are module-scoped.",
    "provisional_findings": "Evidence-bounded candidate findings retained for later authoring and review, not reviewer verdicts.",
    "submodule_ids": "Fixed taxonomy submodules actually covered by the review.",
    "submodule_narratives": (
        "Map of fixed submodule ids to their complete module-local prose. A long prose "
        "value may be the exact artifact_ref string returned by write_result_part; the "
        "submission tool materializes only refs from the active run, task, and revision."
    ),
    "section_bodies": "Runtime-assembled map of exactly the assigned Chapter 1, 3, or 4 section bodies.",
    "section_part_refs": "Deprecated alias; use part_refs for lane-local durable result refs.",
    "part_refs": "Runtime-owned lane-local durable result refs; Chapter 4 uses one special_topic_analysis ref for all 4.x sections.",
    "phase": "Exact lane lifecycle phase declared by the workflow: initial, revision, or recheck as allowed by this contract.",
    "required_findings": (
        "Immutable prior findings assigned to this exact review lane; recheck must answer "
        "all and only these finding ids."
    ),
    "section_ids": "Complete active section ids for this one chapter lane; no other chapter is in scope.",
    "chapter_id": "One parallel Chief/Final chapter lane: 1, 3, or active dynamic Chapter 4.",
    "base_subject_ref": "Exact current edited-report artifact that this lane patch applies to.",
    "subject_ref": "Exact current edited-report artifact visible to this lane only.",
    "summary": "Concise stage-specific explanation of the submitted action or author response.",
    "synthesis_inputs": "Supported Cross relationships for chief synthesis that need no further module writeback.",
    "special_topic_analysis": (
        "Optional dynamic Chapter 4 body. Submit it only when the active input contract "
        "contains special_topic_plan. Planned ### 4.n headings, order, and count must "
        "exactly match that plan; nested headings such as #### 4.1.1 are allowed only "
        "inside their matching planned parent."
    ),
    "special_topic_plan": (
        "Optional runtime-owned immutable Chapter 4 titles and brief requirements. It is "
        "absent when Inputs has no matching Markdown or that file is empty."
    ),
    "tables": "Traceable tables selected for the final report; provide only registered E-* evidence_ids and runtime derives internal bindings.",
    "target_section_ids": "Fixed final-report sections where the chief editor must act.",
    "target_changes": "One explicit required change and reviewer-check set for every targeted final-report section.",
    "target_section_id": "One exact summary/conclusion section in 1.1, 1.2, 1.3, 3.1.1, 3.1.2, 3.1.3, or 3.2; Chapter 2 and Chapter 4 are forbidden.",
    "target_submodule_id": "Exact fixed taxonomy subsection where the author must act or that owns a cross-module response.",
    "target_submodule_ids": "Fixed module-local submodules authorized for review or revision.",
    "target_module_id": "Fixed responsibility module containing the exact target taxonomy subsection.",
    "status": "Stage-specific typed status; use exactly one value allowed by the active contract.",
    "task_id": "Stable task identifier within the current run.",
    "text": "Exact Claim wording that will be protected and traced to declared sources.",
    "title": "Human-readable title of the report or table.",
    "unresolved": "Whether the Claim remains explicitly unresolved in its current wording.",
    "unresolved_editorial_issues": "Chief-editor limitations that remain visible and are not hidden findings.",
    "unresolved_questions": "Concrete module evidence questions that remain unresolved and visible.",
    "verdict": "Original reviewer decision for one immutable finding: resolved, open, or escalate.",
    "verdicts": "Exactly one reviewer verdict for every required prior finding id.",
    "verification_method": "Joint acceptance or monitoring method for a supported Cross synthesis input.",
    "finding_ids": "Escalated immutable finding ids covered by one Main exception decision.",
}


KIND_SEMANTIC_RULES: dict[str, list[str]] = {
    "module_submission": [
        "kind is required and must equal module_submission.",
        "Before committing, write every fixed submodule with write_result_part and pass "
        "its registered E-* ids through evidence_ids.",
        "The final commit contains only the fields declared by this schema; runtime owns all internal bindings.",
    ],
    "module_revision_submission": [
        "kind is required and must equal module_revision_submission.",
        "revision must equal base_revision plus one.",
        "Write each assigned changed submodule with write_result_part and evidence_ids.",
        "The final commit contains only the fields declared by this schema; runtime derives the scoped patch.",
    ],
    "module_review_finding_submission": [
        "Identify findings only by fixed target_submodule_id.",
    ],
    "module_review_verdict_submission": [
        "Recheck fixed submodules and finding_ids.",
    ],
    "cross_review_finding_submission": [
        "Locate writeback by owner module and fixed submodule ids.",
    ],
    "cross_review_verdict_submission": [
        "Recheck module locations and finding_ids.",
    ],
    "cross_owner_finding_submission": [
        "owner_module_id is the one fixed module owned by this Cross-owner lane; coverage.module_id must match it.",
        "Initial findings may be empty when this owner has no actionable cross-module defect.",
        "synthesis_inputs are allowed only for supported relationships that require no owner writeback; related modules are read-only context.",
        "Any finding must target only fixed taxonomy subsections inside owner_module_id; never create a finding for another module.",
    ],
    "cross_owner_verdict_submission": [
        "owner_module_id is the one fixed module owned by this Cross-owner lane; coverage.module_id must match it.",
        "Return exactly one verdict for every required prior owner finding id; an empty verdicts list is valid only when no prior finding is assigned.",
        "new_findings may contain only genuine regressions introduced by the current owner revision; do not repeat required prior findings.",
        "Recheck may return verdicts and genuine new regressions; runtime carries immutable initial synthesis artifacts forward.",
    ],
    "edited_report_submission": [
        "Runtime preserves all approved module bindings automatically.",
        "A table declares registered E-* evidence_ids only; runtime derives its internal bindings.",
    ],
    "chief_revision_submission": [
        "Write only the assigned seven summary/conclusion sections (1.1, 1.2, 1.3, 3.1.1, 3.1.2, 3.1.3, 3.2) with write_result_part.",
        "Do not resubmit the full report, Chapter 2, Cross dispositions, tables, photos, or editorial metadata.",
        "Runtime materializes the assigned result parts and deterministically inherits every unassigned field.",
    ],
    "chief_chapter_lane_submission": [
        "Return only this lane's section_ids and part_refs; the reducer assembles Chapters 1, 3, and optional 4 and inherits Chapter 2 and metadata.",
        "Never include a full report, other chapter bodies, tables, photos, or Cross state.",
    ],
    "chief_chapter_lane_revision_submission": [
        "Return only changed section_ids and matching part_refs for this chapter lane; "
        "do not copy unchanged sections from the input contract's complete lane scope.",
        "section_ids and revision_responses[].changed_target_ids use numeric section "
        "ids, while write_result_part.part_id and part_refs keys use body names: "
        "1.1=assessment_background, 1.2=findings_overview, 1.3=regional_executive_summary, "
        "3.1.1=risk_panorama, 3.1.2=dimension_risk_analysis, 3.1.3=data_gap_analysis, "
        "3.2=improvement_action_plan. Chapter 4 uses one complete special_topic_analysis part.",
        "Never copy another chapter or the full edited report.",
    ],
    "final_chapter_lane_finding_submission": [
        "Check only checked_section_ids in one chapter lane; findings must target those sections.",
        "Chapter 4 is valid only when an active Inputs special-topic plan created the lane.",
    ],
    "final_chapter_lane_verdict_submission": [
        "Return verdicts for required findings in this lane and only genuinely new lane-local regressions.",
        "Do not repeat findings from another chapter lane or submit a whole-report verdict.",
    ],
}


KIND_SUMMARIES: dict[str, str] = {
    "module_submission": (
        "A small commit for specialist-authored result parts; runtime materializes prose and evidence bindings."
    ),
    "module_revision_submission": (
        "A small in-scope revision commit for already saved result parts and author responses."
    ),
    "template_skill_submission": (
        "Complete project-scoped module and role Skills distilled from the template without copied facts."
    ),
    "module_review_finding_submission": (
        "Initial module-local review coverage and new immutable findings."
    ),
    "module_review_verdict_submission": (
        "Module reviewer recheck: one verdict per required finding plus only new regressions."
    ),
    "cross_review_finding_submission": (
        "Initial five-module interface coverage, module-writeback findings, and supported synthesis inputs."
    ),
    "cross_review_verdict_submission": (
        "Same Cross reviewer recheck: one verdict per required Cross finding and updated synthesis inputs."
    ),
    "cross_owner_finding_submission": (
        "Initial Cross-owner result for one fixed module: scoped coverage, owner-local findings, and optional supported synthesis inputs."
    ),
    "cross_owner_verdict_submission": (
        "Cross-owner recheck for one fixed module: exact required-finding verdicts plus genuine owner-local regressions."
    ),
    "final_review_finding_submission": (
        "Initial final-report coverage, immutable editorial findings, and transparent residual risks."
    ),
    "final_review_verdict_submission": (
        "Same final reviewer recheck: one verdict per required finding and only new regressions."
    ),
    "workflow_decision_submission": (
        "Main exception handling for a genuine reviewer escalation, user dependency, or stop decision."
    ),
    "edited_report_submission": (
        "Chief-editor report structure preserving approved module prose and producing fixed synthesis sections."
    ),
    "chief_revision_submission": (
        "A compact chief-editor patch for assigned Chapter 1, 3, or 4 result parts and finding responses."
    ),
    "chief_chapter_lane_submission": (
        "Initial Chief output for one chapter lane; reducer assembles the lane patches into the edited report."
    ),
    "chief_chapter_lane_revision_submission": (
        "Compact Chief revision output for one chapter lane only."
    ),
    "final_chapter_lane_finding_submission": (
        "Initial Final findings for one chapter lane only."
    ),
    "final_chapter_lane_verdict_submission": (
        "Final recheck verdicts and new findings for one chapter lane only."
    ),
    "skill_evolution_submission": (
        "Typed product Skill evolution outcome with traceable artifacts."
    ),
}


def _module_example() -> dict[str, Any]:
    return {
        "kind": "module_submission",
        "module_id": "2.1",
        "unresolved_questions": [],
        "revision": 0,
        "revision_responses": [],
    }


def _module_revision_example() -> dict[str, Any]:
    return {
        "kind": "module_revision_submission",
        "module_id": "2.1",
        "base_revision": 0,
        "revision": 1,
        "unresolved_questions": [],
        "revision_responses": [
            {
                "finding_id": "M-2.1-initial-r0-001",
                "action": "implemented",
                "summary": "已在 2.1.1 中统一对象称谓，并核对相关论断的证据边界。",
                "changed_target_ids": ["2.1.1"],
            }
        ],
    }


def _template_skill_example() -> dict[str, Any]:
    description = (
        "从模板中提炼报告写作、专业分析、跨模块综合、视觉组织和质量校核方法，"
        "仅约束表达与推理，不迁移任何客户事实。"
    )
    return {
        "kind": "template_skill_submission",
        "name": "report-template-role-skills",
        "skills": {
            skill_id: (
                f"---\nname: report-template-{skill_id}\ndescription: {description}\n---\n"
                f"# {skill_id} 模板方法\n\n"
                + "按本职责组织证据边界、分析、行动与可观察质量检查；不提供项目事实。" * 8
            )
            for skill_id in TEMPLATE_ROLE_SKILL_IDS
        },
    }


def _edited_report_example() -> dict[str, Any]:
    modules = {module_id: f"[[APPROVED_MODULE:{module_id}]]" for module_id in REPORT_TAXONOMY}
    return {
        "kind": "edited_report_submission",
        "title": "示例配电安全专家咨询报告",
        "assessment_background": "基于已批准模块说明评估范围、证据边界和适用限制。",
        "findings_overview": "归纳已批准模块中的主要发现，不改变其事实与风险语义。",
        "regional_executive_summary": "当前证据未定义地理区域，按责任边界归纳优先事项。",
        "module_narratives": modules,
        "risk_panorama": "归纳当前模块成果支持的主要风险及其判断依据。",
        "dimension_risk_analysis": "比较五个专业维度的主导风险和管理含义。",
        "data_gap_analysis": "归并证据缺口并说明其判断影响和补证优先级。",
        "improvement_action_plan": "按依赖顺序列出责任接口、行动、验收指标和剩余风险。",
        "special_topic_plan": None,
        "special_topic_analysis": None,
        "tables": [],
        "photo_ids": [],
        "unresolved_editorial_issues": [],
        "revision_responses": [],
    }


def _chief_revision_example() -> dict[str, Any]:
    return {
        "kind": "chief_revision_submission",
        "base_subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "revision": 1,
        "revision_responses": [
            {
                "finding_id": "F-001",
                "action": "implemented",
                "summary": "已按最终审查要求修订目标小节，未改动第二章和其他未分配字段。",
                "changed_target_ids": ["4"],
            }
        ],
    }


def _cross_owner_coverage_example(module_id: str = "2.1") -> dict[str, Any]:
    return {
        "module_id": module_id,
        "checked_dimensions": [
            "terminology",
            "facts",
            "risk_levels",
            "dependencies",
            "propagation",
            "joint_verification",
        ],
    }


KIND_EXAMPLES: dict[str, dict[str, Any]] = {
    "module_submission": _module_example(),
    "module_revision_submission": _module_revision_example(),
    "template_skill_submission": _template_skill_example(),
    "edited_report_submission": _edited_report_example(),
    "chief_revision_submission": _chief_revision_example(),
    "module_review_finding_submission": {
        "kind": "module_review_finding_submission",
        "coverage": {"submodule_ids": ["2.3.1"]},
        "findings": [
            {
                "id": "M-2.3-001",
                "target_submodule_id": "2.3.1",
                "category": "factual_accuracy",
                "impact": "blocking",
                "observation": "当前 Claim 与同一小节正文中的设备称谓不一致，读者无法确认对象。",
                "evidence_refs": ["Work/runs/report-example/modules/2.3-r0.json"],
                "required_change": "统一正文和 Claim 中的设备称谓，并保持来源语义不变。",
                "reviewer_checks": ["正文与结构化 Claim 指向同一设备对象"],
            }
        ],
    },
    "module_review_verdict_submission": {
        "kind": "module_review_verdict_submission",
        "coverage": {"submodule_ids": ["2.3.1"]},
        "verdicts": [
            {
                "finding_id": "M-2.3-001",
                "verdict": "resolved",
                "reason": "当前正文和 Claim 已使用同一设备称谓，且来源语义未改变。",
                "evidence_refs": ["Work/runs/report-example/modules/2.3-r1.json"],
            }
        ],
        "new_findings": [],
    },
    "cross_review_finding_submission": {
        "kind": "cross_review_finding_submission",
        "coverage": [
            {
                "module_id": module_id,
                "checked_dimensions": [
                    "terminology",
                    "facts",
                    "risk_levels",
                    "dependencies",
                    "propagation",
                    "joint_verification",
                ],
            }
            for module_id in REPORT_TAXONOMY
        ],
        "findings": [],
        "synthesis_inputs": [],
    },
    "cross_review_verdict_submission": {
        "kind": "cross_review_verdict_submission",
        "coverage": [
            {
                "module_id": module_id,
                "checked_dimensions": [
                    "terminology",
                    "facts",
                    "risk_levels",
                    "dependencies",
                    "propagation",
                    "joint_verification",
                ],
            }
            for module_id in REPORT_TAXONOMY
        ],
        "verdicts": [
            {
                "finding_id": "X-001",
                "verdict": "resolved",
                "reason": "责任模块已写入作用机制、行动依赖和联合验证，且未改变协作模块结论。",
                "evidence_refs": ["Work/runs/report-example/modules/2.2-r1.json"],
            }
        ],
        "new_findings": [],
        "synthesis_inputs": [],
    },
    "cross_owner_finding_submission": {
        "kind": "cross_owner_finding_submission",
        "owner_module_id": "2.1",
        "coverage": _cross_owner_coverage_example(),
        "findings": [],
        "synthesis_inputs": [],
    },
    "cross_owner_verdict_submission": {
        "kind": "cross_owner_verdict_submission",
        "owner_module_id": "2.1",
        "coverage": _cross_owner_coverage_example(),
        "verdicts": [],
        "new_findings": [],
    },
    "final_review_finding_submission": {
        "kind": "final_review_finding_submission",
        "checked_section_ids": ["1.1", "1.2"],
        "findings": [],
        "residual_risks": [],
    },
    "final_review_verdict_submission": {
        "kind": "final_review_verdict_submission",
        "checked_section_ids": ["1.1", "1.2"],
        "verdicts": [
            {
                "finding_id": "F-001",
                "verdict": "resolved",
                "reason": "当前成稿已恢复批准内容并保留每条 Claim 的唯一引用标记。",
                "evidence_refs": ["Work/runs/report-example/edited-revisions/chief-r1.json"],
            }
        ],
        "new_findings": [],
        "residual_risks": [],
    },
    "chief_chapter_lane_submission": {
        "kind": "chief_chapter_lane_submission",
        "run_id": "report-example",
        "chapter_id": "1",
        "section_ids": ["1.1", "1.2", "1.3"],
        "part_refs": {
            "assessment_background": "Work/runs/report-example/results/chief-c1-1.1.md",
            "findings_overview": "Work/runs/report-example/results/chief-c1-1.2.md",
            "regional_executive_summary": "Work/runs/report-example/results/chief-c1-1.3.md",
        },
        "revision": 0,
    },
    "chief_chapter_lane_revision_submission": {
        "kind": "chief_chapter_lane_revision_submission",
        "run_id": "report-example",
        "base_subject_ref": "Work/runs/report-example/edited-revisions/chief-r0.json",
        "chapter_id": "3",
        "revision": 1,
        "section_ids": ["3.2"],
        "part_refs": {
            "improvement_action_plan": "Work/runs/report-example/results/chief-c3-r1-3.2.md"
        },
        "revision_responses": [{
            "finding_id": "F-3-001",
            "action": "implemented",
            "summary": "已按指定 finding 补充本小节的行动责任、事实边界与核验方法。",
            "changed_target_ids": ["3.2"],
        }],
    },
    "final_chapter_lane_finding_submission": {
        "kind": "final_chapter_lane_finding_submission",
        "run_id": "report-example",
        "chapter_id": "1",
        "checked_section_ids": ["1.1", "1.2", "1.3"],
        "findings": [
            {
                "id": "F-1-001",
                "target_section_ids": ["1.1"],
                "target_changes": [
                    {
                        "target_section_id": "1.1",
                        "required_change": (
                            "补充该结论对应的证据边界，并明确尚待确认的项目事实。"
                        ),
                        "reviewer_checks": [
                            "修改后能够从结论直接追溯到当前运行的证据。"
                        ],
                    }
                ],
                "category": "traceability",
                "impact": "blocking",
                "observation": (
                    "当前结论没有清楚区分已由证据支持的事实与仍待项目确认的判断。"
                ),
                "evidence_refs": [
                    "Work/runs/report-example/edited-revisions/chief-r0.json"
                ],
            }
        ],
        "residual_risks": [],
    },
    "final_chapter_lane_verdict_submission": {
        "kind": "final_chapter_lane_verdict_submission",
        "run_id": "report-example",
        "chapter_id": "3",
        "checked_section_ids": ["3.1.1", "3.1.2", "3.1.3", "3.2"],
        "verdicts": [],
        "new_findings": [],
        "residual_risks": [],
    },
    "workflow_decision_submission": {
        "kind": "workflow_decision_submission",
        "decision": "request_user",
        "rationale": "审查者已升级一个需要用户确认的项目事实。",
        "finding_ids": ["M-2.3-001"],
    },
    "skill_evolution_submission": {
        "kind": "skill_evolution_submission",
        "scope": "product",
        "action": "feedback_recorded",
        "skill_id": "example-skill",
        "artifact_ids": ["artifact-example"],
        "summary": "已记录可追溯反馈，尚未发布 Skill 变更。",
    },
}


def submission_model(kind: str) -> type:
    try:
        return SUBMISSION_INPUT_TYPES[kind]
    except KeyError as exc:
        raise KeyError(f"unknown submission contract kind: {kind}") from exc


def _enrich_properties(
    node: Any,
    *,
    definitions: dict[str, Any] | None = None,
) -> None:
    if not isinstance(node, dict):
        return
    definitions = definitions or node.get("$defs", {})

    def resolve(prop: dict[str, Any]) -> dict[str, Any]:
        reference = prop.get("$ref")
        if isinstance(reference, str) and reference.startswith("#/$defs/"):
            target = definitions.get(reference.rsplit("/", 1)[-1])
            return target if isinstance(target, dict) else prop
        return prop

    def structured_shape(prop: dict[str, Any]) -> str | None:
        resolved = resolve(prop)
        if resolved.get("type") in {"array", "object"}:
            return str(resolved["type"])
        candidates = {
            resolve(branch).get("type")
            for key in ("anyOf", "oneOf")
            for branch in resolved.get(key, [])
            if isinstance(branch, dict)
        }
        structured = candidates & {"array", "object"}
        return next(iter(structured)) if len(structured) == 1 else None

    for name, prop in node.get("properties", {}).items():
        if not isinstance(prop, dict):
            continue
        if not prop.get("description"):
            guidance = FIELD_GUIDANCE.get(name)
            if guidance:
                prop["description"] = guidance
        shape = structured_shape(prop)
        if shape is not None:
            native_guidance = (
                f"Use a native JSON {shape}; never submit this field as a "
                "JSON-encoded string."
            )
            current = str(prop.get("description") or "").strip()
            if native_guidance not in current:
                prop["description"] = f"{current} {native_guidance}".strip()
        _enrich_properties(prop, definitions=definitions)
    items = node.get("items")
    if isinstance(items, dict):
        _enrich_properties(items, definitions=definitions)
    for key in ("anyOf", "oneOf", "allOf"):
        for branch in node.get(key, []):
            _enrich_properties(branch, definitions=definitions)
    for value in node.get("$defs", {}).values():
        _enrich_properties(value, definitions=definitions)


def submission_schema(kind: str) -> dict[str, Any]:
    """Return the exact provider schema enriched with model-visible semantics."""

    schema = deepcopy(TypeAdapter(submission_model(kind)).json_schema())
    rules = KIND_SEMANTIC_RULES.get(kind, [])
    schema["description"] = " ".join([KIND_SUMMARIES[kind], *(f"Rule: {rule}" for rule in rules)])
    required = list(schema.get("required", []))
    if "kind" in schema.get("properties", {}) and "kind" not in required:
        required.insert(0, "kind")
    schema["required"] = required
    example = KIND_EXAMPLES.get(kind)
    if example is not None:
        schema["examples"] = [deepcopy(example)]
    _enrich_properties(schema)
    return schema


def _render_fields(
    properties: dict[str, Any],
    *,
    required: set[str],
    prefix: str = "",
) -> list[str]:
    lines: list[str] = []
    for name, prop in properties.items():
        path = f"{prefix}.{name}" if prefix else name
        requirement = "required" if name in required else "optional"
        description = str(prop.get("description") or "").strip()
        lines.append(f"- {path} ({requirement}): {description}")
    return lines


def render_submission_schema_contract(kind: str, schema: dict[str, Any]) -> str:
    """Render one exact provider schema as task-visible first-submit guidance."""

    lines = [
        f"submission_kind: {kind}",
        f"purpose: {KIND_SUMMARIES[kind]}",
        "semantic_rules:",
        *(f"- {rule}" for rule in KIND_SEMANTIC_RULES.get(kind, [])),
        "top_level_fields:",
        *_render_fields(
            schema.get("properties", {}),
            required=set(schema.get("required", [])),
        ),
    ]
    for definition_name, definition in schema.get("$defs", {}).items():
        properties = definition.get("properties", {})
        if not properties:
            continue
        lines.append(f"nested_type {definition_name}:")
        lines.extend(
            _render_fields(
                properties,
                required=set(definition.get("required", [])),
                prefix=definition_name,
            )
        )
    example = schema.get("examples", [None])[0]
    if example is not None:
        lines.extend(
            [
                "valid_example:",
                json.dumps(example, ensure_ascii=False, separators=(",", ":")),
            ]
        )
    return "\n".join(lines)


def render_submission_contract(kind: str) -> str:
    """Render the generic provider schema for diagnostics and static callers."""

    return render_submission_schema_contract(kind, submission_schema(kind))


def undescribed_property_paths(schema: dict[str, Any]) -> list[str]:
    """Return every provider-visible property still lacking semantics."""

    missing: list[str] = []

    def visit(node: Any, prefix: str = "") -> None:
        if not isinstance(node, dict):
            return
        for name, prop in node.get("properties", {}).items():
            path = f"{prefix}.{name}" if prefix else name
            if not isinstance(prop, dict) or not str(prop.get("description") or "").strip():
                missing.append(path)
        for name, value in node.get("$defs", {}).items():
            visit(value, f"$defs.{name}")

    visit(schema)
    return missing
