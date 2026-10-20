"""Capability-owned bounded delta projected to the module Auditor.

This is the existing compact recheck contract moved out of the legacy
Reporting lifecycle.  The digests describe unchanged content inside the
reviewer's assigned scope; they are not CAS keys or runtime gates.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from manyselves.capabilities.distribution_reporting.domain.revision_diff import (
    build_revision_diff,
)

from .models.agentic import ClaimRecord, ModuleSubmission
from .models.inputs import (
    ModuleRevisionDiff,
    ReviewClaimStatement,
    module_content_view,
    strip_runtime_claim_markers,
)


def _text_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _model_sha256(model: Any) -> str:
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
            "statement-"
            + hashlib.sha256(claim.id.encode("utf-8")).hexdigest()[:12]
        ),
        submodule_id=claim.submodule_id,
        text=claim.text,
        statement_type=claim.claim_type,
        origin=claim.origin,
        evidence_ids=claim.source_ids,
        confidence=None if claim.origin == "runtime_part_binding" else claim.confidence,
        unresolved=claim.unresolved,
    )


def build_module_recheck_delta(
    baseline: ModuleSubmission,
    current: ModuleSubmission,
    scope: set[str],
    view: set[str] | None = None,
) -> dict[str, object]:
    """Keep current finding-scope text self-contained; use the delta for focus.

    ``scope`` is the whole recheck scope the revision must stay inside.
    ``view`` narrows what this delta shows to one batch of that scope.
    """

    raw = build_revision_diff(baseline, current)
    changed_narratives = set(raw["changed_submodule_narratives"])
    changed_assessments = set(raw["changed_assessment_submodules"])
    if not changed_assessments.issubset(scope):
        raise ValueError("module recheck revision changed assessments outside reviewer scope")
    if not changed_narratives.issubset(scope):
        raise ValueError(
            "module recheck revision changed narratives outside reviewer finding "
            f"scope: {sorted(changed_narratives - scope)}"
        )
    baseline_claims = {claim.id: claim for claim in baseline.claims}
    current_claims = {claim.id: claim for claim in current.claims}
    changed_claim_ids = set(raw["changed_claim_ids"])
    all_changed_claim_ids = changed_claim_ids
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
        raise ValueError(
            "module recheck revision changed Claims outside reviewer finding "
            f"scope: {sorted(changed_claim_submodules - scope)}"
        )
    if view is not None:
        changed_narratives &= view
        changed_assessments &= view
        changed_claim_ids = {
            claim_id
            for claim_id in changed_claim_ids
            if any(
                claim is not None and claim.submodule_id in view
                for claim in (baseline_claims.get(claim_id), current_claims.get(claim_id))
            )
        }
        scope = set(view)
    current_statements = [
        _review_claim_statement(current_claims[claim_id])
        for claim_id in sorted(current_claims)
        if current_claims[claim_id].submodule_id in scope
    ]
    prior_statements = [
        _review_claim_statement(baseline_claims[claim_id])
        for claim_id in sorted(changed_claim_ids)
        if claim_id in baseline_claims
    ]
    changed_statement_refs = sorted(
        {
            _review_claim_statement(claim).statement_ref
            for claim_id in changed_claim_ids
            for claim in (current_claims.get(claim_id), baseline_claims.get(claim_id))
            if claim is not None
        }
    )
    changed_statement_ids = set(changed_statement_refs)
    subject_view = module_content_view(current, scope)
    unchanged_statement_sha256 = {
        statement.statement_ref: _model_sha256(statement)
        for statement in (
            _review_claim_statement(claim)
            for claim in current.claims
            if claim.submodule_id in scope and claim.id not in all_changed_claim_ids
        )
        if statement.statement_ref not in changed_statement_ids
    }
    revision_diff = ModuleRevisionDiff(
        module_id=current.module_id,
        from_revision=baseline.revision,
        to_revision=current.revision,
        changed_submodule_narratives=sorted(changed_narratives),
        changed_assessment_submodules=sorted(changed_assessments),
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


__all__ = ["build_module_recheck_delta"]
