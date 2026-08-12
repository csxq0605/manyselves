"""Shared completion verifier for initial, resumed, and revision runs.

The verifier deliberately sits on the read side of delivery.  A delivery
receipt is the authority for the immutable package views; report-version and
archive code may retain CAS references, but an opaque reference is never a
valid output path here.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from docx import Document

from .delivery import DeliveryReceipt
from .models import REPORT_MODULE_IDS


class OutputVerificationError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _reject_opaque(raw: Any, label: str) -> None:
    """Reject CAS/URI handles where a materialized project view is required."""

    if isinstance(raw, Path):
        value = raw.as_posix()
    elif isinstance(raw, str):
        value = raw
    else:
        raise OutputVerificationError(f"{label} is not a readable project path")
    lowered = value.casefold()
    if lowered.startswith(("artifact:", "cas:", "opaque:", "handle:")) or "://" in value:
        raise OutputVerificationError(f"{label} uses an opaque artifact reference")


def _lexical_path(root: Path, raw: Any, label: str) -> Path:
    """Resolve a receipt path lexically while retaining symlink view identity."""

    _reject_opaque(raw, label)
    value = Path(raw)
    # ``resolve`` is intentionally used only for workspace containment.  The
    # lexical path must remain under the delivery package even when it is a
    # trusted CAS compatibility symlink whose target lives under Work/content.
    lexical = value if value.is_absolute() else root / value
    lexical = Path(os.path.abspath(os.fspath(lexical)))
    resolved = lexical.resolve()
    if not resolved.is_relative_to(root):
        raise OutputVerificationError(f"{label} is outside workspace")
    return lexical


def _require_file(path: Path, label: str) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise OutputVerificationError(f"{label} is missing or empty")


def _verify_docx(path: Path, label: str) -> None:
    try:
        Document(path)
    except Exception as exc:  # python-docx raises a few different exceptions
        raise OutputVerificationError(f"{label} DOCX is invalid") from exc


def _verify_typed_receipt(
    root: Path,
    run_id: str,
    receipt_path: Path,
    raw: dict[str, Any],
) -> None:
    """Validate a v3 (or fully shaped legacy) typed receipt and its package."""

    try:
        receipt = DeliveryReceipt.model_validate(raw)
    except Exception as exc:
        raise OutputVerificationError("delivery receipt is invalid") from exc
    if not receipt.success:
        raise OutputVerificationError("delivery receipt is not successful")

    expected_delivery_root = (root / "Work" / "runs" / run_id / "delivery").resolve()
    delivery_dir = _lexical_path(root, receipt.delivery_dir, "delivery directory")
    delivery_resolved = delivery_dir.resolve()
    if not delivery_resolved.is_relative_to(expected_delivery_root):
        raise OutputVerificationError("delivery receipt does not match the current run")
    if not delivery_dir.is_dir():
        raise OutputVerificationError("delivery receipt directory is missing")

    # All public package views are required to be lexical children of the
    # package.  This permits a trusted CAS symlink while rejecting path
    # traversal and opaque canonical references.
    def package_view(raw_path: Any, label: str) -> Path:
        path = _lexical_path(root, raw_path, label)
        if not path.is_relative_to(delivery_dir):
            raise OutputVerificationError(f"{label} is outside its delivery package")
        _require_file(path, label)
        return path

    manifest_path = package_view(receipt.manifest_path, "delivery manifest")
    final_docx = package_view(receipt.final_docx, "delivered final DOCX")
    report_state = package_view(receipt.report_state, "delivered report state")
    source_index = package_view(receipt.source_index, "delivered source index")
    source_index_docx = package_view(
        receipt.source_index_docx, "delivered source-index DOCX"
    )
    if source_index != delivery_dir / "证据与来源索引.md":
        raise OutputVerificationError("delivery source index is not package-bound")
    if source_index_docx != delivery_dir / "证据与来源索引.docx":
        raise OutputVerificationError("delivery source-index DOCX is not package-bound")
    _verify_docx(final_docx, "delivered final")
    _verify_docx(source_index_docx, "source-index")

    if set(receipt.module_files) != set(REPORT_MODULE_IDS):
        raise OutputVerificationError("delivery receipt is missing a complete module set")
    module_paths = {
        f"module:{module_id}": package_view(path, f"module {module_id}")
        for module_id, path in receipt.module_files.items()
    }
    public_paths = {
        "final_docx": final_docx,
        "report_state": report_state,
        "source_index": source_index,
        "source_index_docx": source_index_docx,
        **module_paths,
    }

    try:
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise OutputVerificationError("delivery manifest is invalid") from exc
    if (
        str(manifest_data.get("version")) != run_id
        or manifest_data.get("status") != "success"
    ):
        raise OutputVerificationError("delivery receipt does not match the current run")
    manifest_modules = manifest_data.get("modules")
    if manifest_modules is not None and set(manifest_modules) != set(REPORT_MODULE_IDS):
        raise OutputVerificationError("delivery manifest module set is incomplete")

    manifest_hashes = manifest_data.get("artifact_sha256") or manifest_data.get("artifacts")
    if not isinstance(manifest_hashes, dict):
        raise OutputVerificationError("delivery manifest has no artifact hashes")
    expected_hashes = dict(receipt.artifact_sha256)
    for key, path in public_paths.items():
        expected = expected_hashes.get(key)
        manifest_expected = manifest_hashes.get(key)
        if expected is None or manifest_expected is None:
            raise OutputVerificationError(f"delivery receipt is missing artifact hash: {key}")
        if expected != manifest_expected or expected != _sha256(path):
            raise OutputVerificationError(
                "delivery manifest hash does not match delivered artifact: "
                f"{key}"
            )
    manifest_digest = expected_hashes.get("manifest")
    if manifest_digest is not None and manifest_digest != _sha256(manifest_path):
        raise OutputVerificationError("delivery manifest hash does not match manifest")

    # A receipt itself belongs under the current run.  This check catches a
    # copied receipt whose package happens to have a valid but unrelated hash.
    if not receipt_path.resolve().is_relative_to(root / "Work" / "runs" / run_id):
        raise OutputVerificationError("delivery receipt does not match the current run")


def _verify_legacy_receipt(root: Path, run_id: str, raw: dict[str, Any]) -> None:
    """Read old completion receipts without weakening the new typed contract."""

    try:
        manifest_path = _lexical_path(root, raw["manifest_path"], "delivery manifest")
        _require_file(manifest_path, "delivery manifest")
        manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    except Exception as exc:
        if isinstance(exc, OutputVerificationError):
            raise
        raise OutputVerificationError("delivery receipt is invalid") from exc
    if not raw.get("success") or str(manifest_data.get("version")) != run_id:
        raise OutputVerificationError("delivery receipt does not match the current run")
    artifacts = manifest_data.get("artifact_sha256") or manifest_data.get("artifacts")
    if not isinstance(artifacts, dict):
        raise OutputVerificationError("delivery manifest has no artifact hashes")
    delivered_artifacts: dict[str, Path] = {}
    for key in ("final_docx", "source_index", "source_index_docx"):
        if key not in raw:
            if key == "final_docx":
                raise OutputVerificationError("delivery receipt is missing final DOCX")
            raise OutputVerificationError("delivery receipt is missing the separate source-index artifact")
        path = _lexical_path(root, raw[key], key)
        _require_file(path, key)
        delivered_artifacts[key] = path
    _verify_docx(delivered_artifacts["final_docx"], "delivered final")
    _verify_docx(delivered_artifacts["source_index_docx"], "source-index")
    for key, path in delivered_artifacts.items():
        if artifacts.get(key) != _sha256(path):
            raise OutputVerificationError(
                "delivery manifest hash does not match delivered artifact: "
                f"{key}"
            )


def verify_current_run_outputs(
    workspace: Path,
    run_id: str,
    artifacts: list,
    started_ns: int,
    *,
    allow_existing_run_artifacts: bool = False,
    allow_existing_artifacts: bool = False,
) -> list[Path]:
    root = Path(workspace).resolve()
    run_root = (root / "Work" / "runs" / run_id).resolve()
    paths: list[Path] = []
    for artifact in artifacts:
        raw = getattr(artifact, "path", artifact)
        _reject_opaque(raw, "output")
        path = Path(raw)
        target = path if path.is_absolute() else root / path
        target = Path(os.path.abspath(os.fspath(target)))
        resolved = target.resolve()
        if not resolved.is_relative_to(root):
            raise OutputVerificationError(f"output is outside workspace: {raw}")
        if not target.is_file() or target.stat().st_size == 0:
            raise OutputVerificationError(f"output is missing or empty: {raw}")
        inherited_same_run_artifact = (
            allow_existing_run_artifacts and resolved.is_relative_to(run_root)
        )
        if (
            target.stat().st_mtime_ns < started_ns
            and not inherited_same_run_artifact
            and not allow_existing_artifacts
        ):
            raise OutputVerificationError(f"output is stale: {raw}")
        if target.suffix.casefold() == ".docx":
            _verify_docx(target, "output")
        paths.append(target)
    if not paths:
        raise OutputVerificationError("workflow finished without verified output artifacts")

    receipt_path = root / "Work" / "runs" / run_id / "delivery-receipt.json"
    if receipt_path.exists():
        try:
            raw_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OutputVerificationError("delivery receipt is invalid") from exc
        # Full receipts are typed; sparse v1/v2 compatibility receipts retain
        # their historical read-only checks.
        typed_shape = {
            "delivery_dir",
            "module_files",
            "artifact_sha256",
            "source_index",
            "source_index_docx",
        }.issubset(raw_receipt)
        if typed_shape:
            _verify_typed_receipt(root, run_id, receipt_path, raw_receipt)
        else:
            _verify_legacy_receipt(root, run_id, raw_receipt)
    return paths
