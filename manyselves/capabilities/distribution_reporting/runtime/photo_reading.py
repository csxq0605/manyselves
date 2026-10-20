"""Read every project photo once per run, against the table records it is bound to.

The workflow ``distribution-photo-reading`` owns the order: plan → lanes →
join → reduce.  These deterministic tools build the per-photo inputs, accept
one typed reading, and persist all readings for later tasks.  A photo already
read in this run is not planned again.
"""

from __future__ import annotations

import json
import re
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

MATCH_TEXT = {
    "matches": "程序比对：照片读数与记录数值一致",
    "conflicts": "程序比对：照片读数与记录数值不一致，以表格记录为准，照片读数待核",
    "cannot_tell": "程序比对：无可比数值",
}

_NUMBER = re.compile(r"(?<![A-Za-z\d.])-?\d+(?:\.\d+)?(?![\d.])")
_DATE = re.compile(
    r"\d{4}\s*[-/年]\s*\d{1,2}|\d{4}\.\d{1,2}\.\d{1,2}|\d{1,2}\s*月\s*\d{1,2}|\d{1,2}\s*:\s*\d{2}"
)


def _numbers(text: str) -> list[float]:
    if _DATE.search(text):
        return []
    return [float(match.group(0)) for match in _NUMBER.finditer(text)]


def compare_with_records(
    records: list[PhotoReadingRecord], transcription: list[dict[str, Any]]
) -> tuple[str, str]:
    """Compare legible copied numbers with the numbers the records state.

    A record number matches when a copied number is within display rounding
    (0.05 or 2 %).  Only legible items count; names and dates are not compared.
    """

    expected = list(dict.fromkeys(value for record in records for value in record.values))
    seen = [
        number
        for item in transcription
        if item.get("legible")
        for number in _numbers(str(item.get("text", "")))
    ]
    if not expected or not seen:
        missing = "记录没有数值" if not expected else "照片没有看清的数值"
        return "cannot_tell", missing
    for value in expected:
        if any(abs(value - number) <= max(0.05, 0.02 * abs(value)) for number in seen):
            return "matches", f"记录值 {value:g} 在照片中可见"
    return "conflicts", (
        "记录值 " + "、".join(f"{v:g}" for v in expected)
        + "；照片可见 " + "、".join(f"{v:g}" for v in seen)
    )


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
    copied = "，".join(
        f"{item.get('label')}={item.get('text') or '（空白）'}"
        + ("" if item.get("legible") else "（看不清）")
        for item in reading.transcription
    )
    text = f"{reading.photo_id}：{reading.visible}"
    if copied:
        text += f"；抄写：{copied}"
    if reading.photo_date:
        text += f"；照片日期：{reading.photo_date}"
    text += f"；{MATCH_TEXT[reading.record_match]}（{reading.reason}）"
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
        headers = {
            (Path(result.source_path).as_posix(), sheet): columns
            for result in context.preparation_worker_results
            for sheet, columns in result.column_headers.items()
        }
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
                    values=_record_values(item),
                )
                for index, item in enumerate(bound)
            ]
            column = re.sub(r"\d+", "", photo.source_cell or "")
            columns = headers.get((bound[0].source.path.as_posix(), photo.source_sheet or ""), {})
            lane = PHOTO_READING_LANES[pending % len(PHOTO_READING_LANES)]
            lanes[lane].append(PhotoReadingInput(
                run_id=context.run_id,
                photo_id=photo.id,
                photo_path=Path(photo.path).as_posix(),
                media_type=photo.media_type,
                photo_sheet=photo.source_sheet,
                photo_cell=photo.source_cell,
                column_header=columns.get(column) if column else None,
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
        transcription = [entry.model_dump(mode="json") for entry in submission.transcription]
        match, reason = compare_with_records(item.records, transcription)
        return PhotoReading(
            photo_id=item.photo_id,
            evidence_ids=[record.evidence_id for record in item.records],
            visible=submission.visible,
            transcription=transcription,
            photo_date=submission.photo_date,
            record_match=match,
            reason=reason,
            needs_review=submission.needs_review or match == "conflicts",
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


def _record_values(item: Any) -> list[float]:
    values = [
        measurement.value
        for measurement in item.measurements
        if measurement.value is not None
    ]
    if isinstance(item.value, (int, float)) and not isinstance(item.value, bool):
        values.insert(0, float(item.value))
    return list(dict.fromkeys(float(value) for value in values))


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
