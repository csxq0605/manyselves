"""Typed handoff contracts for deterministic report rendering."""

from pathlib import Path
from typing import Literal

from pydantic import Field, field_serializer

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import ReportingModel


class RenderRequest(ReportingModel):
    run_id: str = Field(min_length=1)
    source_markdown_ref: Path
    source_snapshot_ref: Path | None = None
    template_ref: Path
    output_ref: Path

    @field_serializer(
        "source_markdown_ref",
        "source_snapshot_ref",
        "template_ref",
        "output_ref",
        when_used="json",
    )
    def serialize_project_ref(self, value: Path | None) -> str | None:
        return value.as_posix().replace("\\", "/") if value is not None else None


class RenderResult(ReportingModel):
    status: Literal["completed", "failed"]
    run_id: str = Field(min_length=1)
    source_markdown_ref: Path
    source_snapshot_ref: Path | None = None
    output_ref: Path | None = None
    render_log_ref: Path | None = None
    template_sha256: str | None = None
    output_sha256: str | None = None
    protected_prose_verified: bool = False
    validation_warnings: list[str] = Field(default_factory=list)
    error: str | None = None

    @field_serializer(
        "source_markdown_ref",
        "source_snapshot_ref",
        "output_ref",
        "render_log_ref",
        when_used="json",
    )
    def serialize_project_ref(self, value: Path | None) -> str | None:
        return value.as_posix().replace("\\", "/") if value is not None else None
