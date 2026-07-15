from autoreport.core.reporting.models import Claim, ClaimKind, ModuleDraft, ReviewIssue
from autoreport.core.reporting.review.revisions import (
    RevisionLimitError,
    RevisionRouter,
)


def _claim(claim_id: str, submodule_id: str, text: str) -> Claim:
    return Claim(
        id=claim_id,
        module_id="2.4",
        submodule_id=submodule_id,
        kind=ClaimKind.CONCLUSION,
        text=text,
        evidence_ids=[f"ev-{submodule_id}"],
        skill_ids=["pds.module24.installation@1.0.0"],
    )


def test_revision_router_replaces_only_responsible_submodule() -> None:
    cable = _claim("claim-cable", "2.4.2.5", "电缆状态异常。")
    grounding = _claim("claim-ground", "2.4.2.2", "接地异常。")
    draft = ModuleDraft(
        module_id="2.4",
        markdown="old",
        evidence_ids=["ev-2.4.2.5", "ev-2.4.2.2"],
        claims=[cable, grounding],
    )
    issue = ReviewIssue(
        module_id="2.4",
        submodule_id="2.4.2.2",
        claim_id="claim-ground",
        kind="wording",
        message="改为待复核",
        severity="blocking",
    )

    revised = RevisionRouter(max_rounds=2).route(
        draft,
        [issue],
        lambda submodule_id, claims, issues: [
            _claim("claim-ground-v2", submodule_id, "接地状态待复核。")
        ],
    )

    assert next(c for c in revised.claims if c.submodule_id == "2.4.2.5") == cable
    assert next(c for c in revised.claims if c.submodule_id == "2.4.2.2").id == "claim-ground-v2"
    assert revised.revision == 1
    assert "接地状态待复核" in revised.markdown


def test_revision_router_escalates_after_two_rounds() -> None:
    draft = ModuleDraft(
        module_id="2.4",
        markdown="old",
        evidence_ids=["ev-2.4.2.2"],
        claims=[_claim("claim-ground", "2.4.2.2", "接地异常。")],
        revision=2,
    )
    issue = ReviewIssue(
        module_id="2.4",
        submodule_id="2.4.2.2",
        kind="wording",
        message="仍未通过",
        severity="blocking",
    )

    try:
        RevisionRouter(max_rounds=2).route(draft, [issue], lambda *_: [])
    except RevisionLimitError as exc:
        assert exc.payload["module_id"] == "2.4"
        assert exc.payload["submodule_ids"] == ["2.4.2.2"]
    else:
        raise AssertionError("expected revision escalation")
