"""Shared worker helpers for stable claims and Markdown output."""

import hashlib

from ..models import Claim, ClaimKind


def make_claim(
    *,
    module_id: str,
    submodule_id: str,
    kind: ClaimKind,
    text: str,
    evidence_ids: list[str],
    skill_ids: list[str],
    unverified: bool = False,
) -> Claim:
    identity = "\0".join(
        (module_id, submodule_id, kind.value, text, *evidence_ids, *skill_ids)
    ).encode()
    return Claim(
        id=f"claim-{hashlib.sha256(identity).hexdigest()[:20]}",
        module_id=module_id,
        submodule_id=submodule_id,
        kind=kind,
        text=text,
        evidence_ids=evidence_ids,
        skill_ids=skill_ids,
        unverified=unverified,
    )
