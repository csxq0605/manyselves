"""Current-input projections for the file-defined report revision workflow."""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

from .entrypoint_tools import build_reporting_state
from .models.entrypoint import ReportingRunContext
from .models.preparation import PreparationContext, RevisionPreparationAttachment
from .models.reporting import EvidenceItem, PhotoAsset
from .research.project_evidence import project_evidence_locator
from .source_ledger import SourceLedger
from .storage import ReportingStore


def project_revision_preparation(value: dict[str, Any]) -> PreparationContext:
    return PreparationContext(run_id=value["run_id"], request=value["request"])


def _evidence_key(item: EvidenceItem) -> str:
    value = item.model_dump(mode="json", exclude={"id", "photo_refs"})
    # A workbook edit changes its file id even for rows whose facts did not change.
    value["source"].pop("file_id", None)
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _largest_id(ids: list[str], prefix: str) -> int:
    return max(
        (
            int(value.removeprefix(prefix))
            for value in ids
            if value.startswith(prefix) and value.removeprefix(prefix).isdigit()
        ),
        default=0,
    )


def reconcile_revision_evidence(value: Any, *, workspace: Path) -> PreparationContext:
    """Keep unchanged identities and allocate new IDs after all baseline IDs."""
    context = PreparationContext.model_validate(value)
    root = f"Work/runs/{context.run_id}"
    store = ReportingStore(workspace)
    baseline = json.loads(
        (workspace / root / "baseline/business-state.json").read_text(encoding="utf-8")
    )
    old_evidence = [
        EvidenceItem.model_validate(item) for item in baseline.get("evidence_items", [])
    ]
    old_photos = [PhotoAsset.model_validate(item) for item in baseline.get("photo_assets", [])]
    # Existing ledger IDs include sources retained across more than one revision.
    ledger = SourceLedger(workspace, context.run_id)
    records = ledger.records
    next_e = _largest_id(
        [record.id for record in records] + [item.id for item in old_evidence], "E-"
    )
    reserved_photos = [item.id for item in old_photos]
    for record in records:
        if record.id.startswith("E-") and (ref := ledger.content_ref(record.id)):
            try:
                original = json.loads((workspace / ref).read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                # SourceLedger also permits plain-text project-source content.
                continue
            if isinstance(original, dict):
                reserved_photos.extend(original.get("photo_refs", []))
    next_p = _largest_id(reserved_photos, "P-")
    photo_by_content: dict[tuple[str, str], list[PhotoAsset]] = {}
    for item in old_photos:
        photo_by_content.setdefault((item.sha256, item.source_member), []).append(item)
    photo_ids: dict[str, str] = {}
    photos: list[PhotoAsset] = []
    for item in context.photo_assets:
        candidates = photo_by_content.get((item.sha256, item.source_member), [])
        prior = candidates.pop(0) if candidates else None
        if prior is None:
            next_p += 1
        photo_ids[item.id] = prior.id if prior else f"P-{next_p:04d}"
        photos.append(item.model_copy(update={"id": photo_ids[item.id]}))
    prior_by_key: dict[str, list[EvidenceItem]] = {}
    for item in old_evidence:
        prior_by_key.setdefault(_evidence_key(item), []).append(item)
    evidence_ids: dict[str, str] = {}
    evidence: list[EvidenceItem] = []
    added: list[EvidenceItem] = []
    for item in context.evidence_items:
        mapped_photos = [photo_ids[ref] for ref in item.photo_refs]
        candidates = prior_by_key.get(_evidence_key(item), [])
        prior = next(
            (candidate for candidate in candidates if candidate.photo_refs == mapped_photos), None
        )
        if prior is not None:
            candidates.remove(prior)
            selected = prior
        else:
            next_e += 1
            selected = item.model_copy(
                update={"id": f"E-{next_e:04d}", "photo_refs": mapped_photos}
            )
            added.append(selected)
        evidence_ids[item.id] = selected.id
        evidence.append(selected)
    photos = [
        item.model_copy(update={"primary_evidence_id": evidence_ids[item.primary_evidence_id]})
        for item in photos
    ]
    active_ids = {item.id for item in evidence}
    changes = {
        "files": baseline["input_changes"],
        "superseded_evidence_ids": [item.id for item in old_evidence if item.id not in active_ids],
        "current_evidence": [
            {
                "evidence_id": item.id,
                "title": item.subject,
                "locator": project_evidence_locator(item),
                "content": item.model_dump_json(),
            }
            for item in added
        ],
    }
    store.write_json(f"{root}/revision-input-changes.json", changes)
    adjacency = {
        "schema_version": 1,
        "photo_to_evidence": {
            item.id: [ev.id for ev in evidence if item.id in ev.photo_refs] for item in photos
        },
        "evidence_to_photo": {item.id: item.photo_refs for item in evidence},
        "primary_evidence": {item.id: item.primary_evidence_id for item in photos},
    }
    return context.model_copy(
        update={
            "evidence_items": evidence,
            "photo_assets": photos,
            "photo_evidence_adjacency": adjacency,
        }
    )


def attach_revision_preparation(value: Any, *, workspace: Path) -> dict[str, Any]:
    attachment = RevisionPreparationAttachment.model_validate(value)
    state = dict(attachment.state)
    prepared = attachment.preparation
    projection = build_reporting_state(
        ReportingRunContext(
            run_id=prepared.run_id,
            request=prepared.request,
            preparation_context=prepared,
        )
    )
    state.update(projection)
    state["full_report"] = True
    state["request"] = prepared.request.model_dump(mode="json")
    root = f"Work/runs/{prepared.run_id}"
    state["revision_input_changes"] = json.loads(
        (workspace / root / "revision-input-changes.json").read_text(encoding="utf-8")
    )
    store = ReportingStore(workspace)
    store.write_jsonl(
        f"{root}/evidence.jsonl", [item.model_dump(mode="json") for item in prepared.evidence_items]
    )
    store.write_json(
        f"{root}/context/photo-manifest.json",
        {"assets": [item.model_dump(mode="json") for item in prepared.photo_assets]},
    )
    return state


def build_revision_input_tools(workspace: Path) -> dict[str, Any]:
    return {
        "revision-inputs-changed": lambda value: any(value.get("input_changes", {}).values()),
        "project-revision-preparation": project_revision_preparation,
        "reconcile-revision-evidence": partial(reconcile_revision_evidence, workspace=workspace),
        "attach-revision-preparation": partial(attach_revision_preparation, workspace=workspace),
    }
