"""Capability-owned project content snapshot composition.

This is the mechanically extracted implementation formerly held by
``ReportingService.snapshot_content``.  The Service remains a compatibility
caller; the existing ContentAddressedStore, trusted handle, and project write
lease semantics stay unchanged.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    validate_bound_project_write_lease,
)
from manyselves.runtime.artifacts.content_store import ContentAddressedStore


def snapshot_content(
    workspace: Path,
    content_store: ContentAddressedStore,
    source: Path,
    target: Path,
    *,
    replace_existing_with_view: bool = False,
) -> tuple[Path, str, Path]:
    """Ingest bytes once and expose an immutable project-local compatibility view."""

    workspace = Path(workspace).resolve()
    source = Path(source)
    target = Path(target)
    validate_bound_project_write_lease(workspace)
    blob = content_store.ingest_file(source)
    trusted = content_store.issue_trusted_handle(
        blob,
        lineage_id=(
            "snapshot:"
            + (
                target.relative_to(workspace).as_posix()
                if target.resolve().is_relative_to(workspace)
                else target.as_posix()
            )
        ),
    )
    if target.exists() or target.is_symlink():
        if not target.is_file():
            raise ValueError(f"content snapshot target is not a file: {target}")
        target_sha256 = hashlib.sha256(target.read_bytes()).hexdigest()
        if target_sha256 != blob.sha256:
            raise ValueError(
                "immutable content snapshot already exists with different bytes: "
                f"{target}"
            )
        if replace_existing_with_view and target.resolve() != blob.path:
            staged = target.with_name(
                f".{target.name}.{uuid.uuid4().hex}.cas-view"
            )
            try:
                content_store.link_trusted_view(
                    trusted,
                    staged,
                    final_path=target,
                )
                validate_bound_project_write_lease(workspace)
                os.replace(staged, target)
            finally:
                staged.unlink(missing_ok=True)
    else:
        validate_bound_project_write_lease(workspace)
        content_store.link_trusted_view(trusted, target)
    return target, blob.sha256, blob.relative_path


__all__ = ["snapshot_content"]
