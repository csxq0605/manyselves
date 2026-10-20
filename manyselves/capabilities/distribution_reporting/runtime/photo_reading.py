"""Read every project photo once per run, against the table records it is bound to.

The workflow ``distribution-photo-reading`` owns the order: plan → lanes →
join → reduce.  These deterministic tools build the per-photo inputs, accept
one typed reading, and persist all readings for later tasks.  A photo already
read in this run is not planned again.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models.agentic import PhotoReadingSubmission
from .models.photo_reading import (
    PHOTO_READING_LANES,
    PhotoReading,
    PhotoReadingInput,
    PhotoReadingPlan,
    PhotoReadingRecord,
    PhotoReadingSummary,
)
from .models.preparation import PreparationContext
from .storage import ReportingStore

MATCH_TEXT = {"matches": "与记录相符", "conflicts": "与记录不符", "cannot_tell": "看不出是否相符"}


def photo_readings_ref(run_id: str) -> str:
    return f"Work/runs/{run_id}/preparation/photo-readings.json"


def load_photo_readings(workspace: Path, run_id: str) -> dict[str, PhotoReading]:
    path = Path(workspace) / photo_readings_ref(run_id)
    if not path.is_file():
        return {}
    payload = json.loads(path.read_text(encoding="utf-8"))
    readings = [PhotoReading.model_validate(item) for item in payload.get("readings", [])]
    return {reading.photo_id: reading for reading in readings}


def reading_label(reading: PhotoReading) -> str:
    text = f"{reading.photo_id}：{reading.visible}；{MATCH_TEXT[reading.record_match]}（{reading.reason}）"
    return text + ("；需人工复核" if reading.needs_review else "")


def photo_reading_labels(workspace: Path, run_id: str) -> dict[str, str]:
    return {
        photo_id: reading_label(reading)
        for photo_id, reading in load_photo_readings(workspace, run_id).items()
    }


def _context(value: Any) -> PreparationContext:
    if isinstance(value, PreparationContext):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return PreparationContext.model_validate(value)


def _plan(value: Any) -> PhotoReadingPlan:
    if isinstance(value, PhotoReadingPlan):
        return value
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    return PhotoReadingPlan.model_validate(value)


class PhotoReadingTools:
    """Capability-owned deterministic steps around the photo reader."""

    def __init__(self, workspace: Path, store: ReportingStore | None = None) -> None:
        self.workspace = Path(workspace).resolve()
        self.store = store or ReportingStore(self.workspace)

    def prepare(self, value: Any) -> dict[str, Any]:
        context = _context(value)
        done = load_photo_readings(self.workspace, context.run_id)
        lanes: dict[str, list[PhotoReadingInput]] = {lane: [] for lane in PHOTO_READING_LANES}
        reused: list[str] = []
        pending = 0
        for photo in context.photo_assets:
            if photo.id in done:
                reused.append(photo.id)
                continue
            bound = [
                item for item in context.evidence_items
                if photo.id in item.photo_refs and item.submodule_id
            ]
            if not bound:
                continue
            bound.sort(key=lambda item: item.id != photo.primary_evidence_id)
            records = [
                PhotoReadingRecord(
                    evidence_id=item.id,
                    subject=item.subject,
                    fact=item.fact,
                    sheet=item.source.sheet,
                    cell=item.source.cell,
                    submodule_id=item.submodule_id,
                    primary=index == 0,
                )
                for index, item in enumerate(bound)
            ]
            lane = PHOTO_READING_LANES[pending % len(PHOTO_READING_LANES)]
            lanes[lane].append(PhotoReadingInput(
                run_id=context.run_id,
                photo_id=photo.id,
                photo_path=Path(photo.path).as_posix(),
                media_type=photo.media_type,
                records=records,
            ))
            pending += 1
        return PhotoReadingPlan(
            run_id=context.run_id,
            photo_count=len(context.photo_assets),
            reused_photo_ids=reused,
            lanes=lanes,
        ).model_dump(mode="json")

    def requires_agent(self, value: Any) -> bool:
        return _plan(value).pending > 0

    def select_lane(self, value: Mapping[str, Any]) -> list[dict[str, Any]]:
        plan = _plan(value["plan"])
        return [item.model_dump(mode="json") for item in plan.lanes.get(str(value["lane_id"]), [])]

    def accept(self, value: Mapping[str, Any]) -> dict[str, Any]:
        item = PhotoReadingInput.model_validate(_dumped(value["item"]))
        submission = PhotoReadingSubmission.model_validate(_dumped(value["result"]))
        return PhotoReading(
            photo_id=item.photo_id,
            evidence_ids=[record.evidence_id for record in item.records],
            visible=submission.visible,
            record_match=submission.record_match,
            reason=submission.reason,
            needs_review=submission.needs_review,
        ).model_dump(mode="json")

    def reduce(self, value: Mapping[str, Any]) -> dict[str, Any]:
        plan = _plan(value["plan"])
        readings = load_photo_readings(self.workspace, plan.run_id)
        read_count = 0
        for outcome in (value.get("outcomes") or {}).values():
            for reading in outcome or []:
                accepted = PhotoReading.model_validate(_dumped(reading))
                readings[accepted.photo_id] = accepted
                read_count += 1
        ordered = sorted(readings.values(), key=lambda reading: reading.photo_id)
        ref = photo_readings_ref(plan.run_id)
        self.store.write_json(ref, {
            "run_id": plan.run_id,
            "readings": [reading.model_dump(mode="json") for reading in ordered],
        })
        return PhotoReadingSummary(
            run_id=plan.run_id,
            photo_count=plan.photo_count,
            read_count=read_count,
            reused_count=len(plan.reused_photo_ids),
            conflict_photo_ids=[r.photo_id for r in ordered if r.record_match == "conflicts"],
            review_photo_ids=[r.photo_id for r in ordered if r.needs_review],
            readings_ref=ref,
        ).model_dump(mode="json")


def _dumped(value: Any) -> Any:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def build_photo_reading_tool_implementations(
    workspace: Path, store: ReportingStore | None = None,
) -> dict[str, Any]:
    tools = PhotoReadingTools(workspace, store)
    return {
        "prepare-photo-reading": tools.prepare,
        "photo-reading-requires-agent": tools.requires_agent,
        "select-photo-reading-lane": tools.select_lane,
        "accept-photo-reading": tools.accept,
        "reduce-photo-reading": tools.reduce,
    }


__all__ = [
    "PhotoReadingTools",
    "build_photo_reading_tool_implementations",
    "load_photo_readings",
    "photo_reading_labels",
    "photo_readings_ref",
    "reading_label",
]
