"""Revision-aware project file request and response schemas."""

from datetime import datetime
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, RootModel

UploadConflict = Literal["reject", "replace", "keep-both"]


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


class _Preview(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    path: str


class PdfPreview(_Preview):
    kind: Literal["pdf"]
    mime_type: Literal["application/pdf"] = Field(alias="mimeType")
    size: int
    range_url: str = Field(alias="rangeUrl")


class ImagePreview(_Preview):
    kind: Literal["image"]
    mime_type: str = Field(alias="mimeType")
    width: int | None = None
    height: int | None = None
    content_url: str | None = Field(default=None, alias="contentUrl")
    content: str | None = None


class PreviewSheet(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str
    row_count: int = Field(alias="rowCount")
    column_count: int = Field(alias="columnCount")
    rows: list[list[str]]
    truncated: bool


class SpreadsheetPreview(_Preview):
    kind: Literal["spreadsheet"]
    sheets: list[PreviewSheet]
    sheets_truncated: bool | None = Field(default=None, alias="sheetsTruncated")


class ParagraphPreviewBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["paragraph"]
    text: str


class TablePreviewBlock(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["table"]
    rows: list[list[str]]


class ImagePreviewBlock(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    kind: Literal["image"]
    mime_type: str = Field(alias="mimeType")
    data_url: str = Field(alias="dataUrl")


PreviewBlock = Annotated[
    ParagraphPreviewBlock | TablePreviewBlock | ImagePreviewBlock,
    Field(discriminator="kind"),
]


class DocxPreview(_Preview):
    kind: Literal["docx"]
    blocks: list[PreviewBlock]
    truncated: bool


class MarkdownPreview(_Preview):
    kind: Literal["markdown"]
    content: str
    truncated: bool


class TextPreview(_Preview):
    kind: Literal["text"]
    content: str
    truncated: bool


class UnsupportedFilePreview(_Preview):
    kind: Literal["unsupported"]
    mime_type: str = Field(alias="mimeType")
    size: int
    download_url: str = Field(alias="downloadUrl")


PreviewUnion = Annotated[
    PdfPreview
    | ImagePreview
    | SpreadsheetPreview
    | DocxPreview
    | MarkdownPreview
    | TextPreview
    | UnsupportedFilePreview,
    Field(discriminator="kind"),
]


class PreviewResponse(RootModel[PreviewUnion]):
    """Typed, kind-discriminated frontend preview contract."""
