"""Typed ownership pointer for the workspace's four visible report artifacts."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..artifacts.content_store import ContentAddressedStore
from .delivery import DeliveryReceipt
from .models import ReportingModel
from .parallel_runtime import validate_bound_project_write_lease
from .store import ReportingStore
from .versions import ReportVersionStore

OUTPUT_OWNER_REF = Path("Work/output-owner.json")
OUTPUT_SET_ROOT = Path("Work/output-sets")
CURRENT_OUTPUT_SET_REF = Path("Outputs/Reports/.current")
FINAL_REPORT_MARKDOWN_REF = Path("Outputs/Reports/配电安全专家咨询报告.md")
FINAL_REPORT_DOCX_REF = Path("Outputs/Reports/配电安全专家咨询报告.docx")
SOURCE_INDEX_MARKDOWN_REF = Path("Outputs/Reports/证据与来源索引.md")
SOURCE_INDEX_DOCX_REF = Path("Outputs/Reports/证据与来源索引.docx")
OUTPUT_ARTIFACT_REFS = {
    "final_markdown": FINAL_REPORT_MARKDOWN_REF,
    "final_docx": FINAL_REPORT_DOCX_REF,
    "source_index": SOURCE_INDEX_MARKDOWN_REF,
    "source_index_docx": SOURCE_INDEX_DOCX_REF,
}


class OutputOwnerError(ValueError):
    """Raised when the shared output owner is absent, stale, or inconsistent."""


class OutputOwner(ReportingModel):
    """Identity and integrity binding for one shared four-artifact publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1, 2] = 1
    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    report_version_id: str = Field(
        min_length=1, pattern=r"^[A-Za-z0-9._-]+$"
    )
    final_docx_ref: Path = FINAL_REPORT_DOCX_REF
    final_docx_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    delivery_receipt_ref: Path
    output_set_ref: Path | None = None
    artifact_refs: dict[str, Path] = Field(default_factory=dict)
    artifact_sha256: dict[str, str] = Field(default_factory=dict)
    published_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )

    @field_validator("run_id", "report_version_id")
    @classmethod
    def identity_is_a_safe_path_segment(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("output owner identity must be a safe path segment")
        return value

    @field_validator("final_docx_ref")
    @classmethod
    def final_docx_is_the_shared_output(cls, value: Path) -> Path:
        if value != FINAL_REPORT_DOCX_REF:
            raise ValueError("output owner must bind the canonical final DOCX")
        return value

    @field_validator("delivery_receipt_ref")
    @classmethod
    def receipt_is_project_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("output owner receipt must be project-relative")
        return value

    @field_validator("published_at")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("output owner publication timestamp needs a timezone")
        return value

    @model_validator(mode="after")
    def identities_and_receipt_match(self) -> Self:
        if self.report_version_id != self.run_id:
            raise ValueError("output owner report version must belong to its run")
        expected_receipt = Path(
            f"Work/runs/{self.run_id}/delivery-receipt.json"
        )
        if self.delivery_receipt_ref != expected_receipt:
            raise ValueError("output owner receipt must belong to its run")
        if self.schema_version == 1:
            if self.output_set_ref is not None or self.artifact_refs or self.artifact_sha256:
                raise ValueError("legacy output owner cannot carry an output set")
            return self
        expected_set = OUTPUT_SET_ROOT / self.run_id
        if self.output_set_ref != expected_set:
            raise ValueError("output owner set must belong to its run")
        if self.artifact_refs != OUTPUT_ARTIFACT_REFS:
            raise ValueError("output owner must bind the canonical four-output set")
        if set(self.artifact_sha256) != set(OUTPUT_ARTIFACT_REFS):
            raise ValueError("output owner hashes must cover the canonical four-output set")
        if any(
            len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)
            for value in self.artifact_sha256.values()
        ):
            raise ValueError("output owner artifact hashes must be SHA-256 values")
        if self.artifact_sha256["final_docx"] != self.final_docx_sha256:
            raise ValueError("output owner final DOCX hash conflicts with its output set")
        return self


def build_output_owner(
    *,
    run_id: str,
    report_version_id: str,
    final_docx_sha256: str,
    delivery_receipt_ref: Path,
    final_docx_ref: Path = FINAL_REPORT_DOCX_REF,
    artifact_sha256: dict[str, str] | None = None,
    output_set_ref: Path | None = None,
    published_at: datetime | None = None,
) -> OutputOwner:
    """Build the canonical pointer after delivery completion has persisted."""

    values = {
        "schema_version": 2 if artifact_sha256 is not None else 1,
        "run_id": run_id,
        "report_version_id": report_version_id,
        "final_docx_ref": final_docx_ref,
        "final_docx_sha256": final_docx_sha256,
        "delivery_receipt_ref": delivery_receipt_ref,
    }
    if artifact_sha256 is not None:
        values.update(
            {
                "output_set_ref": output_set_ref or OUTPUT_SET_ROOT / run_id,
                "artifact_refs": OUTPUT_ARTIFACT_REFS,
                "artifact_sha256": artifact_sha256,
            }
        )
    if published_at is not None:
        values["published_at"] = published_at
    return OutputOwner.model_validate(values)


class OutputOwnerStore:
    """Atomically publish and strictly verify the current output owner."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.version_store = ReportVersionStore(self.workspace)
        self.content_store = ContentAddressedStore(self.workspace)
        self.path = self.workspace / OUTPUT_OWNER_REF

    def load(self) -> OutputOwner | None:
        """Load the typed pointer, returning ``None`` only when it is absent."""

        if not self.path.exists() and not self.path.is_symlink():
            return None
        if self.path.is_symlink() or not self.path.is_file():
            raise OutputOwnerError("output owner pointer is not a regular file")
        try:
            return OutputOwner.model_validate_json(
                self.path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise OutputOwnerError("output owner pointer is invalid") from exc

    def publish(self, owner: OutputOwner) -> Path:
        """Validate every binding, then atomically replace the owner pointer."""

        owner = OutputOwner.model_validate(owner)
        self._validate_binding(owner)
        path = self.store.write_json(
            OUTPUT_OWNER_REF.as_posix(), owner.model_dump(mode="json")
        )
        self.store.fsync_directory(path.parent)
        return path

    def publish_output_set(
        self,
        owner: OutputOwner,
        *,
        sources: dict[str, Path],
    ) -> Path:
        """Publish four stable report views, then commit their typed owner.

        Every immutable run gets one output-set directory.  The four public
        names are stable symlinks through one ``.current`` pointer, so after
        the one-time migration from legacy regular files, later publications
        switch the complete set with a single atomic symlink replacement.
        """

        owner = OutputOwner.model_validate(owner)
        if owner.schema_version != 2 or owner.output_set_ref is None:
            raise OutputOwnerError("four-output publication requires an owner v2 set")
        if set(sources) != set(OUTPUT_ARTIFACT_REFS):
            raise OutputOwnerError("output publication sources are incomplete")
        validate_bound_project_write_lease(self.workspace)
        resolved_sources = {
            key: self._workspace_path(path) for key, path in sources.items()
        }
        for key, source in resolved_sources.items():
            if not source.is_file() or self._sha256(source) != owner.artifact_sha256[key]:
                raise OutputOwnerError(
                    f"output publication source does not match its owner: {key}"
                )

        output_set = self._publish_immutable_output_set(owner, resolved_sources)
        self._publish_stable_output_links()
        self._publish_current_output_set(output_set)
        return self.publish(owner)

    def require(
        self,
        *,
        expected_run_id: str | None = None,
        expected_report_version_id: str | None = None,
        expected_final_docx_sha256: str | None = None,
        expected_delivery_receipt_ref: Path | None = None,
    ) -> OutputOwner:
        """Load and validate the live pointer plus optional caller bindings."""

        owner = self.load()
        if owner is None:
            raise OutputOwnerError("output owner pointer is missing")
        expected = {
            "run_id": expected_run_id,
            "report_version_id": expected_report_version_id,
            "final_docx_sha256": expected_final_docx_sha256,
            "delivery_receipt_ref": expected_delivery_receipt_ref,
        }
        for field_name, expected_value in expected.items():
            if expected_value is not None and getattr(owner, field_name) != expected_value:
                raise OutputOwnerError(
                    f"output owner {field_name} does not match the requested run"
                )
        self._validate_binding(owner)
        return owner

    def _validate_binding(self, owner: OutputOwner) -> None:
        final_docx = self._workspace_path(owner.final_docx_ref)
        if not final_docx.is_file() or self._sha256(final_docx) != owner.final_docx_sha256:
            raise OutputOwnerError("visible final DOCX does not match its owner")

        if owner.schema_version == 2:
            self._validate_visible_output_set(owner)

        receipt_path = self._workspace_path(owner.delivery_receipt_ref)
        if receipt_path.is_symlink() or not receipt_path.is_file():
            raise OutputOwnerError("output owner delivery receipt is missing")
        try:
            receipt = DeliveryReceipt.model_validate_json(
                receipt_path.read_text(encoding="utf-8")
            )
        except (OSError, ValueError) as exc:
            raise OutputOwnerError("output owner delivery receipt is invalid") from exc
        expected_delivery_root = (
            self.workspace / "Work" / "runs" / owner.run_id / "delivery"
        )
        delivery_dir = self._workspace_path(receipt.delivery_dir)
        receipt_docx = self._workspace_path(receipt.final_docx)
        receipt_blob_ref = receipt.artifact_refs.get("final_docx")
        if receipt.storage_version == 3 and receipt.artifact_storage.get(
            "final_docx"
        ) != "cas":
            raise OutputOwnerError(
                "output owner requires a CAS-backed final DOCX in the v3 receipt"
            )
        if receipt_blob_ref is None:
            raise OutputOwnerError(
                "output owner delivery receipt lacks a final DOCX blob reference"
            )
        try:
            receipt_blob = self.content_store.resolve_blob(
                receipt_blob_ref,
                expected_sha256=owner.final_docx_sha256,
            )
        except (FileNotFoundError, KeyError, ValueError) as exc:
            raise OutputOwnerError(
                "output owner delivery receipt lacks a valid final DOCX CAS binding"
            ) from exc
        if (
            not receipt.success
            or receipt.storage_version not in {2, 3}
            or delivery_dir.is_symlink()
            or not delivery_dir.is_dir()
            or not delivery_dir.is_relative_to(expected_delivery_root)
            or not receipt_docx.is_file()
            or not receipt_docx.is_relative_to(delivery_dir)
            or (
                receipt_docx.is_symlink()
                and receipt_docx.resolve() != receipt_blob
            )
            or receipt.artifact_sha256.get("final_docx")
            != owner.final_docx_sha256
            or self._sha256(receipt_docx) != owner.final_docx_sha256
        ):
            raise OutputOwnerError(
                "output owner is not bound to its successful delivery receipt"
            )

        try:
            version_path = self._workspace_path(
                Path(
                    f"Work/report-versions/{owner.report_version_id}/version.json"
                )
            )
            if version_path.is_symlink() or not version_path.is_file():
                raise OutputOwnerError(
                    "output owner report version is not a regular file"
                )
            version = self.version_store.load(owner.report_version_id)
        except (FileNotFoundError, ValueError) as exc:
            raise OutputOwnerError("output owner report version is invalid") from exc
        if (
            version.run_id != owner.run_id
            or version.storage_version not in {2, 3}
            or (
                version.storage_version == 3
                and version.artifact_storage.get("final_docx") != "cas"
            )
            or version.artifact_sha256.get("final_docx")
            != owner.final_docx_sha256
            or version.artifact_blob_refs.get("final_docx")
            != receipt_blob_ref
            or version.artifact_sha256.get("delivery_receipt")
            != self._sha256(receipt_path)
        ):
            raise OutputOwnerError(
                "output owner is not bound to its report version"
            )
        if owner.schema_version == 2:
            for output_key, version_key in {
                "final_markdown": "canonical_markdown",
                "final_docx": "final_docx",
                "source_index": "source_index",
                "source_index_docx": "source_index_docx",
            }.items():
                expected = owner.artifact_sha256[output_key]
                if version.artifact_sha256.get(version_key) != expected:
                    raise OutputOwnerError(
                        f"output owner artifact is not bound to its report version: {output_key}"
                    )
            for receipt_key in ("final_docx", "source_index", "source_index_docx"):
                if receipt.artifact_sha256.get(receipt_key) != owner.artifact_sha256[receipt_key]:
                    raise OutputOwnerError(
                        f"output owner artifact is not bound to its delivery receipt: {receipt_key}"
                    )

    def _publish_immutable_output_set(
        self,
        owner: OutputOwner,
        sources: dict[str, Path],
    ) -> Path:
        assert owner.output_set_ref is not None
        destination = self.workspace / owner.output_set_ref
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.is_symlink() or not destination.is_dir():
                raise OutputOwnerError("output set path is not an immutable directory")
            self._validate_output_set_files(destination, owner.artifact_sha256)
            return destination

        staging = Path(
            tempfile.mkdtemp(
                prefix=f".{owner.run_id}.",
                suffix=".output-set",
                dir=destination.parent,
            )
        )
        try:
            for key, public_ref in OUTPUT_ARTIFACT_REFS.items():
                target = staging / public_ref.name
                # Build the link as it will resolve after the staging
                # directory is renamed into its immutable final location.
                # Report-version artifacts are immutable and hash-bound, so
                # this avoids materializing another copy of the large DOCX.
                target.symlink_to(
                    Path(os.path.relpath(sources[key], start=destination))
                )
            os.chmod(staging, 0o555)
            os.replace(staging, destination)
            self.store.fsync_directory(destination.parent)
            self._validate_output_set_files(destination, owner.artifact_sha256)
        finally:
            if staging.exists():
                shutil.rmtree(staging)
        return destination

    def _publish_stable_output_links(self) -> None:
        reports = self.workspace / CURRENT_OUTPUT_SET_REF.parent
        reports.mkdir(parents=True, exist_ok=True)
        for public_ref in OUTPUT_ARTIFACT_REFS.values():
            target = self.workspace / public_ref
            expected_link = Path(CURRENT_OUTPUT_SET_REF.name) / public_ref.name
            if target.is_symlink() and Path(os.readlink(target)) == expected_link:
                continue
            staged = target.with_name(f".{target.name}.{uuid.uuid4().hex}.output-link")
            try:
                staged.symlink_to(expected_link)
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
        self.store.fsync_directory(reports)

    def _publish_current_output_set(self, output_set: Path) -> None:
        current = self.workspace / CURRENT_OUTPUT_SET_REF
        expected_link = Path(os.path.relpath(output_set, start=current.parent))
        staged = current.with_name(f".{current.name}.{uuid.uuid4().hex}.current-link")
        try:
            staged.symlink_to(expected_link, target_is_directory=True)
            if staged.resolve() != output_set.resolve():
                raise OutputOwnerError("staged current output set resolves incorrectly")
            os.replace(staged, current)
            self.store.fsync_directory(current.parent)
        finally:
            staged.unlink(missing_ok=True)

    def _validate_visible_output_set(self, owner: OutputOwner) -> None:
        assert owner.output_set_ref is not None
        output_set = self._workspace_path(owner.output_set_ref)
        current = self.workspace / CURRENT_OUTPUT_SET_REF
        if (
            not output_set.is_dir()
            or not current.is_symlink()
            or current.resolve() != output_set.resolve()
        ):
            raise OutputOwnerError("visible output set does not match its owner")
        self._validate_output_set_files(output_set, owner.artifact_sha256)
        for key, public_ref in OUTPUT_ARTIFACT_REFS.items():
            visible = self.workspace / public_ref
            expected_link = Path(CURRENT_OUTPUT_SET_REF.name) / public_ref.name
            if (
                not visible.is_symlink()
                or Path(os.readlink(visible)) != expected_link
                or not visible.is_file()
                or self._sha256(visible) != owner.artifact_sha256[key]
            ):
                raise OutputOwnerError(
                    f"visible output artifact does not match its owner: {key}"
                )

    def _validate_output_set_files(
        self,
        output_set: Path,
        expected_hashes: dict[str, str],
    ) -> None:
        expected_names = {path.name for path in OUTPUT_ARTIFACT_REFS.values()}
        if {path.name for path in output_set.iterdir()} != expected_names:
            raise OutputOwnerError("output set does not contain exactly four artifacts")
        for key, public_ref in OUTPUT_ARTIFACT_REFS.items():
            path = output_set / public_ref.name
            if not path.is_file() or self._sha256(path) != expected_hashes[key]:
                raise OutputOwnerError(f"immutable output set hash mismatch: {key}")

    def _workspace_path(self, relative: Path) -> Path:
        candidate = Path(relative)
        if candidate.is_absolute():
            absolute = candidate
        else:
            absolute = self.workspace / candidate
        absolute = Path(os.path.abspath(absolute))
        if not absolute.is_relative_to(self.workspace):
            raise OutputOwnerError("output owner path escapes the workspace")
        current = self.workspace
        for part in absolute.parent.relative_to(self.workspace).parts:
            current /= part
            if current.is_symlink():
                raise OutputOwnerError(
                    "output owner path uses a symbolic-link ancestor"
                )
        if (
            not absolute.parent.resolve().is_relative_to(self.workspace)
            or not absolute.resolve().is_relative_to(self.workspace)
        ):
            raise OutputOwnerError("output owner path escapes the workspace")
        return absolute

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
