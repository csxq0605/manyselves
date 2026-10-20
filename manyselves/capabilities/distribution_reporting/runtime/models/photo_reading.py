"""Typed values for reading project photos once per run."""

from __future__ import annotations

from typing import Literal

from pydantic import Field

from .agentic import StrictModel

PHOTO_READING_LANES = ("1", "2", "3", "4")


class PhotoReadingRecord(StrictModel):
    """One table record a photo is bound to; the primary record comes first."""

    evidence_id: str = Field(min_length=1)
    subject: str
    fact: str
    sheet: str | None = None
    cell: str | None = None
    submodule_id: str | None = None
    primary: bool = False


class PhotoReadingInput(StrictModel):
    """One photo and its bound records, sent to the photo reader as one task."""

    run_id: str = Field(min_length=1)
    photo_id: str = Field(min_length=1)
    photo_path: str = Field(min_length=1, description="Workspace-relative image path.")
    media_type: str = Field(min_length=1)
    records: list[PhotoReadingRecord] = Field(min_length=1)


class PhotoReading(StrictModel):
    """Accepted reading of one photo."""

    photo_id: str = Field(min_length=1)
    evidence_ids: list[str] = Field(default_factory=list)
    visible: str
    record_match: Literal["matches", "conflicts", "cannot_tell"]
    reason: str
    needs_review: bool


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
    "PhotoReadingRecord",
    "PhotoReadingSummary",
]
