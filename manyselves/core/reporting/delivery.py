"""Transactional project delivery contract for complete five-module reports."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Literal

from docx import Document
from pydantic import Field, field_validator, model_validator

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)

from ..artifacts.content_store import ContentAddressedStore, ContentBlob
from ..artifacts.storage_policy import CasPolicy, StorageMode, StoredArtifact
from .agentic_models import StrictModel
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


class MaterializedDeliveryReceipt(StrictModel):
    """Current delivery contract: ordinary files and no digest/CAS identity."""

    success: bool
    delivery_dir: Path
    final_docx: Path
    module_files: dict[str, Path]
    report_state: Path
    source_index: Path
    source_index_docx: Path
    manifest_path: Path

    @model_validator(mode="after")
    def source_indexes_are_delivery_views(self) -> "MaterializedDeliveryReceipt":
        delivery_dir = Path(self.delivery_dir)
        if (
            Path(self.source_index) != delivery_dir / "证据与来源索引.md"
            or Path(self.source_index_docx) != delivery_dir / "证据与来源索引.docx"
        ):
            raise ValueError("source index paths must be current delivery view paths")
        return self


class DeliveryReceipt(StrictModel):
    success: bool
    delivery_dir: Path
    final_docx: Path
    module_files: dict[str, Path]
    report_state: Path
    source_index: Path
    source_index_docx: Path
    manifest_path: Path
    artifact_sha256: dict[str, str] = Field(
        default_factory=dict,
        description="Legacy ignored metadata; materialized delivery writers omit it.",
    )
    # ``storage_version`` is deliberately widened for readers while newly
    # published receipts always use version 3.  Older v1/v2 manifests are
    # read as-is and are never rewritten or migrated.
    storage_version: Literal[1, 2, 3] = 1
    # v3 records the physical mode for every package artifact.  The mapping is
    # empty for a legacy receipt, whose manifest predates mixed storage.
    artifact_storage: dict[str, Literal["materialized", "cas"]] = Field(
        default_factory=dict
    )
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

    @model_validator(mode="after")
    def source_indexes_are_delivery_views(self) -> "DeliveryReceipt":
        """Keep source-index fields bound to this package's view paths.

        CAS references belong in ``artifact_refs``.  Exposing one of those
        canonical/opaque paths through the public source-index fields would
        make a receipt unusable when CAS is unavailable and would also allow a
        caller to smuggle an external path into a delivery result.
        """

        delivery_dir = Path(self.delivery_dir)
        if (
            Path(self.source_index) != delivery_dir / "证据与来源索引.md"
            or Path(self.source_index_docx) != delivery_dir / "证据与来源索引.docx"
        ):
            raise ValueError("source index paths must be current delivery view paths")
        return self


class ProjectDelivery:
    """Validate all artifacts before atomically publishing a success package."""

    def __init__(self, delivery_root: Path):
        self.delivery_root = Path(delivery_root)
        self.workspace = self._infer_workspace(self.delivery_root)
        self.content_store = ContentAddressedStore(self.workspace)
        self.storage_policy = CasPolicy()

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
            artifact_storage: dict[str, Literal["materialized", "cas"]] = {}
            artifact_refs: dict[str, Path] = {}
            trusted_handle_refs: dict[str, Path] = {}
            cas_views_to_rebase: list[tuple[StoredArtifact, Path, Path]] = []
            for key, source in sources.items():
                stored = self.content_store.persist_with_policy(
                    source,
                    destination=staging_targets[key],
                    logical_role=self._logical_role(key),
                    policy=self.storage_policy,
                )
                # ``persist_with_policy`` has no final-path argument.  A CAS
                # view is therefore created relative to the staging directory;
                # rebase only that compatibility symlink before the directory
                # is atomically renamed.  The immutable canonical blob is
                # never touched.
                if stored.storage_mode is StorageMode.CAS:
                    cas_views_to_rebase.append(
                        (stored, staging_targets[key], final_targets[key])
                    )
                hashes[key] = stored.sha256
                artifact_storage[key] = stored.storage_mode.value
                if stored.storage_mode is StorageMode.CAS:
                    if stored.blob_ref is None:
                        raise ValueError(f"CAS artifact has no blob reference: {key}")
                    artifact_refs[key] = Path(stored.blob_ref)
                    trusted = self._issue_trusted_handle(
                        stored,
                        lineage_id=(
                            f"delivery:{package.report_id}:{package.version}:{key}"
                        ),
                    )
                    trusted_handle_refs[key] = trusted.manifest_ref

            manifest = {
                "manifest_version": 3,
                "storage": "mixed",
                "report_id": package.report_id,
                "version": package.version,
                "status": "success",
                "project_lease_epoch": (
                    active_lease.lease_epoch if active_lease is not None else None
                ),
                "modules": list(REPORT_MODULE_IDS),
                # ``artifacts`` is retained as a read-only v1/v2 spelling;
                # v3 consumers must use the explicit artifact_sha256 map.
                "artifact_sha256": hashes,
                "artifacts": hashes,
                "artifact_storage": artifact_storage,
                "artifact_refs": {
                    key: path.as_posix() for key, path in artifact_refs.items()
                },
                "trusted_handle_refs": {
                    key: path.as_posix()
                    for key, path in trusted_handle_refs.items()
                },
            }
            # Persist the manifest through the same policy boundary as other
            # lifecycle metadata.  This gives it an atomic materialized write
            # and a post-write SHA-256 without placing it in CAS.
            manifest_source = Path(temp_dir) / "delivery-manifest-source.json"
            manifest_source.write_text(
                json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
                + "\n",
                encoding="utf-8",
            )
            manifest_path = staging / "delivery-manifest.json"
            stored_manifest = self.content_store.persist_with_policy(
                manifest_source,
                destination=manifest_path,
                logical_role="manifest",
                policy=self.storage_policy,
            )
            if stored_manifest.storage_mode is not StorageMode.MATERIALIZED:
                raise ValueError("delivery manifest must remain materialized")
            manifest_sha256 = stored_manifest.sha256

            # Ensure every published view already has the identity promised by
            # the manifest before the final atomic directory rename.
            actual_hashes = {
                key: self._sha256(path) for key, path in staging_targets.items()
            }
            if actual_hashes != hashes:
                raise ValueError("delivery staging view failed SHA-256 validation")

            # Keep the staging symlink valid through the validation above, then
            # rebase it to the final package location immediately before the
            # atomic directory rename.
            for stored, staged_path, final_path in cas_views_to_rebase:
                self._rebase_staged_cas_view(stored, staged_path, final_path)

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
            artifact_sha256={**hashes, "manifest": manifest_sha256},
            storage_version=3,
            artifact_storage={**artifact_storage, "manifest": "materialized"},
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
        artifact_storage: dict[str, Literal["materialized", "cas"]] = {}

        common_valid = (
            storage_version in {1, 2, 3}
            and manifest.get("report_id") == package.report_id
            and manifest.get("version") == package.version
            and manifest.get("status") == "success"
            and manifest.get("modules") == list(REPORT_MODULE_IDS)
            and actual_hashes == expected_hashes
        )
        if storage_version == 1:
            # Legacy snapshots had no storage metadata.  Validate bytes only
            # and return a v1 receipt without synthesizing/migrating refs.
            refs_valid = manifest.get("artifacts") == actual_hashes
        elif storage_version == 2:
            refs_valid = self._validate_v2_refs(
                manifest,
                targets,
                actual_hashes,
            )
            if refs_valid:
                artifact_refs = {
                    str(key): Path(str(value))
                    for key, value in manifest.get("artifact_refs", {}).items()
                }
                raw_handles = manifest.get("trusted_handle_refs")
                if isinstance(raw_handles, dict) and set(raw_handles) == set(actual_hashes):
                    trusted_handle_refs = {
                        str(key): Path(str(value)) for key, value in raw_handles.items()
                    }
                    if not self._validate_trusted_handles(
                        trusted_handle_refs,
                        targets,
                        actual_hashes,
                    ):
                        trusted_handle_refs = {}
        elif storage_version == 3:
            refs_valid, artifact_storage, artifact_refs = self._validate_v3_manifest(
                manifest,
                targets,
                actual_hashes,
            )
            raw_handles = manifest.get("trusted_handle_refs")
            if refs_valid and isinstance(raw_handles, dict):
                trusted_handle_refs = {
                    str(key): Path(str(value)) for key, value in raw_handles.items()
                }
                if not set(trusted_handle_refs).issubset(set(artifact_refs)) or not self._validate_trusted_handles(
                    trusted_handle_refs,
                    targets,
                    actual_hashes,
                ):
                    refs_valid = False

        if not common_valid or not refs_valid:
            raise ValueError(
                "existing delivery does not match the current run artifacts; "
                "refusing to overwrite or synthesize a receipt"
            )
        manifest_sha256 = self._sha256(manifest_path)
        if storage_version == 1:
            artifact_storage = {
                **{key: "materialized" for key in actual_hashes},
                "manifest": "materialized",
            }
        elif storage_version == 2:
            artifact_storage = {
                **{key: "cas" for key in actual_hashes},
                "manifest": "materialized",
            }
        else:
            artifact_storage = {**artifact_storage, "manifest": "materialized"}
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
                "manifest": manifest_sha256,
            },
            storage_version=storage_version,
            artifact_storage=artifact_storage,
            artifact_refs=artifact_refs,
            trusted_handle_refs=trusted_handle_refs,
            project_lease_epoch=manifest.get("project_lease_epoch"),
        )

    def _validate_v2_refs(
        self,
        manifest: dict,
        targets: dict[str, Path],
        actual_hashes: dict[str, str],
    ) -> bool:
        """Validate the all-CAS reference set used by a v2 manifest."""

        if manifest.get("artifacts") != actual_hashes:
            return False
        raw_refs = manifest.get("artifact_refs")
        if not isinstance(raw_refs, dict) or set(raw_refs) != set(actual_hashes):
            return False
        try:
            refs = {str(key): Path(str(value)) for key, value in raw_refs.items()}
            for key, relative in refs.items():
                blob = self.content_store.resolve_blob(
                    relative,
                    expected_sha256=actual_hashes[key],
                )
                # A v2 CAS view may be a symlink (the normal macOS path) or a
                # compatibility copy on a filesystem that disallows symlinks;
                # in both cases the view bytes were already checked above.
                if targets[key].is_symlink() and targets[key].resolve() != blob:
                    return False
        except (FileNotFoundError, ValueError, KeyError):
            return False
        return True

    def _validate_v3_manifest(
        self,
        manifest: dict,
        targets: dict[str, Path],
        actual_hashes: dict[str, str],
    ) -> tuple[
        bool,
        dict[str, Literal["materialized", "cas"]],
        dict[str, Path],
    ]:
        """Validate mixed v3 storage metadata and return its typed maps."""

        raw_hashes = manifest.get("artifact_sha256")
        if not isinstance(raw_hashes, dict) or raw_hashes != actual_hashes:
            return False, {}, {}
        # ``artifacts`` is a compatibility alias and, when present, must not
        # disagree with the explicit v3 hash map.
        if "artifacts" in manifest and manifest.get("artifacts") != raw_hashes:
            return False, {}, {}

        raw_storage = manifest.get("artifact_storage")
        if not isinstance(raw_storage, dict) or set(raw_storage) != set(actual_hashes):
            return False, {}, {}
        if any(value not in {"materialized", "cas"} for value in raw_storage.values()):
            return False, {}, {}
        storage: dict[str, Literal["materialized", "cas"]] = {
            str(key): value for key, value in raw_storage.items()
        }

        raw_refs = manifest.get("artifact_refs")
        if not isinstance(raw_refs, dict):
            return False, {}, {}
        cas_keys = {key for key, mode in storage.items() if mode == "cas"}
        if set(raw_refs) != cas_keys:
            return False, {}, {}
        refs = {str(key): Path(str(value)) for key, value in raw_refs.items()}
        delivery_dir = targets["final_docx"].parent.resolve()
        try:
            for key, mode in storage.items():
                target = targets[key]
                resolved_view = target.resolve()
                if mode == "cas":
                    relative = refs[key]
                    blob = self.content_store.resolve_blob(
                        relative,
                        expected_sha256=actual_hashes[key],
                    )
                    if target.is_symlink() and resolved_view != blob:
                        return False, {}, {}
                else:
                    # Materialized views must be ordinary files in the current
                    # delivery directory.  This blocks a forged external or
                    # opaque symlink even when its bytes happen to hash-match.
                    if key in refs or target.is_symlink():
                        return False, {}, {}
                    if not resolved_view.is_relative_to(delivery_dir):
                        return False, {}, {}
        except (FileNotFoundError, ValueError, KeyError):
            return False, {}, {}
        return True, storage, refs

    def _validate_trusted_handles(
        self,
        handles: dict[str, Path],
        targets: dict[str, Path],
        actual_hashes: dict[str, str],
    ) -> bool:
        try:
            for key, relative in handles.items():
                if key not in actual_hashes:
                    return False
                handle = self.content_store.load_trusted_handle(relative)
                blob = self.content_store.resolve_trusted_handle(
                    handle,
                    expected_sha256=actual_hashes[key],
                )
                if targets[key].is_symlink() and targets[key].resolve() != blob:
                    return False
        except (FileNotFoundError, ValueError, KeyError):
            return False
        return True

    @staticmethod
    def _logical_role(key: str) -> str:
        if key == "final_docx":
            return "final_docx"
        if key == "report_state":
            return "state"
        if key == "source_index":
            return "source_index"
        if key == "source_index_docx":
            return "source_index_docx"
        if key.startswith("module:"):
            return "module"
        return "artifact"

    def _rebase_staged_cas_view(
        self,
        stored: StoredArtifact,
        staged_path: Path,
        final_path: Path,
    ) -> None:
        """Make a policy-created CAS symlink valid after staging is renamed."""

        if stored.storage_mode is not StorageMode.CAS or not staged_path.is_symlink():
            return
        if stored.blob_ref is None:
            raise ValueError("CAS artifact has no blob reference")
        relative = Path(stored.blob_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("CAS artifact reference must be project-relative")
        canonical = (self.workspace / relative).resolve()
        if not canonical.is_file() or not canonical.is_relative_to(
            self.content_store.root.resolve()
        ):
            raise ValueError("CAS artifact reference is outside canonical storage")
        staged_path.unlink()
        # ``/var`` is a symlink to ``/private/var`` on macOS.  Resolve the
        # not-yet-created final parent before computing the relative target so
        # the view remains valid after ``os.replace`` (and does not gain a
        # duplicated ``private`` path component).
        final_parent = Path(final_path).resolve(strict=False).parent
        staged_path.symlink_to(os.path.relpath(canonical, start=final_parent))

    def _issue_trusted_handle(
        self,
        stored: StoredArtifact,
        *,
        lineage_id: str,
    ):
        """Issue a compatibility handle for one policy-selected CAS artifact."""

        if stored.storage_mode is not StorageMode.CAS or stored.blob_ref is None:
            raise ValueError("trusted handles are only valid for CAS artifacts")
        relative = Path(stored.blob_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("CAS artifact reference must be project-relative")
        blob_path = self.workspace / relative
        blob = ContentBlob(
            sha256=stored.sha256,
            size=stored.size_bytes,
            path=blob_path,
            relative_path=relative,
        )
        return self.content_store.issue_trusted_handle(blob, lineage_id=lineage_id)

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
