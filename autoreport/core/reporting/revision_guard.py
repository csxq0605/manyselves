"""Deterministic protection for submodule-scoped Agent revisions."""

from __future__ import annotations

from .agentic_models import ModuleSubmission
from .taxonomy import REPORT_TAXONOMY


class RevisionGuard:
    """Reject changes outside the exact submodules named by review issues."""

    @staticmethod
    def validate(
        previous: ModuleSubmission,
        revised: ModuleSubmission,
        target_submodules: set[str],
    ) -> None:
        if revised.module_id != previous.module_id:
            raise ValueError("revision cannot change module_id")
        if revised.revision != previous.revision + 1:
            raise ValueError("revision must increment by exactly one")
        fixed = set(REPORT_TAXONOMY[previous.module_id].submodules)
        if not target_submodules or not target_submodules <= fixed:
            raise ValueError("revision targets must be fixed submodules of the module")

        protected = fixed - target_submodules
        for submodule_id in sorted(protected):
            if (
                previous.submodule_narratives[submodule_id]
                != revised.submodule_narratives[submodule_id]
            ):
                raise ValueError(f"revision changed protected submodule {submodule_id} narrative")

            previous_claims = sorted(
                (
                    claim.model_dump(mode="json")
                    for claim in previous.claims
                    if claim.submodule_id == submodule_id
                ),
                key=lambda claim: claim["id"],
            )
            revised_claims = sorted(
                (
                    claim.model_dump(mode="json")
                    for claim in revised.claims
                    if claim.submodule_id == submodule_id
                ),
                key=lambda claim: claim["id"],
            )
            if previous_claims != revised_claims:
                raise ValueError(
                    f"revision changed protected submodule {submodule_id} claims or sources"
                )
