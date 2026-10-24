"""Typed values for reading project photos once per run."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import Field

from .agentic import StrictModel

PHOTO_READING_LANES = ("1", "2", "3", "4", "5", "6", "7", "8")


class RecordValue(StrictModel):
    value: float
    unit: str = ""


class PhotoQuantity(StrictModel):
    """A quantity the table declares for the photo's object: what to look for."""

    quantity: str = Field(min_length=1, description="Key to report a reading under.")
    label: str = Field(description="The table's header label of the quantity.")
    unit: str = ""


class PhotoReadingRecord(StrictModel):
    """One table record a photo is bound to; the primary record comes first.

    The reader is told where the photo sits and what the column checks, not
    the recorded values, so a reading is never copied from the record.
    """

    evidence_id: str = Field(min_length=1)
    subject: str
    checkpoint: str = ""
    fact: str = ""
    sheet: str | None = None
    cell: str | None = None
    submodule_id: str | None = None
    primary: bool = False
    values: list[RecordValue] = Field(
        default_factory=list, description="Numbers the record states, with units."
    )


class PhotoImage(StrictModel):
    """One image sent with a reading: the whole photo or an enlarged part of it."""

    path: str = Field(min_length=1, description="Workspace-relative image path.")
    media_type: str = Field(min_length=1)
    part: str = Field(default="整张照片", description="Which part of the photo this image shows.")


class PhotoReadingInput(StrictModel):
    """One photo and its bound records, sent to the photo reader as one task."""

    run_id: str = Field(min_length=1)
    photo_id: str = Field(min_length=1)
    photo_path: str = Field(min_length=1, description="Workspace-relative image path.")
    media_type: str = Field(min_length=1)
    photo_sheet: str | None = Field(default=None, description="Sheet holding the photo.")
    photo_cell: str | None = Field(default=None, description="Cell holding the photo.")
    column_header: str | None = Field(
        default=None,
        description="Header text of the photo's column: why and when the photo is attached.",
    )
    records: list[PhotoReadingRecord] = Field(min_length=1)
    quantities: list[PhotoQuantity] = Field(
        default_factory=list,
        description="Quantities the table declares for this object, to read off the photo if shown.",
    )
    check_labels: list[str] = Field(
        default_factory=list,
        description=(
            "A re-read only: the items a first reading left blank or illegible, to be read "
            "again on their own.  What the first reading said is not given."
        ),
    )
    photo_date: str | None = Field(
        default=None,
        description=(
            "A re-read only: the date the first reading found on the photo, as context for "
            "dated records (a blank after this date is not a missed entry)."
        ),
    )
    images: list[PhotoImage] = Field(
        default_factory=list,
        description=(
            "A re-read only: the whole photo first, then its four quarters enlarged, so "
            "small displays and segment digits can be read."
        ),
    )


class PhotoReading(StrictModel):
    """Accepted reading of one photo.

    ``record_match`` and ``reason`` come from the program comparing the copied
    numbers with the record's numbers, not from the reader.  When they differ
    the table record stands and the photo reading is to be checked.
    """

    photo_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    visible: str
    transcription: list[dict[str, Any]] = Field(default_factory=list)
    photo_date: str | None = None
    readings: list[dict[str, Any]] = Field(
        default_factory=list, description="Structured readings of declared quantities."
    )
    record_match: Literal["matches", "conflicts", "cannot_tell"]
    reason: str
    needs_review: bool
    reread_agrees: bool | None = Field(
        default=None,
        description="A second, independent reading agreed (True) or not (False); None = not re-read.",
    )
    reread_reason: str = ""


class PhotoReadingPlan(StrictModel):
    """Photos still to read in this run, split over the fixed lanes."""

    run_id: str = Field(min_length=1)
    photo_count: int = Field(ge=0)
    reused_photo_ids: list[str] = Field(default_factory=list)
    lanes: dict[str, list[PhotoReadingInput]] = Field(default_factory=dict)

    @property
    def pending(self) -> int:
        return sum(len(items) for items in self.lanes.values())


class PhotoReadingSummary(StrictModel):
    run_id: str = Field(min_length=1)
    photo_count: int = Field(ge=0)
    read_count: int = Field(ge=0)
    reused_count: int = Field(ge=0)
    conflict_photo_ids: list[str] = Field(default_factory=list)
    review_photo_ids: list[str] = Field(default_factory=list)
    readings_ref: str | None = None


__all__ = [
    "PHOTO_READING_LANES",
    "PhotoReading",
    "PhotoReadingInput",
    "PhotoReadingPlan",
    "PhotoImage",
    "PhotoQuantity",
    "PhotoReadingRecord",
    "RecordValue",
    "PhotoReadingSummary",
]
