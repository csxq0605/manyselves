"""Deterministic project-photo/evidence binding rules for report preparation."""

from __future__ import annotations

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    EvidenceItem,
    PhotoAsset,
)


def runtime_photo_ids(
    evidence: list[EvidenceItem],
    photos: list[PhotoAsset],
) -> list[str]:
    """Return every source-table photo in manifest order or reject an orphan."""

    photo_ids = [photo.id for photo in photos]
    if len(photo_ids) != len(set(photo_ids)):
        duplicates = sorted(
            photo_id
            for photo_id in set(photo_ids)
            if photo_ids.count(photo_id) > 1
        )
        raise ValueError(f"project photo manifest contains duplicate ids: {duplicates}")
    evidence_photo_refs = {
        photo_id
        for item in evidence
        for photo_id in item.photo_refs
    }
    unknown_refs = sorted(evidence_photo_refs - set(photo_ids))
    if unknown_refs:
        raise ValueError(
            "source-table evidence references photos missing from the runtime manifest: "
            f"{unknown_refs}"
        )
    noncanonical_evidence = sorted(
        {
            item.id
            for item in evidence
            if item.photo_refs and not item.id.startswith("E-")
        }
    )
    if noncanonical_evidence:
        raise ValueError(
            "photo evidence must use canonical E-* ids: "
            f"{noncanonical_evidence}"
        )
    evidence_by_photo: dict[str, list[EvidenceItem]] = {}
    for item in evidence:
        if item.submodule_id is None:
            continue
        for photo_id in item.photo_refs:
            evidence_by_photo.setdefault(photo_id, []).append(item)
    orphaned = [photo.id for photo in photos if photo.id not in evidence_by_photo]
    if orphaned:
        raise ValueError(
            "project photos require source-table evidence and a smallest submodule: "
            f"{orphaned}"
        )
    invalid_primary_bindings = {}
    for photo in photos:
        candidates = evidence_by_photo[photo.id]
        candidate_ids = [item.id for item in candidates]
        primary_id = photo.primary_evidence_id
        # Legacy immutable preparation snapshots predate the explicit
        # field. They retain mapper/source-table evidence order, which is
        # the compatibility fallback; new runs persist the chosen ID.
        if primary_id is None:
            continue
        if candidate_ids.count(primary_id) != 1:
            invalid_primary_bindings[photo.id] = {
                "primary_evidence_id": primary_id,
                "candidate_evidence_ids": candidate_ids,
            }
    if invalid_primary_bindings:
        raise ValueError(
            "project photo primary evidence binding must reference exactly one "
            f"candidate: {invalid_primary_bindings}"
        )
    return [photo.id for photo in photos]
