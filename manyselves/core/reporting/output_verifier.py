"""Shared completion verifier for initial, resumed, and revision runs."""

from __future__ import annotations

import json
from pathlib import Path

from docx import Document


class OutputVerificationError(ValueError):
    pass


def verify_current_run_outputs(
    workspace: Path,
    run_id: str,
    artifacts: list,
    started_ns: int,
) -> list[Path]:
    root = Path(workspace).resolve()
    paths: list[Path] = []
    for artifact in artifacts:
        raw = getattr(artifact, "path", artifact)
        path = Path(raw)
        target = path.resolve() if path.is_absolute() else (root / path).resolve()
        if not target.is_relative_to(root):
            raise OutputVerificationError(f"output is outside workspace: {raw}")
        if not target.is_file() or target.stat().st_size == 0:
            raise OutputVerificationError(f"output is missing or empty: {raw}")
        if target.stat().st_mtime_ns < started_ns:
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
    return paths
