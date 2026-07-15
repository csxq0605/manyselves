"""Bounded local claim replacement for audit failures."""

from collections.abc import Callable

from ..models import Claim, ModuleDraft, ReviewIssue
from ..workers.module_24 import render_module_24_markdown

RevisionFunction = Callable[[str, list[Claim], list[ReviewIssue]], list[Claim]]


class RevisionLimitError(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        super().__init__("local revision limit exceeded")


class RevisionRouter:
    def __init__(self, *, max_rounds: int = 2):
        if max_rounds < 1:
            raise ValueError("max_rounds must be at least 1")
        self.max_rounds = max_rounds

    def route(
        self,
        draft: ModuleDraft,
        issues: list[ReviewIssue],
        revise: RevisionFunction,
    ) -> ModuleDraft:
        blocking = [issue for issue in issues if issue.severity == "blocking"]
        if not blocking:
            return draft.model_copy(update={"approved": True})
        submodule_ids = sorted({issue.submodule_id for issue in blocking if issue.submodule_id})
        if draft.revision >= self.max_rounds:
            raise RevisionLimitError(
                {
                    "module_id": draft.module_id,
                    "submodule_ids": submodule_ids,
                    "round": draft.revision,
                    "issues": [issue.model_dump(mode="json") for issue in blocking],
                }
            )

        claims_by_submodule: dict[str, list[Claim]] = {}
        for claim in draft.claims:
            claims_by_submodule.setdefault(claim.submodule_id, []).append(claim)
        replacements = {
            submodule_id: revise(
                submodule_id,
                claims_by_submodule.get(submodule_id, []),
                [issue for issue in blocking if issue.submodule_id == submodule_id],
            )
            for submodule_id in submodule_ids
        }

        revised_claims: list[Claim] = []
        inserted: set[str] = set()
        for claim in draft.claims:
            if claim.submodule_id not in replacements:
                revised_claims.append(claim)
                continue
            if claim.submodule_id not in inserted:
                revised_claims.extend(replacements[claim.submodule_id])
                inserted.add(claim.submodule_id)
        for submodule_id in submodule_ids:
            if submodule_id not in inserted:
                revised_claims.extend(replacements[submodule_id])

        if draft.module_id != "2.4":
            raise ValueError("Phase A revision router only supports module 2.4")
        return draft.model_copy(
            update={
                "claims": revised_claims,
                "markdown": render_module_24_markdown(revised_claims),
                "revision": draft.revision + 1,
                "approved": False,
            }
        )
