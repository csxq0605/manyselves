"""Transactional project delivery contract for complete five-module reports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from docx import Document
from pydantic import Field, field_validator

from ..artifacts.content_store import ContentAddressedStore
from .agentic_models import StrictModel
from .models import REPORT_MODULE_IDS
from .parallel_runtime import (
    current_bound_project_write_lease,
    validate_bound_project_write_lease,
)


class DeliveryPackage(StrictModel):
    report_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    version: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    module_files: dict[str, Path]
    final_docx: Path
    report_state: Path
    source_index: Path
    source_index_docx: Path

    @field_validator("module_files")
    @classmethod
    def complete_module_set(cls, value: dict[str, Path]) -> dict[str, Path]:
        if set(value) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery requires exactly modules 2.1-2.5")
        return value


class DeliveryReceipt(StrictModel):
    success: bool
    delivery_dir: Path
    final_docx: Path
    module_files: dict[str, Path]
    report_state: Path
    source_index: Path
    source_index_docx: Path
    manifest_path: Path
    artifact_sha256: dict[str, str]
    storage_version: Literal[1, 2] = 1
    artifact_refs: dict[str, Path] = Field(default_factory=dict)
    trusted_handle_refs: dict[str, Path] = Field(default_factory=dict)
    project_lease_epoch: int | None = Field(default=None, ge=1)

    @field_validator("artifact_refs", "trusted_handle_refs")
    @classmethod
    def artifact_refs_are_project_relative(
        cls, value: dict[str, Path]
    ) -> dict[str, Path]:
        if any(path.is_absolute() or ".." in path.parts for path in value.values()):
            raise ValueError("delivery artifact refs must be project-relative")
        return value


class ProjectDelivery:
    """Validate all artifacts before atomically publishing a success package."""

    def __init__(self, delivery_root: Path):
        self.delivery_root = Path(delivery_root)
        self.workspace = self._infer_workspace(self.delivery_root)
        self.content_store = ContentAddressedStore(self.workspace)

    def deliver(self, package: DeliveryPackage) -> DeliveryReceipt:
        validate_bound_project_write_lease(self.workspace)
        active_lease = current_bound_project_write_lease(self.workspace)
        self._validate_inputs(package)
        destination = self.delivery_root / f"{package.report_id}-{package.version}"
        if destination.exists():
            return self._reuse_existing(package, destination)

        self.delivery_root.parent.mkdir(parents=True, exist_ok=True)
        staging_parent = self.delivery_root.parent
        with tempfile.TemporaryDirectory(prefix=".manyselves-delivery-", dir=staging_parent) as temp_dir:
            staging = Path(temp_dir) / destination.name
            staging_targets = self._artifact_paths(staging)
            final_targets = self._artifact_paths(destination)
            sources = self._package_sources(package)
            hashes: dict[str, str] = {}
            artifact_refs: dict[str, Path] = {}
            trusted_handle_refs: dict[str, Path] = {}
            for key, source in sources.items():
                blob = self.content_store.ingest_file(source)
                trusted = self.content_store.issue_trusted_handle(
                    blob,
                    lineage_id=(
                        f"delivery:{package.report_id}:{package.version}:{key}"
                    ),
                )
                self.content_store.link_trusted_view(
                    trusted,
                    staging_targets[key],
                    final_path=final_targets[key],
                )
                hashes[key] = blob.sha256
                artifact_refs[key] = blob.relative_path
                trusted_handle_refs[key] = trusted.manifest_ref
            manifest = {
                "manifest_version": 2,
                "storage": "sha256-cas",
                "report_id": package.report_id,
                "version": package.version,
                "status": "success",
                "project_lease_epoch": (
                    active_lease.lease_epoch if active_lease is not None else None
                ),
                "modules": list(REPORT_MODULE_IDS),
                "artifacts": hashes,
                "artifact_refs": {
                    key: path.as_posix() for key, path in artifact_refs.items()
                },
                "trusted_handle_refs": {
                    key: path.as_posix()
                    for key, path in trusted_handle_refs.items()
                },
            }
            manifest_path = staging / "delivery-manifest.json"
            manifest_path.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
                encoding="utf-8",
            )
            hashes["manifest"] = self._sha256(manifest_path)

            self.delivery_root.mkdir(parents=True, exist_ok=True)
            validate_bound_project_write_lease(self.workspace)
            os.replace(staging, destination)

        return DeliveryReceipt(
            success=True,
            delivery_dir=destination,
            final_docx=final_targets["final_docx"],
            module_files={
                module_id: final_targets[f"module:{module_id}"]
                for module_id in REPORT_MODULE_IDS
            },
            report_state=final_targets["report_state"],
            source_index=final_targets["source_index"],
            source_index_docx=final_targets["source_index_docx"],
            manifest_path=destination / manifest_path.name,
            artifact_sha256=hashes,
            storage_version=2,
            artifact_refs=artifact_refs,
            trusted_handle_refs=trusted_handle_refs,
            project_lease_epoch=(
                active_lease.lease_epoch if active_lease is not None else None
            ),
        )

    def _reuse_existing(
        self, package: DeliveryPackage, destination: Path
    ) -> DeliveryReceipt:
        """Reuse only a complete package whose bytes equal the requested inputs."""

        manifest_path = destination / "delivery-manifest.json"
        targets = self._artifact_paths(destination)
        final_docx = targets["final_docx"]
        report_state = targets["report_state"]
        source_index = targets["source_index"]
        source_index_docx = targets["source_index_docx"]
        module_files = {
            module_id: targets[f"module:{module_id}"] for module_id in REPORT_MODULE_IDS
        }
        required = [
            manifest_path,
            final_docx,
            report_state,
            source_index,
            source_index_docx,
            *module_files.values(),
        ]
        missing = [str(path) for path in required if not path.is_file()]
        if missing:
            raise ValueError(
                "existing delivery is partial and cannot be resumed safely: "
                f"{missing}"
            )
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        expected_hashes = {
            key: self._sha256(source)
            for key, source in self._package_sources(package).items()
        }
        actual_hashes = {
            key: self._sha256(path) for key, path in targets.items()
        }
        storage_version = manifest.get("manifest_version", 1)
        artifact_refs: dict[str, Path] = {}
        trusted_handle_refs: dict[str, Path] = {}
        refs_valid = storage_version == 1
        if storage_version == 2:
            raw_refs = manifest.get("artifact_refs")
            if isinstance(raw_refs, dict) and set(raw_refs) == set(actual_hashes):
                artifact_refs = {
                    str(key): Path(str(value)) for key, value in raw_refs.items()
                }
                try:
                    for key, relative in artifact_refs.items():
                        self.content_store.resolve_blob(
                            relative,
                            expected_sha256=actual_hashes[key],
                        )
                except (FileNotFoundError, ValueError):
                    refs_valid = False
                else:
                    refs_valid = True
            raw_handles = manifest.get("trusted_handle_refs")
            if isinstance(raw_handles, dict) and set(raw_handles) == set(actual_hashes):
                trusted_handle_refs = {
                    str(key): Path(str(value)) for key, value in raw_handles.items()
                }
                try:
                    for key, relative in trusted_handle_refs.items():
                        handle = self.content_store.load_trusted_handle(relative)
                        blob = self.content_store.resolve_trusted_handle(
                            handle,
                            expected_sha256=actual_hashes[key],
                        )
                        if targets[key].resolve() != blob:
                            raise ValueError(
                                "delivery view does not match trusted blob handle"
                            )
                except (FileNotFoundError, ValueError):
                    trusted_handle_refs = {}
        if (
            storage_version not in {1, 2}
            or manifest.get("report_id") != package.report_id
            or manifest.get("version") != package.version
            or manifest.get("status") != "success"
            or manifest.get("modules") != list(REPORT_MODULE_IDS)
            or manifest.get("artifacts") != actual_hashes
            or actual_hashes != expected_hashes
            or not refs_valid
        ):
            raise ValueError(
                "existing delivery does not match the current run artifacts; "
                "refusing to overwrite or synthesize a receipt"
            )
        return DeliveryReceipt(
            success=True,
            delivery_dir=destination,
            final_docx=final_docx,
            module_files=module_files,
            report_state=report_state,
            source_index=source_index,
            source_index_docx=source_index_docx,
            manifest_path=manifest_path,
            artifact_sha256={
                **actual_hashes,
                "manifest": self._sha256(manifest_path),
            },
            storage_version=storage_version,
            artifact_refs=artifact_refs,
            trusted_handle_refs=trusted_handle_refs,
            project_lease_epoch=manifest.get("project_lease_epoch"),
        )

    @staticmethod
    def _artifact_paths(root: Path) -> dict[str, Path]:
        return {
            "final_docx": root / "配电安全专家咨询报告.docx",
            "report_state": root / "report-state.json",
            "source_index": root / "证据与来源索引.md",
            "source_index_docx": root / "证据与来源索引.docx",
            **{
                f"module:{module_id}": root / "modules" / f"{module_id}.md"
                for module_id in REPORT_MODULE_IDS
            },
        }

    @staticmethod
    def _package_sources(package: DeliveryPackage) -> dict[str, Path]:
        return {
            "final_docx": package.final_docx,
            "report_state": package.report_state,
            "source_index": package.source_index,
            "source_index_docx": package.source_index_docx,
            **{
                f"module:{module_id}": package.module_files[module_id]
                for module_id in REPORT_MODULE_IDS
            },
        }

    @staticmethod
    def _infer_workspace(delivery_root: Path) -> Path:
        resolved = Path(delivery_root).resolve()
        for candidate in (resolved, *resolved.parents):
            if candidate.name == "Work":
                return candidate.parent
        return resolved.parent

    @staticmethod
    def _validate_inputs(package: DeliveryPackage) -> None:
        if set(package.module_files) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery requires exactly modules 2.1-2.5")
        paths = [
            *package.module_files.values(),
            package.final_docx,
            package.report_state,
            package.source_index,
            package.source_index_docx,
        ]
        missing = [str(path) for path in paths if not Path(path).is_file()]
        if missing:
            raise FileNotFoundError(f"delivery artifacts missing: {missing}")
        if package.final_docx.suffix.lower() != ".docx":
            raise ValueError("final report must be a .docx file")
        if package.source_index.suffix.lower() != ".md":
            raise ValueError("source index must be a Markdown file")
        if package.source_index_docx.suffix.lower() != ".docx":
            raise ValueError("source index companion must be a .docx file")
        try:
            Document(package.final_docx)
        except Exception as exc:
            raise ValueError("final report is not Word/WPS-openable") from exc
        try:
            Document(package.source_index_docx)
        except Exception as exc:
            raise ValueError("source index companion is not Word/WPS-openable") from exc
        try:
            state = json.loads(package.report_state.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("report state must be valid UTF-8 JSON") from exc
        if not isinstance(state, dict):
            raise ValueError("report state must be a JSON object")

    @staticmethod
    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()
