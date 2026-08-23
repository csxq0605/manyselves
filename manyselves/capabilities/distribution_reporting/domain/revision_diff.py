"""Describe Agent revisions without making a business acceptance decision."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission


def build_revision_diff(previous: ModuleSubmission, revised: ModuleSubmission) -> dict:
    """Return a serializable semantic change summary for reviewers and the lead Agent."""

    changed_narratives = [
        submodule_id
        for submodule_id in REPORT_TAXONOMY[previous.module_id].submodules
        if previous.submodule_narratives[submodule_id]
        != revised.submodule_narratives[submodule_id]
    ]
    previous_claims = {claim.id: claim.model_dump(mode="json") for claim in previous.claims}
    revised_claims = {claim.id: claim.model_dump(mode="json") for claim in revised.claims}
    changed_claims = sorted(
        claim_id
        for claim_id in previous_claims.keys() | revised_claims.keys()
        if previous_claims.get(claim_id) != revised_claims.get(claim_id)
    )
    return {
        "module_id": revised.module_id,
        "from_revision": previous.revision,
        "to_revision": revised.revision,
        "changed_submodule_narratives": changed_narratives,
        "changed_claim_ids": changed_claims,
        "source_ids_added": sorted(set(revised.source_ids) - set(previous.source_ids)),
        "source_ids_removed": sorted(set(previous.source_ids) - set(revised.source_ids)),
    }
