"""Typed carriers for deterministic Distribution Reporting preparation."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import Field

from .reporting import (
    CoverageMatrix,
    EvidenceItem,
    PhotoAsset,
    ReportingModel,
    ReportRequest,
    SourceLocation,
    SpecialTopicPlan,
)


class ManifestFile(ReportingModel):
    id: str = Field(min_length=1)
    path: Path
    sha256: str = Field(min_length=1)
    media_type: str = Field(min_length=1)
    purpose: str | None = None
    snapshot_ref: Path | None = Field(
        default=None,
        description="Run-frozen source bytes used for parsing; path remains logical provenance.",
    )
    parse_status: Literal["pending", "parsed", "failed"] = "pending"
    error: str | None = None


class ProjectManifest(ReportingModel):
    files: list[ManifestFile] = Field(default_factory=list)


class ParsedArtifact(ReportingModel):
    id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    source: SourceLocation
    payload: dict[str, Any]


ParsedArtifacts = list[ParsedArtifact]


class FilePreparationResult(ReportingModel):
    """Complete provisional result for one manifest file."""

    schema_version: Literal["1"] = "1"
    manifest_order: int = Field(ge=0)
    file_id: str = Field(min_length=1)
    source_path: Path
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    purpose: str | None = None
    status: Literal["parsed", "failed"]
    parsed_artifacts: list[ParsedArtifact] = Field(default_factory=list)
    provisional_evidence: list[EvidenceItem] = Field(default_factory=list)
    raw_photo_assets: dict[str, PhotoAsset] = Field(default_factory=dict)
    mapping_gaps: list[dict] = Field(default_factory=list)
    error: str | None = None


class MappingGap(ReportingModel):
    code: str
    message: str
    sheet: str | None = None
    cell: str | None = None
    module_id: str | None = None
    submodule_id: str | None = None


class MappingResult(ReportingModel):
    evidence_items: list[EvidenceItem]
    gaps: list[MappingGap]


class PreparationContext(ReportingModel):
    """Serializable state passed through the file-defined preparation workflow."""

    run_id: str = Field(min_length=1)
    request: ReportRequest
    resume: bool = False
    input_snapshot_ref: str | None = None
    input_snapshot_digest: str | None = None
    project_manifest: ProjectManifest | None = None
    preparation_worker_results: list[FilePreparationResult] = Field(
        default_factory=list
    )
    parsed_artifacts: list[ParsedArtifact] = Field(default_factory=list)
    preparation_parallelism: dict[str, Any] = Field(default_factory=dict)
    evidence_items: list[EvidenceItem] = Field(default_factory=list)
    photo_assets: list[PhotoAsset] = Field(default_factory=list)
    photo_evidence_adjacency: dict[str, Any] = Field(default_factory=dict)
    mapping_gaps: list[dict[str, Any]] = Field(default_factory=list)
    coverage_matrix: CoverageMatrix | None = None
    report_taxonomy: dict[str, Any] = Field(default_factory=dict)
    special_topic_plan: SpecialTopicPlan | None = None
    preparation_refs: dict[str, str] = Field(default_factory=dict)
    preparation_completion_ref: str | None = None
