"""Capability-owned handoff contract materialization for Delivery preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .storage import ReportingStore


def write_handoff_contracts(store: ReportingStore, state: dict[str, Any]) -> Path:
    """Persist the producer/consumer contract matrix for this exact run."""

    run_id = state["run_id"]
    contracts = [
        {
            "stage": "template-skill-read",
            "producer": "separate template-distiller action",
            "consumer": "module specialists/auditors, chief editor, and final auditor",
            "input": (
                "hash-verified Work/report-template-role-skills files selected by exact "
                "module/role identity and embedded whole in task inline_context"
            ),
            "output": (
                "complete identity-scoped reusable Skill guidance with fact-free examples; "
                "Cross intentionally receives no template Skill"
            ),
            "content_checks": [
                "only analysis, synthesis, visual, and quality-check methods",
                "no domain knowledge, standards/thresholds, project facts, or identifiers",
                "writing run never reads or distills the template DOCX",
            ],
        },
        {
            "stage": "dispatch",
            "producer": "main-agent",
            "consumer": "module-2.x-specialist",
            "input": "ModuleAuthoringInput + TaskEnvelope",
            "output": "ModuleSubmission",
            "content_checks": [
                "fixed module and submodule taxonomy",
                "all Claim source_ids declared by module",
                "all sources exist in SourceLedger",
            ],
        },
        {
            "stage": "module-validation",
            "producer": "module-2.x-specialist + deterministic validator + evidence-auditor",
            "consumer": "module specialist and downstream workflow",
            "input": "ModuleReviewInput + exact ModuleSubmission + SourceLedger",
            "output": (
                "ValidationReport + ModuleReviewFinding + RevisionResponse + "
                "ResolutionVerdict + ReviewCompletionRecord"
            ),
            "content_checks": [
                "module_id and fixed taxonomy match",
                "every Claim source_id exists in SourceLedger",
                "typed module result is persisted before advancing",
                "structural checks never substitute for the module auditor's semantic judgment",
                "module quality, gaps, evidence validity, inference boundaries, and action closure are judged by the module auditor",
                "every finding triggers an explicit author response and same-reviewer verdict",
                "Main enters only for reviewer verdict=escalate",
            ],
        },
        {
            "stage": "cross-review",
            "producer": "five independently reviewed module pipelines",
            "consumer": "cross-module-reviewer",
            "input": "CrossReviewInput with five exact ModuleSubmission artifacts",
            "output": (
                "coverage + CrossReviewFinding + CrossSynthesisInput + "
                "original-reviewer ResolutionVerdict"
            ),
            "content_checks": [
                "cross-module terminology, facts, risk levels, dependencies, propagation, and joint verification only",
                "no routine re-audit of module-local prose, evidence sufficiency, or image binding",
                "owner specialist writes back each finding",
                "module auditor checks only local regression",
                "the original Cross reviewer alone closes Cross findings",
            ],
        },
        {
            "stage": "synthesis",
            "producer": "chief-editor",
            "consumer": "main-agent",
            "input": (
                "ChiefEditorInput + project Evidence/photo manifest; Claim/Source ledgers "
                "remain runtime-only"
            ),
            "output": "EditedReportSubmission + canonical Markdown",
            "content_checks": [
                "exactly modules 2.1-2.5",
                "all fixed submodule ids and titles retained",
                "every approved submodule narrative is deterministically preserved verbatim",
                "only the currently defined Chapter 1 and Chapter 3 sections are authored",
                "dynamic Chapter 4 headings and requirements exactly match the immutable Inputs plan",
                "approved Claim semantics protected",
                "approved Claim markers preserved exactly once",
            ],
        },
        {
            "stage": "final-review",
            "producer": "chief-editor",
            "consumer": "chief-editor-auditor",
            "input": "FinalReviewInput with exact EditedReportSubmission and canonical Markdown",
            "output": (
                "FinalReviewFinding + RevisionResponse + ResolutionVerdict + "
                "ReviewCompletionRecord"
            ),
            "content_checks": [
                "all currently defined final sections checked",
                "approved module prose and Claim semantics retained",
                "only current report sections are reviewed; deleted legacy sections are not reconstructed",
                "citation, table, image, action, and residual-risk presentation ready for delivery",
                "every finding triggers scoped chief-editor response and original-reviewer verdict",
            ],
        },
        {
            "stage": "render",
            "producer": "final review completion record",
            "consumer": "deterministic DOCX renderer",
            "input": "RenderRequest(source_markdown_ref, template_ref, output_ref)",
            "output": "RenderResult + readable DOCX",
            "content_checks": [
                "canonical Markdown exists and is non-empty",
                "renderer reports completed for the current run",
                "delivery completion reports delivered for the current run",
            ],
        },
    ]
    return store.write_json(f"Work/runs/{run_id}/handoff-contracts.json", contracts)


__all__ = ["write_handoff_contracts"]
