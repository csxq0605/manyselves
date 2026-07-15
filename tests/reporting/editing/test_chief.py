import pytest

from autoreport.core.reporting.editing.chief import ChiefEditor, ProtectedClaimError
from autoreport.core.reporting.models import Claim, ClaimKind, ModuleDraft


def _draft() -> ModuleDraft:
    claims = [
        Claim(
            id="claim-fact",
            module_id="2.5",
            submodule_id="2.5.1",
            kind=ClaimKind.FACT,
            text="操作规程有效性=NG。",
            evidence_ids=["ev-sop"],
            skill_ids=["pds.module25.operations@1.0.0"],
        ),
        Claim(
            id="claim-action",
            module_id="2.5",
            submodule_id="2.5.1",
            kind=ClaimKind.RECOMMENDATION,
            text="修订操作规程并组织演练。",
            evidence_ids=["ev-sop"],
            skill_ids=["pds.module25.operations@1.0.0"],
        ),
    ]
    return ModuleDraft(
        module_id="2.5",
        markdown="draft",
        evidence_ids=["ev-sop"],
        claims=claims,
        approved=True,
    )


def test_chief_editor_builds_chapter_three_without_mutating_protected_claims() -> None:
    draft = _draft()

    result = ChiefEditor().compile([draft], [])

    assert result.protected_claim_ids == ["claim-fact", "claim-action"]
    assert result.risk_summary
    assert result.actions[0].claim_ids == ["claim-action"]
    assert result.actions[0].evidence_ids == ["ev-sop"]

    altered = draft.model_copy(deep=True)
    altered.claims[0].text = "操作规程有效性=OK。"
    with pytest.raises(ProtectedClaimError, match="claim-fact"):
        ChiefEditor.validate_protected_claims([draft], [altered])


def test_chief_editor_deduplicates_summary_text_but_merges_traceability() -> None:
    draft = _draft()
    duplicate = draft.claims[1].model_copy(
        update={"id": "claim-action-2", "evidence_ids": ["ev-sop-2"]}
    )
    draft.claims.append(duplicate)

    result = ChiefEditor().compile([draft], [])

    assert len(result.actions) == 1
    assert result.actions[0].claim_ids == ["claim-action", "claim-action-2"]
    assert result.actions[0].evidence_ids == ["ev-sop", "ev-sop-2"]
