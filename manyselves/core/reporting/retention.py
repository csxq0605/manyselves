"""Dry-run storage accounting and retention planning for reporting artifacts."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .store import ReportingStore


class ReportingRetentionPlanner:
    """Mark referenced CAS blobs and report candidates without deleting anything."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.store = ReportingStore(self.workspace)
        self.content_root = self.workspace / "Work/content/sha256"

    def generate(self, *, grace_days: int = 7) -> dict[str, Any]:
        if grace_days < 0:
            raise ValueError("grace_days must be non-negative")
        blobs = {
            path.relative_to(self.workspace).as_posix(): path
            for path in self.content_root.glob("*/*/*")
            if path.is_file() and not path.is_symlink()
        }
        references: dict[str, set[str]] = {ref: set() for ref in blobs}
        view_logical_bytes = 0
        view_count = 0

        work_root = self.workspace / "Work"
        if work_root.is_dir():
            for path in work_root.rglob("*"):
                if not path.is_symlink():
                    continue
                try:
                    resolved = path.resolve(strict=True)
                except (FileNotFoundError, OSError):
                    continue
                if not resolved.is_relative_to(self.content_root):
                    continue
                ref = resolved.relative_to(self.workspace).as_posix()
                if ref in references:
                    references[ref].add(path.relative_to(self.workspace).as_posix())
                    view_logical_bytes += resolved.stat().st_size
                    view_count += 1

        for manifest_path in self._manifest_paths():
            try:
                payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            for ref in self._content_refs(payload):
                if ref in references:
                    references[ref].add(
                        manifest_path.relative_to(self.workspace).as_posix()
                    )

        canonical_bytes = sum(path.stat().st_size for path in blobs.values())
        allocated_bytes = sum(
            int(getattr(path.stat(), "st_blocks", 0) or 0) * 512
            for path in blobs.values()
        )
        referenced = {
            ref: path for ref, path in blobs.items() if references.get(ref)
        }
        unreferenced = {
            ref: path for ref, path in blobs.items() if not references.get(ref)
        }
        cutoff = datetime.now(timezone.utc) - timedelta(days=grace_days)
        candidates = []
        for ref, path in sorted(unreferenced.items()):
            modified = datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
            candidates.append(
                {
                    "blob_ref": ref,
                    "bytes": path.stat().st_size,
                    "modified_at": modified.isoformat(),
                    "eligible_after_grace": modified <= cutoff,
                    "reason": "no current manifest or compatibility view references this blob",
                    "action": "quarantine_then_delete_after_explicit_execution",
                }
            )

        usage = {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "content_store": "Work/content/sha256",
            "blob_count": len(blobs),
            "referenced_blob_count": len(referenced),
            "unreferenced_blob_count": len(unreferenced),
            "canonical_bytes": canonical_bytes,
            "allocated_bytes": allocated_bytes,
            "compatibility_view_count": view_count,
            "compatibility_view_logical_bytes": view_logical_bytes,
            "reused_bytes": view_logical_bytes,
            "referenced_blob_bytes": sum(
                path.stat().st_size for path in referenced.values()
            ),
            "unreferenced_blob_bytes": sum(
                path.stat().st_size for path in unreferenced.values()
            ),
        }
        plan = {
            "generated_at": usage["generated_at"],
            "mode": "dry_run",
            "automatic_deletion": False,
            "grace_days": grace_days,
            "mark_sources": [
                "report-version manifests",
                "delivery manifests",
                "conversation manifests",
                "template provenance",
                "content-backed compatibility views",
            ],
            "candidate_count": len(candidates),
            "candidate_bytes": sum(item["bytes"] for item in candidates),
            "candidates": candidates,
            "execution_requirements": [
                "re-scan references immediately before mutation",
                "exclude active or pinned runs",
                "move candidates to quarantine before deletion",
                "require an explicit cleanup action",
            ],
        }
        self.store.write_json("Work/storage-usage.json", usage)
        self.store.write_json("Work/retention-plan.json", plan)
        return {"usage": usage, "plan": plan}

    def _manifest_paths(self) -> list[Path]:
        patterns = (
            "Work/report-versions/*/version.json",
            "Work/runs/*/delivery/*/delivery-manifest.json",
            "Work/runs/*/agent-conversations/*.json",
            "Work/runs/*/template-provenance.json",
            "Work/report-template-writing/source.json",
        )
        return [
            path
            for pattern in patterns
            for path in self.workspace.glob(pattern)
            if path.is_file()
        ]

    def _content_refs(self, payload: Any) -> set[str]:
        found: set[str] = set()

        def visit(value: Any, key: str | None = None) -> None:
            if isinstance(value, dict):
                for child_key, child in value.items():
                    visit(child, str(child_key))
                return
            if isinstance(value, list):
                for child in value:
                    visit(child, key)
                return
            if not isinstance(value, str):
                return
            # CAS references are self-identifying by their workspace-relative
            # prefix.  Do not depend on the immediate JSON key: nested maps
            # such as ``artifact_blob_refs: {final_docx: ...}`` otherwise lose
            # the semantic parent key while walking their leaf values.
            if value.startswith("Work/content/sha256/"):
                found.add(Path(value).as_posix())

        visit(payload)
        return found
