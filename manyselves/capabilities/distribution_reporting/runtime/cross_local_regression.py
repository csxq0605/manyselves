"""Capability-owned Cross-triggered local regression input construction."""

from __future__ import annotations

import hashlib
from pathlib import Path

from manyselves.capabilities.distribution_reporting.domain.revision_diff import (
    build_revision_diff,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
    CrossReviewFinding,
    ModuleSubmission,
)
from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
    ModuleRevisionDiff,
    ReviewCompletionRecord,
)
from manyselves.capabilities.distribution_reporting.runtime.models.review import (
    ModuleLocalRegressionContext,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def build_cross_owner_local_regression_context(
    *,
    workspace: Path,
    store: ReportingStore,
    run_id: str,
    owner_module_id: str,
    reviewed_baseline: ModuleSubmission,
    revised: ModuleSubmission,
    findings: list[CrossReviewFinding],
    review_round: int,
    prior_completion_ref: str | None,
) -> tuple[ModuleLocalRegressionContext, set[str]]:
    """Build the existing Cross-triggered module regression input once.

    The completion binding, artifact paths, deterministic revision diff, and
    statement reference names mirror the established Reporting lifecycle.  The
    caller provides the already-resolved prior completion ref explicitly so the
    helper does not depend on a mutable Reporting coordinator state object.
    """

    baseline_subject_ref = (
        f"Work/runs/{run_id}/modules/{owner_module_id}-r{reviewed_baseline.revision}.json"
    )
    if not prior_completion_ref:
        raise ValueError(
            f"Cross local regression requires prior module review: {owner_module_id}"
        )
    try:
        prior_completion = ReviewCompletionRecord.model_validate_json(
            (Path(workspace) / prior_completion_ref).read_text(encoding="utf-8")
        )
    except (OSError, ValueError) as exc:
        raise ValueError(
            f"Cross local regression prior completion is unreadable: {owner_module_id}"
        ) from exc
    if (
        prior_completion.lifecycle != "module"
        or prior_completion.run_id != run_id
        or baseline_subject_ref not in prior_completion.subject_refs
    ):
        raise ValueError(
            f"Cross local regression prior completion does not bind {owner_module_id}"
        )

    local_scope = {
        target_id
        for finding in findings
        for target_id in finding.target_submodule_ids
    }
    local_diff_ref = (
        f"Work/runs/{run_id}/reviews/module/cross-r{review_round}/"
        f"{owner_module_id}/trigger-diff-r{revised.revision}.json"
    )
    raw_local_diff = build_revision_diff(reviewed_baseline, revised)
    local_diff = ModuleRevisionDiff(
        module_id=raw_local_diff["module_id"],
        from_revision=raw_local_diff["from_revision"],
        to_revision=raw_local_diff["to_revision"],
        changed_submodule_narratives=raw_local_diff["changed_submodule_narratives"],
        changed_statement_refs=[
            "statement-" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]
            for value in raw_local_diff["changed_claim_ids"]
        ],
        evidence_ids_added=raw_local_diff["source_ids_added"],
        evidence_ids_removed=raw_local_diff["source_ids_removed"],
    )
    store.write_json(local_diff_ref, local_diff.model_dump(mode="json"))
    return (
        ModuleLocalRegressionContext(
            prior_review_completion_ref=prior_completion_ref,
            prior_review_completion=prior_completion,
            baseline_subject_ref=baseline_subject_ref,
            trigger_cross_findings=findings,
            trigger_revision_responses=revised.revision_responses,
            revision_diff_ref=local_diff_ref,
            revision_diff=local_diff,
        ),
        local_scope,
    )


__all__ = ["build_cross_owner_local_regression_context"]
