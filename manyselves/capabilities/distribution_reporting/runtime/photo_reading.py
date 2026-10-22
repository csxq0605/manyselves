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
    PhotoQuantity,
    PhotoReadingRecord,
    PhotoReadingSummary,
)
from .models.preparation import PreparationContext
from .storage import ReportingStore

MATCH_TEXT = {
    "matches": "程序比对：照片读数与记录数值一致",
    "conflicts": "程序比对：照片读数与记录数值不一致，以表格记录为准，照片读数或归属待核",
    "cannot_tell": "程序比对：无可比数值",
}


def declared_quantities(item: Any) -> list[tuple[str, str, str, float]]:
    """(quantity, header label, unit, value) the mapper declared for one record."""

    found = [
        (m.quantity, m.label or m.quantity, m.unit or "", float(m.value))
        for m in item.measurements
        if m.value is not None
    ]
    value = item.value
    if (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and not any(v == float(value) and u == (item.unit or "") for _, _, u, v in found)
    ):
        label = item.checkpoint_field or "记录值"
        found.append((label, label, item.unit or "", float(value)))
    return found


def _close(a: float, b: float) -> bool:
    # Display rounding: 0.05 absolute or 2 %.
    return abs(a - b) <= max(0.05, 0.02 * abs(a))


def compare_readings(
    readings: list[dict[str, Any]],
    bound: Mapping[str, list[float]],
    same_subject: Mapping[str, list[list[Any]]] | None = None,
) -> tuple[str, str]:
    """Compare the reader's structured readings with the bound records, by quantity.

    ``bound`` holds each quantity's values in the records the photo is bound
    to; ``same_subject`` holds [value, evidence id] of the same quantity in the
    object's other records.  A legible reading that matches no bound value is
    a conflict: the reading or the photo's attribution is to be checked, and
    a match with another record of the object is named.
    """

    seen = [
        r for r in readings
        if r.get("legible") and r.get("value") is not None and r.get("quantity") in bound
    ]
    if not seen:
        return "cannot_tell", "照片没有看清所附记录声明的量"
    matched, mismatched = [], []
    for reading in seen:
        quantity, number = reading["quantity"], float(reading["value"])
        if any(_close(value, number) for value in bound[quantity]):
            matched.append(f"{quantity}={number:g}")
            continue
        others = [
            evidence_id
            for value, evidence_id in (same_subject or {}).get(quantity, [])
            if _close(float(value), number)
        ]
        note = f"（与同一对象另一条记录 {'、'.join(others)} 一致）" if others else ""
        mismatched.append(
            f"{quantity} 照片 {number:g}，记录 "
            + "、".join(dict.fromkeys(f"{v:g}" for v in bound[quantity]))
            + note
        )
    if mismatched:
        return "conflicts", "；".join(mismatched)
    return "matches", "记录值在照片中可见：" + "、".join(matched)


def readings_agree(first: list[dict[str, Any]], second: list[dict[str, Any]]) -> tuple[bool, str]:
    """Two independent readings of one photo agree on every quantity both read."""

    def legible(readings):
        return {
            r["quantity"]: float(r["value"])
            for r in readings
            if r.get("legible") and r.get("value") is not None
        }

    one, two = legible(first), legible(second)
    differing = [
        f"{quantity}：{one.get(quantity, '看不清')} / {two.get(quantity, '看不清')}"
        for quantity in sorted(set(one) | set(two))
        if quantity not in one or quantity not in two or not _close(one[quantity], two[quantity])
    ]
    if differing:
        return False, "两次读数不一致，读数待核：" + "；".join(differing)
    return True, "两次读数一致"


def unread_labels(transcription: list[dict[str, Any]]) -> list[str]:
    """Items a reading left blank or could not read: what a second reading looks at."""

    return list(dict.fromkeys(
        str(item.get("label"))
        for item in transcription
        if item.get("label") and (not item.get("legible") or not str(item.get("text") or "").strip())
    ))


def unread_agree(
    first: list[dict[str, Any]], second: list[dict[str, Any]], labels: list[str]
) -> tuple[bool, str]:
    """A second reading that reads text where the first saw a blank or nothing legible disagrees."""

    def by_label(entries):
        return {str(e.get("label")): e for e in entries if e.get("label")}

    one, two = by_label(first), by_label(second)
    differing = []
    for label in labels:
        again = two.get(label)
        text = str((again or {}).get("text") or "").strip()
        if again is None or not again.get("legible") or not text:
            continue
        before = one.get(label) or {}
        said = "看不清" if not before.get("legible") else "空白"
        differing.append(f"{label}：首读为{said}，复读为“{text}”")
    if differing:
        return False, "两次读图不一致，读数待核：" + "；".join(differing)
    return True, ""


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
    if reading.readings:
        text += "；读数：" + "，".join(
            f"{r.get('quantity')}={r.get('value') if r.get('legible') else '看不清'}{r.get('unit') or ''}"
            for r in reading.readings
        )
    text += f"；{MATCH_TEXT[reading.record_match]}（{reading.reason}）"
    if reading.reread_agrees is False:
        text += f"；{reading.reread_reason}"
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
        expected: dict[str, Any] = self._expected(context.run_id)
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
            records, quantities = photo_records(bound, context.evidence_items)
            expected[photo.id] = expected_values(bound, context.evidence_items)
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
                quantities=quantities,
            ))
            pending += 1
        # The recorded values stay with the program; the reader never sees them.
        self.store.write_json(photo_expected_ref(context.run_id), expected)
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
        readings = [entry.model_dump(mode="json") for entry in submission.readings]
        expected = self._expected(item.run_id).get(item.photo_id) or {}
        match, reason = compare_readings(
            readings, expected.get("bound") or {}, expected.get("same_subject") or {}
        )
        return PhotoReading(
            photo_id=item.photo_id,
            evidence_ids=[record.evidence_id for record in item.records],
            visible=submission.visible,
            transcription=transcription,
            photo_date=submission.photo_date,
            readings=readings,
            record_match=match,
            reason=reason,
            needs_review=submission.needs_review or match == "conflicts",
        ).model_dump(mode="json")

    def _expected(self, run_id: str) -> dict[str, Any]:
        path = self.workspace / photo_expected_ref(run_id)
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}

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


def photo_expected_ref(run_id: str) -> str:
    return f"Work/runs/{run_id}/preparation/photo-expected.json"


def photo_records(
    bound: list[Any], items: list[Any]
) -> tuple[list[PhotoReadingRecord], list[PhotoQuantity]]:
    """What the reader is told: where the photo sits and what to look for, no values.

    The quantities are those the table declares for the bound records and the
    object's other records, so a reading that belongs elsewhere still has a
    name to be reported under.
    """

    records = [
        PhotoReadingRecord(
            evidence_id=item.id,
            subject=item.subject,
            checkpoint=item.checkpoint_field or "",
            sheet=item.source.sheet,
            cell=item.source.cell,
            submodule_id=item.submodule_id,
            primary=index == 0,
        )
        for index, item in enumerate(bound)
    ]
    subjects = {item.subject for item in bound}
    related = [*bound, *(i for i in items if i.subject in subjects and i not in bound)]
    quantities = {
        quantity: PhotoQuantity(quantity=quantity, label=label, unit=unit)
        for item in related
        for quantity, label, unit, _value in declared_quantities(item)
    }
    return records, list(quantities.values())


def expected_values(bound: list[Any], items: list[Any]) -> dict[str, Any]:
    """The bound records' values by quantity, and the object's other records' values."""

    found: dict[str, list[float]] = {}
    for item in bound:
        for quantity, _label, _unit, value in declared_quantities(item):
            found.setdefault(quantity, []).append(value)
    subjects = {item.subject for item in bound}
    bound_ids = {item.id for item in bound}
    others: dict[str, list[list[Any]]] = {}
    for item in items:
        if item.subject in subjects and item.id not in bound_ids:
            for quantity, _label, _unit, value in declared_quantities(item):
                others.setdefault(quantity, []).append([value, item.id])
    return {"bound": found, "same_subject": others}


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
    "compare_readings",
    "declared_quantities",
    "photo_records",
    "readings_agree",
    "unread_agree",
    "unread_labels",
    "PhotoReadingTools",
    "build_photo_reading_tool_implementations",
    "load_photo_readings",
    "photo_reading_labels",
    "photo_readings_ref",
    "reading_label",
]
