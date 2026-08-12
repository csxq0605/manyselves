"""Explicit, reversible storage compaction for completed report outputs."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import uuid
import zipfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator, Literal

from docx import Document
from docx.opc.exceptions import PackageNotFoundError
from pydantic import Field, field_validator

from ..artifacts.content_store import ContentAddressedStore
from .delivery import DeliveryReceipt
from .locks import ReportingWriterBusyError, exclusive_reporting_writer_lock
from .models import REPORT_MODULE_IDS, ReportingModel
from .output_ownership import OutputOwnerStore
from .store import ReportingStore
from .versions import ReportVersionStore

StorageCompactionAction = Literal["dry_run", "apply", "rollback"]
StorageCompactionStatus = Literal[
    "planned",
    "intent",
    "completed",
    "rolled_back",
    "recovery_required",
]
StorageOutputState = Literal["materialized", "cas_view"]
StorageCompletionKind = Literal["initial", "revision"]


class StorageCompactionRecord(ReportingModel):
    """Auditable state of one output-storage compaction operation."""

    schema_version: Literal[1] = 1
    operation_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    generated_at: datetime = Field(
        default_factory=lambda: datetime.now(timezone.utc)
    )
    run_id: str = Field(pattern=r"^[A-Za-z0-9._-]+$")
    completion_kind: StorageCompletionKind
    action: StorageCompactionAction
    status: StorageCompactionStatus
    dry_run: bool
    pre_state: StorageOutputState
    post_state: StorageOutputState | None = None
    target_ref: Path
    blob_ref: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    logical_size_bytes: int = Field(ge=0)
    allocated_size_bytes: int = Field(ge=0)
    target_inode: int = Field(ge=0)
    target_link_count: int = Field(ge=1)
    allocation_estimate_method: Literal[
        "st_blocks_x_512_zero_reclaim_when_hardlinked"
    ] = "st_blocks_x_512_zero_reclaim_when_hardlinked"
    estimated_reclaimed_logical_bytes: int = Field(ge=0)
    estimated_reclaimed_allocated_bytes: int = Field(ge=0)
    materialized_logical_bytes: int = Field(ge=0)
    materialized_allocated_bytes: int = Field(ge=0)
    receipt_ref: Path
    version_ref: Path
    run_result_ref: Path
    owner_ref: Path = Path("Work/output-owner.json")
    owner_verified: bool
    mutation_allowed: bool
    validation: list[str]
    error: str | None = Field(default=None, max_length=4_000)

    @field_validator("run_id")
    @classmethod
    def run_id_is_one_real_segment(cls, value: str) -> str:
        if value in {".", ".."}:
            raise ValueError("run_id must be a real path segment")
        return value

    @field_validator(
        "target_ref",
        "blob_ref",
        "receipt_ref",
        "version_ref",
        "run_result_ref",
        "owner_ref",
    )
    @classmethod
    def references_are_project_relative(cls, value: Path) -> Path:
        if value.is_absolute() or ".." in value.parts:
            raise ValueError("storage compaction references must be project-relative")
        return value


class StorageCompactionError(ValueError):
    """Raised when a run is not safe to compact."""


@dataclass(frozen=True)
class _ValidatedOutput:
    run_id: str
    completion_kind: StorageCompletionKind
    target: Path
    target_ref: Path
    blob_ref: Path
    blob: Path
    sha256: str
    logical_size: int
    allocated_size: int
    target_inode: int
    target_link_count: int
    state: StorageOutputState
    receipt_ref: Path
    version_ref: Path
    run_result_ref: Path
    owner_verified: bool


class CompletedRunOutputCompactor:
    """Promote one verified final DOCX to a CAS view, or materialize it again.

    This is an explicit, default-off maintenance operation.  Mutating actions
    require both the reporting workspace writer lock and an output-owner
    pointer that binds the shared visible DOCX to the requested completed run.
    A legacy workspace without that pointer can be inspected with ``dry_run``
    but cannot be mutated.
    """

    FINAL_DOCX_REF = Path("Outputs/Reports/配电安全专家咨询报告.docx")
    OWNER_REF = Path("Work/output-owner.json")
    MANIFEST_NAME = "storage-compaction.json"

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.content_store = ContentAddressedStore(self.workspace)
        self.version_store = ReportVersionStore(self.workspace)
        self.owner_store = OutputOwnerStore(self.workspace)
        self.store = ReportingStore(self.workspace)

    def execute(
        self,
        run_id: str,
        *,
        action: StorageCompactionAction = "dry_run",
    ) -> StorageCompactionRecord:
        """Validate and optionally compact or materialize the final DOCX."""

        safe_run_id = self._safe_run_id(run_id)
        self._validated_run_root(safe_run_id)
        if action not in {"dry_run", "apply", "rollback"}:
            raise ValueError(f"unknown storage compaction action: {action}")
        operation_id = uuid.uuid4().hex
        if action == "dry_run":
            validated = self._validate(safe_run_id, require_owner=False)
            return self._record(
                validated,
                action=action,
                status="planned",
                operation_id=operation_id,
            )

        try:
            with exclusive_reporting_writer_lock(self.workspace):
                with self._exclusive_run_lock(safe_run_id):
                    return self._execute_mutation(
                        safe_run_id,
                        action=action,
                        operation_id=operation_id,
                    )
        except ReportingWriterBusyError as exc:
            raise StorageCompactionError(
                "another reporting writer owns the shared workspace outputs"
            ) from exc

    def _execute_mutation(
        self,
        run_id: str,
        *,
        action: Literal["apply", "rollback"],
        operation_id: str,
    ) -> StorageCompactionRecord:
        validated = self._validate(run_id, require_owner=True)
        intent = self._record(
            validated,
            action=action,
            status="intent",
            operation_id=operation_id,
            post_state=None,
        )
        # The intent is durable before the shared output path changes.  It is
        # deliberately overwritten only by a terminal record for this same
        # operation.
        self._write_record(intent)

        try:
            self._mutate(validated, action=action)
            post = self._validate(run_id, require_owner=True)
            expected_state: StorageOutputState = (
                "cas_view" if action == "apply" else "materialized"
            )
            if post.state != expected_state:
                raise StorageCompactionError(
                    f"storage compaction ended in unexpected state: {post.state}"
                )
            completed = self._record(
                post,
                action=action,
                status="completed",
                operation_id=operation_id,
                pre_state=validated.state,
                metrics=validated,
            )
            self._write_record(completed)
            return completed
        except Exception as original_error:
            self._compensate_failed_mutation(
                validated,
                action=action,
                operation_id=operation_id,
                original_error=original_error,
            )
            raise StorageCompactionError(
                "storage compaction failed; the original output state was restored"
            ) from original_error

    def _mutate(
        self,
        output: _ValidatedOutput,
        *,
        action: Literal["apply", "rollback"],
    ) -> None:
        if action == "apply":
            self.content_store.promote_existing_to_view(
                output.blob_ref,
                output.target,
                expected_sha256=output.sha256,
            )
        else:
            self.content_store.materialize_view(
                output.blob_ref,
                output.target,
                expected_sha256=output.sha256,
            )

    def _compensate_failed_mutation(
        self,
        original: _ValidatedOutput,
        *,
        action: Literal["apply", "rollback"],
        operation_id: str,
        original_error: Exception,
    ) -> None:
        try:
            if original.state == "materialized":
                self.content_store.materialize_view(
                    original.blob_ref,
                    original.target,
                    expected_sha256=original.sha256,
                )
            else:
                self.content_store.promote_existing_to_view(
                    original.blob_ref,
                    original.target,
                    expected_sha256=original.sha256,
                )
            restored = self._validate(original.run_id, require_owner=True)
            if restored.state != original.state:
                raise StorageCompactionError(
                    "automatic compensation did not restore the pre-operation state"
                )
            rolled_back = self._record(
                restored,
                action=action,
                status="rolled_back",
                operation_id=operation_id,
                pre_state=original.state,
                metrics=original,
                error=self._error_text(original_error),
            )
            self._write_record(rolled_back)
            return
        except Exception as recovery_error:
            recovery_required = self._record(
                original,
                action=action,
                status="recovery_required",
                operation_id=operation_id,
                pre_state=original.state,
                post_state=self._detect_state(original),
                metrics=original,
                error=self._error_text(
                    original_error,
                    recovery_error=recovery_error,
                ),
            )
            # Best effort only: if the journal itself is unavailable, the
            # already-written intent remains the durable recovery signal.
            try:
                self._write_record(recovery_required)
            except Exception:
                pass
            raise StorageCompactionError(
                "storage compaction failed and automatic recovery is required"
            ) from recovery_error

    def _validate(
        self,
        run_id: str,
        *,
        require_owner: bool,
    ) -> _ValidatedOutput:
        run_root = self._validated_run_root(run_id)
        workflow_ref = Path(f"Work/runs/{run_id}/workflow-state.json")
        workflow = self._read_json(self.workspace / workflow_ref)
        completion_kind = self._completion_kind(workflow, run_id=run_id)

        completion_ref = Path(f"Work/runs/{run_id}/delivery-completion.json")
        if workflow.get("delivery_completion_ref") != completion_ref.as_posix():
            raise StorageCompactionError(
                "workflow state does not bind the expected delivery completion"
            )
        completion = self._read_json(self.workspace / completion_ref)
        receipt_ref = Path(f"Work/runs/{run_id}/delivery-receipt.json")
        version_id = completion.get("report_version_id")
        if (
            completion.get("run_id") != run_id
            or completion.get("status") != "completed"
            or completion.get("delivery_receipt_ref") != receipt_ref.as_posix()
            or not isinstance(version_id, str)
            or version_id != run_id
        ):
            raise StorageCompactionError(
                "delivery completion does not match the requested run"
            )

        receipt_path = self.workspace / receipt_ref
        try:
            receipt = DeliveryReceipt.model_validate(self._read_json(receipt_path))
        except (TypeError, ValueError) as exc:
            raise StorageCompactionError("delivery receipt is invalid") from exc
        if not receipt.success or receipt.storage_version not in {2, 3}:
            raise StorageCompactionError(
                "storage compaction requires a successful v2 or v3 delivery receipt"
            )

        delivery_root = self._workspace_path(receipt.delivery_dir)
        expected_delivery_root = run_root / "delivery"
        if (
            delivery_root.is_symlink()
            or not delivery_root.is_dir()
            or not delivery_root.is_relative_to(expected_delivery_root)
        ):
            raise StorageCompactionError(
                "delivery receipt points outside the requested run"
            )
        artifacts = self._receipt_artifacts(receipt)
        required_keys = {
            "final_docx",
            "report_state",
            "source_index",
            "source_index_docx",
            *(f"module:{module_id}" for module_id in REPORT_MODULE_IDS),
        }
        if set(artifacts) != required_keys or set(receipt.artifact_sha256) != (
            required_keys | {"manifest"}
        ):
            raise StorageCompactionError(
                "delivery receipt does not contain the complete artifact set"
            )

        # v2 receipts expose a canonical CAS reference for every package
        # artifact.  v3 keeps the same view paths and hash map but records a
        # mixed materialized/CAS policy; only its CAS subset appears in
        # ``artifact_refs``.  The final DOCX must remain CAS-backed because it
        # is the immutable object compacted by this operation and bound by the
        # output-owner pointer.
        if receipt.storage_version == 2:
            artifact_storage = {key: "cas" for key in required_keys}
            if set(receipt.artifact_refs) != required_keys:
                raise StorageCompactionError(
                    "v2 delivery receipt does not contain the complete CAS reference set"
                )
        else:
            if set(receipt.artifact_storage) != required_keys | {"manifest"}:
                raise StorageCompactionError(
                    "v3 delivery receipt does not contain the complete storage map"
                )
            artifact_storage = {
                key: receipt.artifact_storage[key] for key in required_keys
            }
            if receipt.artifact_storage.get("manifest") != "materialized":
                raise StorageCompactionError(
                    "v3 delivery manifest must remain materialized"
                )
            if artifact_storage.get("final_docx") != "cas":
                raise StorageCompactionError(
                    "v3 delivery final DOCX must be CAS-backed"
                )
            cas_keys = {
                key for key, mode in artifact_storage.items() if mode == "cas"
            }
            if set(receipt.artifact_refs) != cas_keys:
                raise StorageCompactionError(
                    "v3 delivery receipt CAS references do not match its storage map"
                )
            if any(mode not in {"cas", "materialized"} for mode in artifact_storage.values()):
                raise StorageCompactionError(
                    "v3 delivery receipt has an invalid storage mode"
                )

        actual_hashes: dict[str, str] = {}
        for key, raw_path in artifacts.items():
            path = self._workspace_path(raw_path)
            if not path.is_relative_to(delivery_root) or not path.is_file():
                raise StorageCompactionError(
                    f"delivery artifact is missing or outside delivery: {key}"
                )
            actual_hashes[key] = self._sha256(path)
        if actual_hashes != {
            key: receipt.artifact_sha256[key] for key in required_keys
        }:
            raise StorageCompactionError(
                "delivery artifact hashes do not match the receipt"
            )
        resolved_blobs: dict[str, Path] = {}
        try:
            for key in required_keys:
                artifact_path = self._workspace_path(artifacts[key])
                if artifact_storage[key] == "cas":
                    resolved_blobs[key] = self.content_store.resolve_blob(
                        receipt.artifact_refs[key],
                        expected_sha256=actual_hashes[key],
                    )
                    if (
                        artifact_path.is_symlink()
                        and artifact_path.resolve() != resolved_blobs[key]
                    ):
                        raise StorageCompactionError(
                            f"delivery artifact is not a controlled CAS view: {key}"
                        )
                else:
                    # Materialized v3 views must be ordinary files owned by
                    # this delivery directory, never references or symlinks.
                    if key in receipt.artifact_refs or artifact_path.is_symlink():
                        raise StorageCompactionError(
                            f"delivery materialized artifact is not local: {key}"
                        )
                    if not artifact_path.resolve().is_relative_to(delivery_root):
                        raise StorageCompactionError(
                            f"delivery materialized artifact escaped its directory: {key}"
                        )
        except (FileNotFoundError, ValueError) as exc:
            raise StorageCompactionError(
                "delivery CAS references failed validation"
            ) from exc

        manifest_path = self._workspace_path(receipt.manifest_path)
        if (
            not manifest_path.is_relative_to(delivery_root)
            or not manifest_path.is_file()
            or self._sha256(manifest_path) != receipt.artifact_sha256["manifest"]
        ):
            raise StorageCompactionError(
                "delivery manifest is missing or does not match the receipt"
            )
        manifest = self._read_json(manifest_path)
        expected_refs = {
            key: receipt.artifact_refs[key].as_posix()
            for key in receipt.artifact_refs
        }
        manifest_matches = (
            manifest.get("status") == "success"
            and manifest.get("version") == run_id
            and manifest.get("modules") == list(REPORT_MODULE_IDS)
            and manifest.get("artifact_refs") == expected_refs
        )
        if receipt.storage_version == 2:
            manifest_matches = manifest_matches and (
                manifest.get("manifest_version") == 2
                and manifest.get("artifacts") == actual_hashes
            )
        else:
            manifest_matches = manifest_matches and (
                manifest.get("manifest_version") == 3
                and manifest.get("storage") == "mixed"
                and manifest.get("artifact_sha256") == actual_hashes
                and manifest.get("artifacts") == actual_hashes
                and manifest.get("artifact_storage")
                == {
                    key: value
                    for key, value in receipt.artifact_storage.items()
                    if key != "manifest"
                }
            )
        if not manifest_matches:
            raise StorageCompactionError(
                "delivery manifest does not match the completed run"
            )

        sha256 = receipt.artifact_sha256["final_docx"]
        blob_ref = receipt.artifact_refs["final_docx"]
        blob = resolved_blobs["final_docx"]

        try:
            version = self.version_store.load(version_id)
        except (FileNotFoundError, ValueError) as exc:
            raise StorageCompactionError(
                "matching report version failed validation"
            ) from exc
        version_ref = Path(f"Work/report-versions/{version_id}/version.json")
        version_final_is_cas = (
            version.storage_version == 2
            or (
                version.storage_version == 3
                and version.artifact_storage.get("final_docx") == "cas"
            )
        )
        if (
            version.run_id != run_id
            or version.storage_version not in {2, 3}
            or not version_final_is_cas
            or version.artifact_sha256.get("final_docx") != sha256
            or version.artifact_blob_refs.get("final_docx") != blob_ref
            or version.artifact_sha256.get("delivery_receipt")
            != self._sha256(receipt_path)
        ):
            raise StorageCompactionError(
                "report version is not bound to the current receipt and DOCX"
            )

        target = self._workspace_path(self.FINAL_DOCX_REF)
        if not target.is_file() or self._sha256(target) != sha256:
            raise StorageCompactionError(
                "visible final DOCX does not match the completed run receipt"
            )
        if target.is_symlink():
            if target.resolve() != blob:
                raise StorageCompactionError(
                    "visible final DOCX is an uncontrolled symbolic link"
                )
            state: StorageOutputState = "cas_view"
        else:
            state = "materialized"
        self._validate_docx(target)

        run_result_ref = self._validate_run_result(
            run_id,
            required_paths={target, manifest_path},
        )

        owner_verified = self._verify_owner(
            run_id=run_id,
            version_id=version_id,
            sha256=sha256,
            receipt_ref=receipt_ref,
            require_owner=require_owner,
        )
        target_stat = target.lstat()
        logical_size = blob.stat().st_size
        allocated_size = int(getattr(target_stat, "st_blocks", 0) or 0) * 512
        return _ValidatedOutput(
            run_id=run_id,
            completion_kind=completion_kind,
            target=target,
            target_ref=self.FINAL_DOCX_REF,
            blob_ref=blob_ref,
            blob=blob,
            sha256=sha256,
            logical_size=logical_size,
            allocated_size=allocated_size,
            target_inode=int(target_stat.st_ino),
            target_link_count=int(target_stat.st_nlink),
            state=state,
            receipt_ref=receipt_ref,
            version_ref=version_ref,
            run_result_ref=run_result_ref,
            owner_verified=owner_verified,
        )

    def _verify_owner(
        self,
        *,
        run_id: str,
        version_id: str,
        sha256: str,
        receipt_ref: Path,
        require_owner: bool,
    ) -> bool:
        try:
            owner = self.owner_store.load()
        except (OSError, ValueError) as exc:
            raise StorageCompactionError(
                "shared output-owner pointer is invalid"
            ) from exc
        if owner is None:
            if require_owner:
                raise StorageCompactionError(
                    "mutating storage compaction requires Work/output-owner.json"
                )
            return False
        try:
            self.owner_store.require(
                expected_run_id=run_id,
                expected_report_version_id=version_id,
                expected_final_docx_sha256=sha256,
                expected_delivery_receipt_ref=receipt_ref,
            )
        except (OSError, ValueError) as exc:
            raise StorageCompactionError(
                "shared output-owner pointer does not bind the requested run"
            ) from exc
        return True

    def _record(
        self,
        output: _ValidatedOutput,
        *,
        action: StorageCompactionAction,
        status: StorageCompactionStatus,
        operation_id: str,
        pre_state: StorageOutputState | None = None,
        post_state: StorageOutputState | None | Literal[False] = False,
        metrics: _ValidatedOutput | None = None,
        error: str | None = None,
    ) -> StorageCompactionRecord:
        basis = metrics or output
        before = pre_state or basis.state
        if post_state is False:
            after: StorageOutputState | None = output.state
        else:
            after = post_state
        potential_apply = action in {"dry_run", "apply"} and before == "materialized"
        reclaimed_logical = basis.logical_size if potential_apply else 0
        reclaimed_allocated = (
            basis.allocated_size
            if potential_apply and basis.target_link_count == 1
            else 0
        )
        materialized_logical = (
            output.logical_size
            if action == "rollback"
            and before == "cas_view"
            and status == "completed"
            and after == "materialized"
            else 0
        )
        materialized_allocated = (
            output.allocated_size if materialized_logical else 0
        )
        owner_verified = output.owner_verified
        validation = [
            "workflow completed delivery",
            (
                "modules 2.1-2.5 completed"
                if output.completion_kind == "initial"
                else "authorized revision modules completed"
            ),
            "Cross and Final review completed",
            "top-level run result completed with current output paths",
            "delivery receipt and manifest hashes matched",
            "report version bound to receipt and CAS blob",
            "visible DOCX hash and ZIP structure verified",
        ]
        if owner_verified:
            validation.append("shared output-owner pointer verified")
        else:
            validation.append(
                "legacy workspace has no output-owner pointer; mutation disabled"
            )
        return StorageCompactionRecord(
            operation_id=operation_id,
            run_id=output.run_id,
            completion_kind=output.completion_kind,
            action=action,
            status=status,
            dry_run=action == "dry_run",
            pre_state=before,
            post_state=after,
            target_ref=output.target_ref,
            blob_ref=output.blob_ref,
            sha256=output.sha256,
            logical_size_bytes=basis.logical_size,
            allocated_size_bytes=basis.allocated_size,
            target_inode=basis.target_inode,
            target_link_count=basis.target_link_count,
            estimated_reclaimed_logical_bytes=reclaimed_logical,
            estimated_reclaimed_allocated_bytes=reclaimed_allocated,
            materialized_logical_bytes=materialized_logical,
            materialized_allocated_bytes=materialized_allocated,
            receipt_ref=output.receipt_ref,
            version_ref=output.version_ref,
            run_result_ref=output.run_result_ref,
            owner_verified=owner_verified,
            mutation_allowed=owner_verified,
            validation=validation,
            error=error,
        )

    def _write_record(self, record: StorageCompactionRecord) -> Path:
        path = self.store.write_json(
            f"Work/runs/{record.run_id}/{self.MANIFEST_NAME}",
            record.model_dump(mode="json"),
        )
        self.store.fsync_directory(path.parent)
        return path

    def _completion_kind(
        self,
        workflow: dict,
        *,
        run_id: str,
    ) -> StorageCompletionKind:
        if (
            workflow.get("run_id") != run_id
            or workflow.get("status") != "completed"
            or workflow.get("cross_review_completed") is not True
            or workflow.get("final_review_completed") is not True
        ):
            raise StorageCompactionError(
                "storage compaction requires completed Cross and Final review"
            )
        activity = workflow.get("activity")
        if activity == "delivery":
            completed = workflow.get("completed_modules")
            if (
                not isinstance(completed, list)
                or not all(isinstance(item, str) for item in completed)
                or set(completed) != set(REPORT_MODULE_IDS)
            ):
                raise StorageCompactionError(
                    "initial delivery does not contain all five completed modules"
                )
            return "initial"
        if activity == "revision-delivery":
            request = self._read_json(
                self.workspace / f"Work/runs/{run_id}/revision-request.json"
            )
            targets = request.get("target_module_ids")
            completed = workflow.get("completed_revision_modules")
            if (
                not isinstance(targets, list)
                or not targets
                or not all(isinstance(item, str) for item in targets)
                or not set(targets).issubset(REPORT_MODULE_IDS)
                or not isinstance(completed, list)
                or not all(isinstance(item, str) for item in completed)
                or set(completed) != set(targets)
            ):
                raise StorageCompactionError(
                    "revision delivery does not match its authorized modules"
                )
            return "revision"
        raise StorageCompactionError(
            "storage compaction requires an initial or revision delivery checkpoint"
        )

    def _validate_run_result(
        self,
        run_id: str,
        *,
        required_paths: set[Path],
    ) -> Path:
        result_ref = Path(f"Work/runs/{run_id}.json")
        payload = self._read_json(self.workspace / result_ref)
        raw_paths = payload.get("output_paths")
        if (
            payload.get("run_id") != run_id
            or payload.get("status") != "completed"
            or payload.get("error") not in {None, ""}
            or not isinstance(raw_paths, list)
            or not raw_paths
            or not all(isinstance(item, str) and item for item in raw_paths)
        ):
            raise StorageCompactionError(
                "top-level report run result is not a completed delivery"
            )
        output_paths: set[Path] = set()
        for raw in raw_paths:
            path = self._workspace_path(Path(raw))
            if not path.is_file() or path.stat().st_size == 0:
                raise StorageCompactionError(
                    "top-level report run result contains a missing output"
                )
            output_paths.add(path)
        if not required_paths.issubset(output_paths):
            raise StorageCompactionError(
                "top-level report run result is not bound to the current receipt"
            )
        return result_ref

    def _receipt_artifacts(self, receipt: DeliveryReceipt) -> dict[str, Path]:
        return {
            "final_docx": receipt.final_docx,
            "report_state": receipt.report_state,
            "source_index": receipt.source_index,
            "source_index_docx": receipt.source_index_docx,
            **{
                f"module:{module_id}": receipt.module_files[module_id]
                for module_id in REPORT_MODULE_IDS
                if module_id in receipt.module_files
            },
        }

    def _workspace_path(self, raw: Path) -> Path:
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        absolute = Path(os.path.abspath(candidate))
        if not absolute.is_relative_to(self.workspace):
            raise StorageCompactionError(
                "storage compaction path escapes the project workspace"
            )
        current = self.workspace
        for part in absolute.parent.relative_to(self.workspace).parts:
            current /= part
            if current.is_symlink():
                raise StorageCompactionError(
                    "storage compaction path uses a symbolic-link ancestor"
                )
        if (
            not absolute.parent.resolve().is_relative_to(self.workspace)
            or not absolute.resolve().is_relative_to(self.workspace)
        ):
            raise StorageCompactionError(
                "storage compaction path escapes the project workspace"
            )
        return absolute

    @staticmethod
    def _safe_run_id(run_id: str) -> str:
        if (
            run_id in {"", ".", ".."}
            or not re.fullmatch(r"[A-Za-z0-9._-]+", run_id or "")
        ):
            raise ValueError("run_id must be a safe, real path segment")
        return run_id

    def _validated_run_root(self, run_id: str) -> Path:
        safe_run_id = self._safe_run_id(run_id)
        work_root = self.workspace / "Work"
        runs_root = work_root / "runs"
        if work_root.is_symlink() or runs_root.is_symlink():
            raise StorageCompactionError(
                "Work/runs must not use a symbolic-link ancestor"
            )
        resolved_runs = runs_root.resolve()
        if not resolved_runs.is_relative_to(self.workspace):
            raise StorageCompactionError("Work/runs escapes the project workspace")
        run_root = runs_root / safe_run_id
        if run_root.is_symlink() or not run_root.is_dir():
            raise StorageCompactionError(
                f"completed report run does not exist: {safe_run_id}"
            )
        resolved_run = run_root.resolve()
        if not resolved_run.is_relative_to(resolved_runs):
            raise StorageCompactionError(
                "report run path escapes Work/runs"
            )
        return run_root

    @contextmanager
    def _exclusive_run_lock(self, run_id: str) -> Iterator[None]:
        run_root = self._validated_run_root(run_id)
        lock_path = run_root / ".active.lock"
        if lock_path.is_symlink():
            raise StorageCompactionError(
                "report run lock must not be a symbolic link"
            )
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lock_path, flags, 0o600)
        try:
            opened_stat = os.fstat(descriptor)
            try:
                path_stat = os.lstat(lock_path)
            except OSError as exc:
                raise StorageCompactionError(
                    "report run lock identity cannot be verified"
                ) from exc
            if (
                not stat.S_ISREG(opened_stat.st_mode)
                or not stat.S_ISREG(path_stat.st_mode)
                or opened_stat.st_nlink != 1
                or path_stat.st_nlink != 1
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (path_stat.st_dev, path_stat.st_ino)
            ):
                raise StorageCompactionError(
                    "report run lock is not one verified regular file"
                )
            os.fchmod(descriptor, 0o600)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise StorageCompactionError(
                    f"report run is active and cannot be compacted: {run_id}"
                ) from exc
            locked_stat = os.lstat(lock_path)
            if (
                not stat.S_ISREG(locked_stat.st_mode)
                or locked_stat.st_nlink != 1
                or (opened_stat.st_dev, opened_stat.st_ino)
                != (locked_stat.st_dev, locked_stat.st_ino)
            ):
                raise StorageCompactionError(
                    "report run lock identity changed while acquiring the lock"
                )
            yield
        finally:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                os.close(descriptor)

    def _detect_state(self, output: _ValidatedOutput) -> StorageOutputState | None:
        target = output.target
        try:
            if target.is_symlink():
                return "cas_view" if target.resolve() == output.blob else None
            if target.is_file() and self._sha256(target) == output.sha256:
                return "materialized"
        except OSError:
            return None
        return None

    @staticmethod
    def _error_text(
        original_error: Exception,
        *,
        recovery_error: Exception | None = None,
    ) -> str:
        text = f"{type(original_error).__name__}: {original_error}"
        if recovery_error is not None:
            text += (
                "; recovery="
                f"{type(recovery_error).__name__}: {recovery_error}"
            )
        return text[:4_000]

    @staticmethod
    def _read_json(path: Path) -> dict:
        if path.is_symlink() or not path.is_file():
            raise StorageCompactionError(f"required JSON file is missing: {path}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise StorageCompactionError(
                f"required JSON file is invalid: {path}"
            ) from exc
        if not isinstance(payload, dict):
            raise StorageCompactionError(
                f"required JSON payload is not an object: {path}"
            )
        return payload

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _validate_docx(path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as archive:
                corrupt_member = archive.testzip()
            if corrupt_member is not None:
                raise StorageCompactionError(
                    f"final DOCX contains a corrupt member: {corrupt_member}"
                )
            Document(path)
        except (
            KeyError,
            OSError,
            PackageNotFoundError,
            ValueError,
            zipfile.BadZipFile,
        ) as exc:
            raise StorageCompactionError(
                "final DOCX is not a readable Office document"
            ) from exc
