"""Immutable, restorable report-version snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from ..artifacts.content_store import ContentAddressedStore
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
    storage_version: Literal[1, 2] = 1
    artifact_blob_refs: dict[str, Path] = Field(default_factory=dict)
    skill_provenance: list[SkillProvenance]
    session_summary_refs: list[Path]
    session_summary_blob_refs: list[Path] = Field(default_factory=list)
    session_summary_sha256: list[str] = Field(default_factory=list)

    @field_validator(
        "artifact_refs",
        "artifact_blob_refs",
        "session_summary_refs",
        "session_summary_blob_refs",
    )
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
        self.content_store = ContentAddressedStore(self.workspace)

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
            source = self.workspace / relative
            resolved = source.resolve()
            if not resolved.is_relative_to(self.workspace) or not resolved.is_file():
                raise FileNotFoundError(f"report version artifact is missing: {relative}")
            sources[key] = source
        summary_sources: list[Path] = []
        for relative in version.session_summary_refs:
            source = self.workspace / relative
            resolved = source.resolve()
            if not resolved.is_relative_to(self.workspace) or not resolved.is_file():
                raise FileNotFoundError(f"session summary is missing: {relative}")
            summary_sources.append(source)

        if destination.exists():
            published = self.load(version_id)
            requested_hashes = {
                key: self._sha256(source) for key, source in sources.items()
            }
            requested_summary_hashes = [
                self._sha256(source) for source in summary_sources
            ]
            if (
                published.run_id != version.run_id
                or published.parent_version_id != version.parent_version_id
                or set(published.artifact_refs) != set(version.artifact_refs)
                or published.artifact_sha256 != requested_hashes
                or published.skill_provenance != version.skill_provenance
                or (
                    published.storage_version == 2
                    and published.session_summary_sha256
                    != requested_summary_hashes
                )
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
            blob_refs: dict[str, Path] = {}
            for index, (key, source) in enumerate(sorted(sources.items()), start=1):
                safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-_") or "artifact"
                target = artifact_dir / f"{index:02d}-{safe_key}{source.suffix}"
                final_target = destination / "artifacts" / target.name
                blob = self.content_store.ingest_file(source)
                self.content_store.link_view(
                    blob,
                    target,
                    final_path=final_target,
                )
                relative = final_target.relative_to(self.workspace)
                snapshot_refs[key] = relative
                hashes[key] = blob.sha256
                blob_refs[key] = blob.relative_path
            summary_dir = staging / "session-summaries"
            summary_dir.mkdir()
            summary_refs: list[Path] = []
            summary_blob_refs: list[Path] = []
            summary_hashes: list[str] = []
            for index, source in enumerate(summary_sources, start=1):
                target = summary_dir / f"{index:02d}-{source.name}"
                final_target = destination / "session-summaries" / target.name
                blob = self.content_store.ingest_file(source)
                self.content_store.link_view(
                    blob,
                    target,
                    final_path=final_target,
                )
                summary_refs.append(final_target.relative_to(self.workspace))
                summary_blob_refs.append(blob.relative_path)
                summary_hashes.append(blob.sha256)
            published = version.model_copy(
                update={
                    "storage_version": 2,
                    "artifact_refs": snapshot_refs,
                    "artifact_sha256": hashes,
                    "artifact_blob_refs": blob_refs,
                    "session_summary_refs": summary_refs,
                    "session_summary_blob_refs": summary_blob_refs,
                    "session_summary_sha256": summary_hashes,
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
        safe_version_id = self._safe_id(version_id)
        version_root = self.root / safe_version_id
        path = version_root / "version.json"
        if (
            version_root.is_symlink()
            or path.is_symlink()
            or not path.is_file()
        ):
            raise FileNotFoundError(f"unknown report version: {version_id}")
        version = ReportVersion.model_validate_json(path.read_text(encoding="utf-8"))
        if version.version_id != safe_version_id:
            raise ValueError(
                "report version manifest identity does not match its directory"
            )
        if version.storage_version == 2:
            self._validate_v2(version, version_root=version_root)
        return version

    def latest(self) -> ReportVersion:
        pointer = self.root / "latest.json"
        if not pointer.is_file():
            raise FileNotFoundError("no report version has been published")
        version_id = json.loads(pointer.read_text(encoding="utf-8"))["version_id"]
        return self.load(version_id)

    def list_versions(self) -> list[ReportVersion]:
        if not self.root.is_dir():
            return []
        versions = [self.load(path.parent.name) for path in sorted(self.root.glob("*/version.json"))]
        return sorted(versions, key=lambda item: (item.created_at, item.version_id))

    def _validate_v2(
        self,
        version: ReportVersion,
        *,
        version_root: Path,
    ) -> None:
        if (
            set(version.artifact_refs) != set(version.artifact_sha256)
            or set(version.artifact_refs) != set(version.artifact_blob_refs)
        ):
            raise ValueError("report version v2 artifact manifest is incomplete")
        if not (
            len(version.session_summary_refs)
            == len(version.session_summary_blob_refs)
            == len(version.session_summary_sha256)
        ):
            raise ValueError("report version v2 session-summary manifest is incomplete")
        for key, relative in version.artifact_refs.items():
            expected = version.artifact_sha256[key]
            try:
                blob = self.content_store.resolve_blob(
                    version.artifact_blob_refs[key],
                    expected_sha256=expected,
                )
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"report version artifact {key} failed validation"
                ) from exc
            self._validate_v2_view(
                relative,
                blob=blob,
                expected_sha256=expected,
                version_root=version_root,
                label=f"artifact {key}",
            )
        for view_ref, blob_ref, expected in zip(
            version.session_summary_refs,
            version.session_summary_blob_refs,
            version.session_summary_sha256,
            strict=True,
        ):
            try:
                blob = self.content_store.resolve_blob(
                    blob_ref,
                    expected_sha256=expected,
                )
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"report version session summary {view_ref} failed validation"
                ) from exc
            self._validate_v2_view(
                view_ref,
                blob=blob,
                expected_sha256=expected,
                version_root=version_root,
                label=f"session summary {view_ref}",
            )

    def _validate_v2_view(
        self,
        relative: Path,
        *,
        blob: Path,
        expected_sha256: str,
        version_root: Path,
        label: str,
    ) -> None:
        """Bind one v2 compatibility view to its owning version and CAS blob."""

        view = self.workspace / relative
        resolved_version_root = version_root.resolve()
        if (
            not view.absolute().is_relative_to(resolved_version_root)
            or not view.parent.resolve().is_relative_to(resolved_version_root)
            or not view.is_file()
        ):
            raise ValueError(
                f"report version {label} is outside its version directory"
            )
        if view.is_symlink():
            if view.resolve() != blob:
                raise ValueError(
                    f"report version {label} is not a controlled CAS symlink"
                )
        elif not view.resolve().is_relative_to(resolved_version_root):
            raise ValueError(
                f"report version {label} is not a regular version-local file"
            )
        if self._sha256(view) != expected_sha256:
            raise ValueError(f"report version {label} failed validation")

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
