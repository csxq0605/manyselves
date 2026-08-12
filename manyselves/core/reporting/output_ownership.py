"""Typed ownership pointer for the workspace's currently visible final report."""

from __future__ import annotations

import hashlib
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal, Self

from pydantic import ConfigDict, Field, field_validator, model_validator

from ..artifacts.content_store import ContentAddressedStore
from .delivery import DeliveryReceipt
from .models import ReportingModel
from .store import ReportingStore
from .versions import ReportVersionStore

OUTPUT_OWNER_REF = Path("Work/output-owner.json")
FINAL_REPORT_DOCX_REF = Path("Outputs/Reports/配电安全专家咨询报告.docx")


class OutputOwnerError(ValueError):
    """Raised when the shared output owner is absent, stale, or inconsistent."""


class OutputOwner(ReportingModel):
    """Identity and integrity binding for the shared final DOCX publication."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    run_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9._-]+$")
    report_version_id: str = Field(
        min_length=1, pattern=r"^[A-Za-z0-9._-]+$"
    )
    final_docx_ref: Path = FINAL_REPORT_DOCX_REF
    final_docx_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    delivery_receipt_ref: Path
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
        return self


def build_output_owner(
    *,
    run_id: str,
    report_version_id: str,
    final_docx_sha256: str,
    delivery_receipt_ref: Path,
    final_docx_ref: Path = FINAL_REPORT_DOCX_REF,
    published_at: datetime | None = None,
) -> OutputOwner:
    """Build the canonical pointer after delivery completion has persisted."""

    values = {
        "run_id": run_id,
        "report_version_id": report_version_id,
        "final_docx_ref": final_docx_ref,
        "final_docx_sha256": final_docx_sha256,
        "delivery_receipt_ref": delivery_receipt_ref,
    }
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
