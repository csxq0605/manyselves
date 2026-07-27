"""Immutable, restorable report-version snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from .models import ReportingModel


class SkillProvenance(ReportingModel):
    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    scope: Literal["packaged", "product", "project"]


class ReportVersion(ReportingModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version_id: str = Field(min_length=1)
    run_id: str = Field(min_length=1)
    parent_version_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    artifact_refs: dict[str, Path]
    artifact_sha256: dict[str, str] = Field(default_factory=dict)
    skill_provenance: list[SkillProvenance]
    session_summary_refs: list[Path]

    @field_validator("artifact_refs", "session_summary_refs")
    @classmethod
    def references_are_project_relative(cls, value):
        refs = value.values() if isinstance(value, dict) else value
        if any(Path(ref).is_absolute() or ".." in Path(ref).parts for ref in refs):
            raise ValueError("report version references must be project-relative")
        return value


class ReportVersionStore:
    """Publish complete snapshots without mutating prior versions."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "Work/report-versions"

    @staticmethod
    def _safe_id(value: str) -> str:
        if not re.fullmatch(r"[A-Za-z0-9._-]+", value or ""):
            raise ValueError("version_id must be a safe path segment")
        return value

    def publish(self, version: ReportVersion) -> ReportVersion:
        version_id = self._safe_id(version.version_id)
        destination = self.root / version_id
        if (
            version.parent_version_id is not None
            and not (
                self.root / self._safe_id(version.parent_version_id) / "version.json"
            ).is_file()
        ):
            raise ValueError(f"parent report version does not exist: {version.parent_version_id}")

        sources: dict[str, Path] = {}
        for key, relative in version.artifact_refs.items():
            source = (self.workspace / relative).resolve()
            if not source.is_relative_to(self.workspace) or not source.is_file():
                raise FileNotFoundError(f"report version artifact is missing: {relative}")
            sources[key] = source
        for relative in version.session_summary_refs:
            source = (self.workspace / relative).resolve()
            if not source.is_relative_to(self.workspace) or not source.is_file():
                raise FileNotFoundError(f"session summary is missing: {relative}")

        if destination.exists():
            published = self.load(version_id)
            requested_hashes = {
                key: self._sha256(source) for key, source in sources.items()
            }
            if (
                published.run_id != version.run_id
                or published.parent_version_id != version.parent_version_id
                or set(published.artifact_refs) != set(version.artifact_refs)
                or published.artifact_sha256 != requested_hashes
                or published.skill_provenance != version.skill_provenance
            ):
                raise ValueError(
                    "existing report version is partial or differs from the current "
                    "run; refusing to overwrite it"
                )
            return published

        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".report-version-", dir=self.root) as temp:
            staging = Path(temp) / version_id
            artifact_dir = staging / "artifacts"
            artifact_dir.mkdir(parents=True)
            snapshot_refs: dict[str, Path] = {}
            hashes: dict[str, str] = {}
            for index, (key, source) in enumerate(sorted(sources.items()), start=1):
                safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-_") or "artifact"
                target = artifact_dir / f"{index:02d}-{safe_key}{source.suffix}"
                shutil.copyfile(source, target)
                relative = (destination / "artifacts" / target.name).relative_to(self.workspace)
                snapshot_refs[key] = relative
                hashes[key] = self._sha256(target)
            summary_dir = staging / "session-summaries"
            summary_dir.mkdir()
            summary_refs: list[Path] = []
            for index, relative in enumerate(version.session_summary_refs, start=1):
                source = self.workspace / relative
                target = summary_dir / f"{index:02d}-{source.name}"
                shutil.copyfile(source, target)
                summary_refs.append(
                    (destination / "session-summaries" / target.name).relative_to(self.workspace)
                )
            published = version.model_copy(
                update={
                    "artifact_refs": snapshot_refs,
                    "artifact_sha256": hashes,
                    "session_summary_refs": summary_refs,
                }
            )
            (staging / "version.json").write_text(
                published.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            os.replace(staging, destination)

        pointer = self.root / "latest.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".latest-",
            suffix=".tmp",
            dir=self.root,
            delete=False,
        ) as handle:
            handle.write(json.dumps({"version_id": version_id}) + "\n")
            temporary_pointer = Path(handle.name)
        os.replace(temporary_pointer, pointer)
        return published

    def load(self, version_id: str) -> ReportVersion:
        path = self.root / self._safe_id(version_id) / "version.json"
        if not path.is_file():
            raise FileNotFoundError(f"unknown report version: {version_id}")
        return ReportVersion.model_validate_json(path.read_text(encoding="utf-8"))

    def latest(self) -> ReportVersion:
        pointer = self.root / "latest.json"
        if not pointer.is_file():
            raise FileNotFoundError("no report version has been published")
        version_id = json.loads(pointer.read_text(encoding="utf-8"))["version_id"]
        return self.load(version_id)

    def list_versions(self) -> list[ReportVersion]:
        if not self.root.is_dir():
            return []
        versions = [
            ReportVersion.model_validate_json(path.read_text(encoding="utf-8"))
            for path in sorted(self.root.glob("*/version.json"))
        ]
        return sorted(versions, key=lambda item: (item.created_at, item.version_id))

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
