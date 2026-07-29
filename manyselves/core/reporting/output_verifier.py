"""Shared completion verifier for initial, resumed, and revision runs."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from docx import Document


class OutputVerificationError(ValueError):
    pass


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


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
        path = Path(raw)
        target = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not target.is_relative_to(root):
            raise OutputVerificationError(f"output is outside workspace: {raw}")
        if not target.is_file() or target.stat().st_size == 0:
            raise OutputVerificationError(f"output is missing or empty: {raw}")
        inherited_same_run_artifact = (
            allow_existing_run_artifacts and target.is_relative_to(run_root)
        )
        if (
            target.stat().st_mtime_ns < started_ns
            and not inherited_same_run_artifact
            and not allow_existing_artifacts
        ):
            raise OutputVerificationError(f"output is stale: {raw}")
        if target.suffix.casefold() == ".docx":
            try:
                Document(target)
            except Exception as exc:
                raise OutputVerificationError(f"output DOCX is invalid: {raw}") from exc
        paths.append(target)
    if not paths:
        raise OutputVerificationError("workflow finished without verified output artifacts")

    receipt_path = root / "Work" / "runs" / run_id / "delivery-receipt.json"
    if receipt_path.exists():
        try:
            receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
            manifest = Path(receipt["manifest_path"])
            manifest = manifest if manifest.is_absolute() else root / manifest
            manifest_data = json.loads(manifest.read_text(encoding="utf-8"))
        except Exception as exc:
            raise OutputVerificationError("delivery receipt is invalid") from exc
        if not receipt.get("success") or manifest_data.get("version") != run_id:
            raise OutputVerificationError("delivery receipt does not match the current run")
        delivered = Path(receipt["final_docx"])
        delivered = delivered.resolve() if delivered.is_absolute() else (root / delivered).resolve()
        if not delivered.is_file() or not delivered.is_relative_to(root):
            raise OutputVerificationError("delivery receipt points to an invalid artifact")
        source_index = Path(receipt["source_index"])
        source_index = (
            source_index.resolve()
            if source_index.is_absolute()
            else (root / source_index).resolve()
        )
        if (
            not source_index.is_file()
            or not source_index.is_relative_to(root)
            or "source_index" not in manifest_data.get("artifacts", {})
        ):
            raise OutputVerificationError(
                "delivery receipt is missing the separate source-index artifact"
            )
        source_index_docx = Path(receipt["source_index_docx"])
        source_index_docx = (
            source_index_docx.resolve()
            if source_index_docx.is_absolute()
            else (root / source_index_docx).resolve()
        )
        if (
            not source_index_docx.is_file()
            or not source_index_docx.is_relative_to(root)
            or "source_index_docx" not in manifest_data.get("artifacts", {})
        ):
            raise OutputVerificationError(
                "delivery receipt is missing the source-index DOCX companion"
            )
        try:
            Document(source_index_docx)
        except Exception as exc:
            raise OutputVerificationError("source-index DOCX companion is invalid") from exc
        artifact_hashes = manifest_data.get("artifacts", {})
        delivered_artifacts = {
            "final_docx": delivered,
            "source_index": source_index,
            "source_index_docx": source_index_docx,
        }
        mismatched = [
            name
            for name, path in delivered_artifacts.items()
            if artifact_hashes.get(name) != _sha256(path)
        ]
        if mismatched:
            raise OutputVerificationError(
                "delivery manifest hash does not match delivered artifact: "
                f"{mismatched}"
            )
    return paths
