"""Revision-aware project file request and response schemas."""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class FileContent(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    path: str
    revision: str
    size: int
    modified_at: datetime = Field(alias="modifiedAt")
    content: str


class SaveFileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    base_revision: str = Field(alias="baseRevision", min_length=64, max_length=64)
    content: str


class CreateEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    kind: Literal["file", "directory"]
    content: str = ""


class RenameEntryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    source: str
    destination: str
    base_revision: str = Field(alias="baseRevision", pattern=r"^[0-9a-f]{64}$")


class FileEntryResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    path: str
    name: str
    kind: Literal["file", "directory"]
    size: int | None
    modified_at: datetime = Field(alias="modifiedAt")
    revision: str


class FileTreeResponse(BaseModel):
    entries: list[FileEntryResponse]
