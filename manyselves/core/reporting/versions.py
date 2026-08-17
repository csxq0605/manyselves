"""Immutable, restorable report-version snapshots."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from collections.abc import Mapping, Sequence
from typing import Literal

from pydantic import ConfigDict, Field, field_validator

from ..artifacts.content_store import ContentAddressedStore
from ..artifacts.storage_policy import CasPolicy, StorageMode
from .delivery import DeliveryReceipt
from .models import REPORT_MODULE_IDS, ReportingModel
from .parallel_runtime import (
    current_bound_project_write_lease,
    validate_bound_project_write_lease,
)


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
    storage_version: Literal[1, 2, 3] = 1
    # v3 records the physical mode for every artifact.  Legacy v1/v2
    # manifests leave this map empty and are read without migration.
    artifact_storage: dict[str, Literal["materialized", "cas"]] = Field(
        default_factory=dict
    )
    artifact_blob_refs: dict[str, Path] = Field(default_factory=dict)
    artifact_trusted_handle_refs: dict[str, Path] = Field(default_factory=dict)
    skill_provenance: list[SkillProvenance]
    session_summary_refs: list[Path]
    session_summary_blob_refs: list[Path] = Field(default_factory=list)
    session_summary_sha256: list[str] = Field(default_factory=list)
    session_summary_trusted_handle_refs: list[Path] = Field(default_factory=list)
    project_lease_epoch: int | None = Field(default=None, ge=1)

    @field_validator(
        "artifact_refs",
        "artifact_blob_refs",
        "artifact_trusted_handle_refs",
        "session_summary_refs",
        "session_summary_blob_refs",
        "session_summary_trusted_handle_refs",
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

    def publish(
        self,
        version: ReportVersion,
        *,
        trusted_handle_refs: dict[str, Path] | None = None,
        session_summary_trusted_handle_refs: list[Path] | None = None,
    ) -> ReportVersion:
        """Publish a snapshot, reusing same-lineage verified handles when supplied."""

        trusted_handle_refs = trusted_handle_refs or {}
        session_summary_trusted_handle_refs = (
            session_summary_trusted_handle_refs or []
        )
        validate_bound_project_write_lease(self.workspace)
        active_lease = current_bound_project_write_lease(self.workspace)
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
            published_handle_refs: dict[str, Path] = {}
            for index, (key, source) in enumerate(sorted(sources.items()), start=1):
                safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-_") or "artifact"
                target = artifact_dir / f"{index:02d}-{safe_key}{source.suffix}"
                final_target = destination / "artifacts" / target.name
                supplied_handle_ref = trusted_handle_refs.get(key)
                if supplied_handle_ref is not None:
                    trusted = self.content_store.load_trusted_handle(
                        supplied_handle_ref
                    )
                    blob_path = self.content_store.resolve_trusted_handle(trusted)
                    if source.resolve() != blob_path:
                        raise ValueError(
                            f"trusted handle does not identify version source: {key}"
                        )
                else:
                    blob = self.content_store.ingest_file(source)
                    trusted = self.content_store.issue_trusted_handle(
                        blob,
                        lineage_id=f"report-version:{version_id}:artifact:{key}",
                    )
                self.content_store.link_trusted_view(
                    trusted, target, final_path=final_target
                )
                relative = final_target.relative_to(self.workspace)
                snapshot_refs[key] = relative
                hashes[key] = trusted.sha256
                blob_refs[key] = trusted.relative_path
                published_handle_refs[key] = trusted.manifest_ref
            summary_dir = staging / "session-summaries"
            summary_dir.mkdir()
            summary_refs: list[Path] = []
            summary_blob_refs: list[Path] = []
            summary_hashes: list[str] = []
            summary_handle_refs: list[Path] = []
            for index, source in enumerate(summary_sources, start=1):
                target = summary_dir / f"{index:02d}-{source.name}"
                final_target = destination / "session-summaries" / target.name
                supplied_handle_ref = (
                    session_summary_trusted_handle_refs[index - 1]
                    if index <= len(session_summary_trusted_handle_refs)
                    else None
                )
                if supplied_handle_ref is not None:
                    trusted = self.content_store.load_trusted_handle(
                        supplied_handle_ref
                    )
                    blob_path = self.content_store.resolve_trusted_handle(trusted)
                    if source.resolve() != blob_path:
                        raise ValueError(
                            "trusted handle does not identify session summary source"
                        )
                else:
                    blob = self.content_store.ingest_file(source)
                    trusted = self.content_store.issue_trusted_handle(
                        blob,
                        lineage_id=(
                            f"report-version:{version_id}:session-summary:{index}"
                        ),
                    )
                self.content_store.link_trusted_view(
                    trusted, target, final_path=final_target
                )
                summary_refs.append(final_target.relative_to(self.workspace))
                summary_blob_refs.append(trusted.relative_path)
                summary_hashes.append(trusted.sha256)
                summary_handle_refs.append(trusted.manifest_ref)
            published = version.model_copy(
                update={
                    "storage_version": 2,
                    "artifact_refs": snapshot_refs,
                    "artifact_sha256": hashes,
                    "artifact_blob_refs": blob_refs,
                    "artifact_trusted_handle_refs": published_handle_refs,
                    "session_summary_refs": summary_refs,
                    "session_summary_blob_refs": summary_blob_refs,
                    "session_summary_sha256": summary_hashes,
                    "session_summary_trusted_handle_refs": summary_handle_refs,
                    "project_lease_epoch": (
                        active_lease.lease_epoch
                        if active_lease is not None
                        else version.project_lease_epoch
                    ),
                }
            )
            (staging / "version.json").write_text(
                published.model_dump_json(indent=2) + "\n", encoding="utf-8"
            )
            validate_bound_project_write_lease(self.workspace)
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
            handle.write(
                json.dumps(
                    {
                        "version_id": version_id,
                        "project_lease_epoch": published.project_lease_epoch,
                    }
                )
                + "\n"
            )
            temporary_pointer = Path(handle.name)
        validate_bound_project_write_lease(self.workspace)
        os.replace(temporary_pointer, pointer)
        return published

    def publish_from_delivery(
        self,
        receipt: DeliveryReceipt,
        receipt_ref: Path,
        additional_artifacts: Mapping[str, Path] | None = None,
        session_summary_refs: Sequence[Path] | None = None,
        skill_provenance: Sequence[SkillProvenance] | None = None,
    ) -> ReportVersion:
        """Publish a mixed v3 snapshot from one typed delivery receipt.

        The package paths are intentionally taken only from the typed receipt
        fields.  ``receipt.artifact_refs`` contains canonical CAS references,
        not delivery view paths, and therefore is never used as a source path.
        CAS package artifacts reuse the receipt's verified blob/handle; small
        materialized artifacts are copied atomically into this version's own
        directory.  Additional artifacts are persisted using the explicit CAS
        policy and the receipt JSON itself is kept as a materialized artifact.
        """

        if not isinstance(receipt, DeliveryReceipt):
            raise TypeError("publish_from_delivery requires a typed DeliveryReceipt")
        if not receipt.success:
            raise ValueError("cannot publish a failed delivery receipt")

        validate_bound_project_write_lease(self.workspace)
        active_lease = current_bound_project_write_lease(self.workspace)
        receipt_source_ref = self._workspace_path(receipt_ref)
        manifest_identity, _manifest = self._load_delivery_manifest(receipt)
        version_id = self._safe_id(manifest_identity)
        destination = self.root / version_id

        delivery_dir = self._workspace_path(receipt.delivery_dir)
        delivery_root = delivery_dir.resolve()
        if not delivery_dir.is_dir() or not delivery_root.is_relative_to(self.workspace):
            raise ValueError("delivery receipt directory is outside the project")

        package_sources: dict[str, Path] = {
            "final_docx": self._workspace_path(receipt.final_docx),
            "report_state": self._workspace_path(receipt.report_state),
            "source_index": self._workspace_path(receipt.source_index),
            "source_index_docx": self._workspace_path(receipt.source_index_docx),
        }
        if set(receipt.module_files) != set(REPORT_MODULE_IDS):
            raise ValueError("delivery receipt must contain exactly modules 2.1-2.5")
        package_sources.update(
            {
                f"module:{module_id}": self._workspace_path(path)
                for module_id, path in receipt.module_files.items()
            }
        )

        # Validate each typed delivery view against the receipt hash and CAS
        # metadata before any version-local file is created.
        metadata: dict[str, dict[str, object]] = {}
        package_hashes: dict[str, str] = {}
        for key, source in package_sources.items():
            expected = receipt.artifact_sha256.get(key)
            if expected is None:
                raise ValueError(f"delivery receipt is missing artifact hash: {key}")
            mode = self._receipt_storage_mode(receipt, key)
            blob, trusted = self._validate_delivery_source(
                receipt,
                key,
                source,
                mode=mode,
                expected_sha256=expected,
                delivery_dir=delivery_dir,
                delivery_root=delivery_root,
            )
            package_hashes[key] = expected
            metadata[key] = {
                "source": source,
                "mode": mode,
                "blob": blob,
                "trusted": trusted,
            }

        receipt_source = self._workspace_file(receipt_source_ref, label="delivery receipt")
        metadata["delivery_receipt"] = {
            "source": receipt_source,
            "mode": "materialized",
            "blob": None,
            "trusted": None,
        }

        extras = dict(additional_artifacts or {})
        for key, raw_source in extras.items():
            key = str(key)
            if not key or key in metadata:
                raise ValueError(f"duplicate or empty report-version artifact key: {key!r}")
            source = self._workspace_file(raw_source, label=f"artifact {key}")
            decision = CasPolicy().decide(
                source,
                logical_role=self._storage_role(key, source),
                size_bytes=source.stat().st_size,
                suffix=source.suffix,
            )
            metadata[key] = {
                "source": source,
                "mode": decision.mode.value,
                "blob": None,
                "trusted": None,
            }

        summaries = [
            self._workspace_file(path, label="session summary")
            for path in (session_summary_refs or [])
        ]
        summary_hashes = [self._sha256(path) for path in summaries]

        # Compute the identity maps used for idempotent resume.  Receipt CAS
        # hashes were verified above; additional artifacts are hashed here only
        # when they are not already represented by an ingested blob.
        requested_hashes = dict(package_hashes)
        requested_hashes["delivery_receipt"] = self._sha256(receipt_source)
        requested_storage = {
            key: str(value["mode"]) for key, value in metadata.items()
        }
        for key, item in metadata.items():
            if key in package_hashes or key == "delivery_receipt":
                continue
            requested_hashes[key] = self._sha256(item["source"])  # type: ignore[arg-type]

        if destination.exists():
            published = self.load(version_id)
            if (
                published.run_id != version_id
                or published.parent_version_id is not None
                or published.artifact_sha256 != requested_hashes
                or published.artifact_storage != requested_storage
                or published.skill_provenance != list(skill_provenance or [])
                or published.session_summary_sha256 != summary_hashes
            ):
                raise ValueError(
                    "existing report version is partial or differs from the current delivery"
                )
            return published

        self.root.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".report-version-v3-", dir=self.root) as temp:
            staging = Path(temp) / version_id
            artifact_dir = staging / "artifacts"
            artifact_dir.mkdir(parents=True)
            snapshot_refs: dict[str, Path] = {}
            hashes: dict[str, str] = {}
            storage: dict[str, Literal["materialized", "cas"]] = {}
            blob_refs: dict[str, Path] = {}
            handle_refs: dict[str, Path] = {}

            for index, key in enumerate(sorted(metadata), start=1):
                item = metadata[key]
                source = item["source"]  # type: ignore[assignment]
                mode = str(item["mode"])
                safe_key = re.sub(r"[^A-Za-z0-9._-]+", "-", key).strip("-_") or "artifact"
                target = artifact_dir / f"{index:02d}-{safe_key}{Path(source).suffix}"
                final_target = destination / "artifacts" / target.name

                if mode == StorageMode.CAS.value:
                    blob = item.get("blob")
                    trusted = item.get("trusted")
                    if blob is None:
                        blob = self.content_store.ingest_file(Path(source))
                    if trusted is None:
                        trusted = self.content_store.issue_trusted_handle(
                            blob,
                            lineage_id=f"report-version:{version_id}:artifact:{key}",
                        )
                    self.content_store.link_trusted_view(
                        trusted,
                        target,
                        final_path=final_target,
                    )
                    hashes[key] = blob.sha256
                    blob_refs[key] = blob.relative_path
                    handle_refs[key] = trusted.manifest_ref
                    storage[key] = "cas"
                else:
                    digest, _size = self.content_store._atomic_copy_verified(
                        Path(source),
                        target,
                    )
                    hashes[key] = digest
                    storage[key] = "materialized"
                snapshot_refs[key] = final_target.relative_to(self.workspace)

            if hashes != requested_hashes or storage != requested_storage:
                raise ValueError(
                    "report version staging artifacts changed during publication"
                )

            summary_dir = staging / "session-summaries"
            summary_dir.mkdir(parents=True)
            published_summary_refs: list[Path] = []
            published_summary_blob_refs: list[Path] = []
            published_summary_hashes: list[str] = []
            published_summary_handles: list[Path] = []
            for index, source in enumerate(summaries, start=1):
                target = summary_dir / f"{index:02d}-{source.name}"
                final_target = destination / "session-summaries" / target.name
                blob = self.content_store.ingest_file(source)
                trusted = self.content_store.issue_trusted_handle(
                    blob,
                    lineage_id=f"report-version:{version_id}:session-summary:{index}",
                )
                self.content_store.link_trusted_view(
                    trusted,
                    target,
                    final_path=final_target,
                )
                published_summary_refs.append(final_target.relative_to(self.workspace))
                published_summary_blob_refs.append(blob.relative_path)
                published_summary_hashes.append(blob.sha256)
                published_summary_handles.append(trusted.manifest_ref)

            published = ReportVersion(
                version_id=version_id,
                run_id=version_id,
                parent_version_id=None,
                artifact_refs=snapshot_refs,
                artifact_sha256=hashes,
                storage_version=3,
                artifact_storage=storage,
                artifact_blob_refs=blob_refs,
                artifact_trusted_handle_refs=handle_refs,
                skill_provenance=list(skill_provenance or []),
                session_summary_refs=published_summary_refs,
                session_summary_blob_refs=published_summary_blob_refs,
                session_summary_sha256=published_summary_hashes,
                session_summary_trusted_handle_refs=published_summary_handles,
                project_lease_epoch=(
                    active_lease.lease_epoch
                    if active_lease is not None
                    else receipt.project_lease_epoch
                ),
            )
            (staging / "version.json").write_text(
                published.model_dump_json(indent=2) + "\n",
                encoding="utf-8",
            )
            validate_bound_project_write_lease(self.workspace)
            os.replace(staging, destination)

        self._write_latest_pointer(published)
        return published

    def _workspace_path(self, value: Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.workspace / path

    def _workspace_file(self, value: Path, *, label: str) -> Path:
        path = self._workspace_path(value)
        resolved = path.resolve()
        if (
            not resolved.is_relative_to(self.workspace)
            or not resolved.is_file()
            or not path.is_file()
        ):
            raise FileNotFoundError(f"{label} is missing or outside the project: {value}")
        return path

    def _load_delivery_manifest(
        self,
        receipt: DeliveryReceipt,
    ) -> tuple[str, dict[str, object]]:
        """Read and authenticate the typed delivery manifest identity."""

        delivery_dir = self._workspace_path(receipt.delivery_dir)
        delivery_root = delivery_dir.resolve()
        manifest = self._workspace_file(receipt.manifest_path, label="delivery manifest")
        lexical = Path(os.path.abspath(manifest))
        if (
            not lexical.is_relative_to(Path(os.path.abspath(delivery_dir)))
            or manifest.is_symlink()
            or not manifest.resolve().is_relative_to(delivery_root)
        ):
            raise ValueError("delivery manifest is outside its delivery directory")
        expected_hash = receipt.artifact_sha256.get("manifest")
        if expected_hash is None or self._sha256(manifest) != expected_hash:
            raise ValueError("delivery manifest hash does not match the receipt")
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("delivery manifest is not valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("delivery manifest must be a JSON object")
        if payload.get("manifest_version") != 3:
            raise ValueError("publish_from_delivery requires a v3 delivery manifest")
        report_id = str(payload.get("report_id", ""))
        package_version = str(payload.get("version", ""))
        if not re.fullmatch(r"[A-Za-z0-9._-]+", report_id):
            raise ValueError("delivery manifest report_id is missing or unsafe")
        if not re.fullmatch(r"[A-Za-z0-9._-]+", package_version):
            raise ValueError("delivery manifest version is missing or unsafe")
        if delivery_dir.name != f"{report_id}-{package_version}":
            raise ValueError("delivery manifest identity does not match its directory")

        package_hashes = {
            key: value
            for key, value in receipt.artifact_sha256.items()
            if key != "manifest"
        }
        manifest_hashes = payload.get("artifact_sha256")
        if not isinstance(manifest_hashes, dict) or manifest_hashes != package_hashes:
            raise ValueError("delivery manifest artifact hashes disagree with the receipt")
        manifest_storage = payload.get("artifact_storage")
        receipt_storage = {
            key: value
            for key, value in receipt.artifact_storage.items()
            if key != "manifest"
        }
        if not isinstance(manifest_storage, dict) or manifest_storage != receipt_storage:
            raise ValueError("delivery manifest storage map disagrees with the receipt")
        manifest_refs = payload.get("artifact_refs")
        receipt_refs = {
            key: value.as_posix() for key, value in receipt.artifact_refs.items()
        }
        if not isinstance(manifest_refs, dict) or manifest_refs != receipt_refs:
            raise ValueError("delivery manifest blob map disagrees with the receipt")
        return report_id, payload

    @staticmethod
    def _storage_role(key: str, source: Path) -> str:
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
        if key in {"delivery_receipt", "delivery_manifest", "manifest"}:
            return "manifest"
        return key or source.stem

    @staticmethod
    def _receipt_storage_mode(
        receipt: DeliveryReceipt,
        key: str,
    ) -> Literal["materialized", "cas"]:
        raw = receipt.artifact_storage.get(key)
        if raw is None:
            # Legacy receipts predate mixed metadata.  A canonical reference
            # is the only safe indication that the view was CAS-backed.
            return "cas" if key in receipt.artifact_refs else "materialized"
        if raw not in {"materialized", "cas"}:
            raise ValueError(f"delivery receipt has invalid storage mode: {key}")
        return raw

    def _validate_delivery_source(
        self,
        receipt: DeliveryReceipt,
        key: str,
        source: Path,
        *,
        mode: Literal["materialized", "cas"],
        expected_sha256: str,
        delivery_dir: Path,
        delivery_root: Path,
    ) -> tuple[object | None, object | None]:
        """Validate one typed delivery view and return reusable CAS proofs."""

        if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
            raise ValueError(f"delivery receipt artifact {key} has an invalid hash")
        lexical = Path(os.path.abspath(source))
        delivery_lexical = Path(os.path.abspath(delivery_dir))
        if (
            not lexical.is_relative_to(delivery_lexical)
            or not source.is_file()
            or not source.resolve().is_relative_to(self.workspace)
        ):
            raise ValueError(f"delivery receipt artifact {key} is outside its delivery directory")

        if mode == "cas":
            blob_ref = receipt.artifact_refs.get(key)
            if blob_ref is None:
                raise ValueError(f"delivery receipt CAS artifact has no blob reference: {key}")
            trusted = None
            try:
                trusted_ref = receipt.trusted_handle_refs.get(key)
                if trusted_ref is not None:
                    trusted = self.content_store.load_trusted_handle(trusted_ref)
                    blob = self.content_store.resolve_trusted_handle(
                        trusted,
                        expected_sha256=expected_sha256,
                    )
                    if Path(blob_ref) != trusted.relative_path:
                        raise ValueError("delivery CAS blob and trusted handle disagree")
                else:
                    blob = self.content_store.resolve_blob(
                        blob_ref,
                        expected_sha256=expected_sha256,
                    )
            except (FileNotFoundError, ValueError) as exc:
                raise ValueError(
                    f"delivery receipt CAS artifact failed validation: {key}"
                ) from exc
            if source.is_symlink():
                if source.resolve() != blob:
                    raise ValueError(f"delivery CAS view points to another blob: {key}")
            elif self._sha256(source) != expected_sha256:
                raise ValueError(f"delivery CAS view failed validation: {key}")
            return self.content_store._blob(
                expected_sha256,
                blob.stat().st_size,
                blob,
            ), trusted

        # Materialized views have no canonical blob reference and must be
        # independently readable in the current delivery directory.
        if key in receipt.artifact_refs or source.is_symlink():
            raise ValueError(f"delivery materialized artifact is not local: {key}")
        if not source.resolve().is_relative_to(delivery_root):
            raise ValueError(f"delivery materialized artifact escaped its directory: {key}")
        if self._sha256(source) != expected_sha256:
            raise ValueError(f"delivery materialized artifact failed validation: {key}")
        return None, None

    def _write_latest_pointer(self, published: ReportVersion) -> None:
        pointer = self.root / "latest.json"
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=".latest-",
            suffix=".tmp",
            dir=self.root,
            delete=False,
        ) as handle:
            handle.write(
                json.dumps(
                    {
                        "version_id": published.version_id,
                        "project_lease_epoch": published.project_lease_epoch,
                    }
                )
                + "\n"
            )
            temporary_pointer = Path(handle.name)
        validate_bound_project_write_lease(self.workspace)
        os.replace(temporary_pointer, pointer)

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
        elif version.storage_version == 3:
            self._validate_v3(version, version_root=version_root)
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
        if version.artifact_trusted_handle_refs and set(
            version.artifact_trusted_handle_refs
        ) != set(version.artifact_refs):
            raise ValueError("report version trusted artifact handles are incomplete")
        if not (
            len(version.session_summary_refs)
            == len(version.session_summary_blob_refs)
            == len(version.session_summary_sha256)
        ):
            raise ValueError("report version v2 session-summary manifest is incomplete")
        if version.session_summary_trusted_handle_refs and len(
            version.session_summary_trusted_handle_refs
        ) != len(version.session_summary_refs):
            raise ValueError("report version trusted session handles are incomplete")
        for key, relative in version.artifact_refs.items():
            expected = version.artifact_sha256[key]
            try:
                trusted_ref = version.artifact_trusted_handle_refs.get(key)
                if trusted_ref is not None:
                    trusted = self.content_store.load_trusted_handle(trusted_ref)
                    blob = self.content_store.resolve_trusted_handle(
                        trusted,
                        expected_sha256=expected,
                    )
                else:
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
                trusted=trusted_ref is not None,
            )
        for index, (view_ref, blob_ref, expected) in enumerate(zip(
            version.session_summary_refs,
            version.session_summary_blob_refs,
            version.session_summary_sha256,
            strict=True,
        )):
            try:
                trusted_ref = (
                    version.session_summary_trusted_handle_refs[index]
                    if version.session_summary_trusted_handle_refs
                    else None
                )
                if trusted_ref is not None:
                    trusted = self.content_store.load_trusted_handle(trusted_ref)
                    blob = self.content_store.resolve_trusted_handle(
                        trusted,
                        expected_sha256=expected,
                    )
                else:
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
                trusted=trusted_ref is not None,
            )

    def _validate_v2_view(
        self,
        relative: Path,
        *,
        blob: Path,
        expected_sha256: str,
        version_root: Path,
        label: str,
        trusted: bool = False,
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
        if (not trusted or not view.is_symlink()) and self._sha256(view) != expected_sha256:
            raise ValueError(f"report version {label} failed validation")

    def _validate_v3(
        self,
        version: ReportVersion,
        *,
        version_root: Path,
    ) -> None:
        """Validate a mixed-storage v3 manifest without rewriting it.

        v3 deliberately separates three maps: ``artifact_refs`` always points
        to a view owned by this version directory, ``artifact_sha256`` covers
        every view, and ``artifact_blob_refs`` contains only the CAS subset.
        A CAS view may be a symlink to its canonical project blob; a
        materialized view must remain an ordinary file below this version root.
        """

        artifact_keys = set(version.artifact_refs)
        if artifact_keys != set(version.artifact_sha256) or artifact_keys != set(
            version.artifact_storage
        ):
            raise ValueError("report version v3 artifact manifest is incomplete")
        if any(
            mode not in {"materialized", "cas"}
            for mode in version.artifact_storage.values()
        ):
            raise ValueError("report version v3 artifact storage mode is invalid")
        cas_keys = {
            key
            for key, mode in version.artifact_storage.items()
            if mode == "cas"
        }
        if set(version.artifact_blob_refs) != cas_keys:
            raise ValueError("report version v3 blob manifest is not the CAS subset")
        if version.artifact_trusted_handle_refs and set(
            version.artifact_trusted_handle_refs
        ) != cas_keys:
            raise ValueError("report version v3 trusted artifact handles are incomplete")

        resolved_version_root = version_root.resolve()
        for key, relative in version.artifact_refs.items():
            expected = version.artifact_sha256[key]
            if not re.fullmatch(r"[0-9a-f]{64}", expected):
                raise ValueError(f"report version artifact {key} has an invalid hash")
            view = self.workspace / relative
            # The lexical view path must be owned by this version.  Resolve
            # only the parent here: a CAS view itself is allowed to resolve to
            # the canonical content root, while a symlinked parent is not.
            if (
                view.is_symlink()
                or not view.absolute().is_relative_to(resolved_version_root)
                or not view.parent.resolve().is_relative_to(resolved_version_root)
                or not view.is_file()
            ):
                # ``view.is_symlink()`` is handled below for CAS entries; the
                # first branch keeps a malformed path from being accepted as
                # a materialized file before its mode is known.
                if not view.absolute().is_relative_to(resolved_version_root) or not view.parent.resolve().is_relative_to(
                    resolved_version_root
                ) or not view.is_file():
                    raise ValueError(
                        f"report version artifact {key} is outside its version directory"
                    )

            mode = version.artifact_storage[key]
            if mode == "cas":
                try:
                    blob = self.content_store.resolve_blob(
                        version.artifact_blob_refs[key],
                        expected_sha256=expected,
                    )
                except (FileNotFoundError, ValueError) as exc:
                    raise ValueError(
                        f"report version artifact {key} failed CAS validation"
                    ) from exc
                if view.is_symlink() and view.resolve() != blob:
                    raise ValueError(
                        f"report version artifact {key} is not a controlled CAS symlink"
                    )
                if not view.is_symlink() and self._sha256(view) != expected:
                    raise ValueError(f"report version artifact {key} failed validation")
                trusted_ref = version.artifact_trusted_handle_refs.get(key)
                if trusted_ref is not None:
                    try:
                        trusted = self.content_store.load_trusted_handle(trusted_ref)
                        trusted_blob = self.content_store.resolve_trusted_handle(
                            trusted,
                            expected_sha256=expected,
                        )
                    except (FileNotFoundError, ValueError) as exc:
                        raise ValueError(
                            f"report version artifact {key} trusted handle failed validation"
                        ) from exc
                    if trusted_blob != blob:
                        raise ValueError(
                            f"report version artifact {key} trusted handle points to another blob"
                        )
            else:
                if key in version.artifact_blob_refs or view.is_symlink():
                    raise ValueError(
                        f"report version artifact {key} materialized view is not local"
                    )
                if self._sha256(view) != expected:
                    raise ValueError(f"report version artifact {key} failed validation")

        self._validate_v3_session_summaries(version, version_root=version_root)

    def _validate_v3_session_summaries(
        self,
        version: ReportVersion,
        *,
        version_root: Path,
    ) -> None:
        """Validate the legacy all-CAS session-summary sidecar in a v3 file."""

        if not (
            len(version.session_summary_refs)
            == len(version.session_summary_blob_refs)
            == len(version.session_summary_sha256)
        ):
            raise ValueError("report version v3 session-summary manifest is incomplete")
        if version.session_summary_trusted_handle_refs and len(
            version.session_summary_trusted_handle_refs
        ) != len(version.session_summary_refs):
            raise ValueError("report version v3 trusted session handles are incomplete")
        for index, (view_ref, blob_ref, expected) in enumerate(
            zip(
                version.session_summary_refs,
                version.session_summary_blob_refs,
                version.session_summary_sha256,
                strict=True,
            )
        ):
            trusted_ref = (
                version.session_summary_trusted_handle_refs[index]
                if version.session_summary_trusted_handle_refs
                else None
            )
            try:
                if trusted_ref is not None:
                    trusted = self.content_store.load_trusted_handle(trusted_ref)
                    blob = self.content_store.resolve_trusted_handle(
                        trusted,
                        expected_sha256=expected,
                    )
                else:
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
                trusted=trusted_ref is not None,
            )

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
