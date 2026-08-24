"""Immutable run input inventory for queued/headless execution."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import Field

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import StrictModel
from manyselves.capabilities.distribution_reporting.runtime.state.parallel import (
    exclusive_file_lock,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


class FrozenProjectFile(StrictModel):
    logical_ref: Path
    snapshot_ref: Path
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size: int = Field(ge=0)
    blob_ref: Path
    trusted_handle_ref: Path


class RunInputSnapshot(StrictModel):
    schema_version: Literal["1"] = "1"
    run_id: str
    inventory_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[FrozenProjectFile] = Field(default_factory=list)

    def scope_root(self, workspace: Path, scope: str) -> Path:
        return (
            Path(workspace)
            / "Work"
            / "runs"
            / self.run_id
            / "frozen-project"
            / scope
        )

    def resolve(self, logical_ref: Path) -> Path:
        logical_ref = Path(logical_ref)
        matches = [
            item.snapshot_ref
            for item in self.files
            if item.logical_ref == logical_ref
        ]
        if len(matches) != 1:
            raise FileNotFoundError(
                f"request input is not present in the frozen inventory: {logical_ref}"
            )
        return matches[0]


class RunInputSnapshotStore:
    SCOPES = ("Inputs", "Knowledge", "Templates")

    def __init__(self, workspace: Path) -> None:
        # Import the generic content store only when a snapshot store is
        # constructed. Importing this Capability model must not initialize the
        # historical Reporting package through ``manyselves.core`` side effects.
        from manyselves.runtime.artifacts.content_store import ContentAddressedStore

        self.workspace = Path(workspace).resolve()
        self.content_store = ContentAddressedStore(self.workspace)
        self.store = ReportingStore(self.workspace)
        self.lock_path = self.workspace / "Work/leases/run-input-snapshot.lock"

    def _manifest_path(self, run_id: str) -> Path:
        if not run_id or Path(run_id).name != run_id:
            raise ValueError("run_id must be one safe path component")
        return self.workspace / f"Work/runs/{run_id}/input-snapshot.json"

    @staticmethod
    def _inventory_digest(files: list[FrozenProjectFile]) -> str:
        payload = [
            {
                "logical_ref": item.logical_ref.as_posix(),
                "sha256": item.sha256,
                "size": item.size,
            }
            for item in files
        ]
        return hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()

    def freeze(
        self,
        run_id: str,
        *,
        extra_refs: list[Path] | tuple[Path, ...] = (),
    ) -> RunInputSnapshot:
        manifest_path = self._manifest_path(run_id)
        with exclusive_file_lock(self.lock_path):
            if manifest_path.is_file():
                return self.load(run_id)
            scoped_candidates = [
                path
                for scope in self.SCOPES
                for path in (self.workspace / scope).rglob("*")
                if path.is_file()
            ]
            by_logical_ref: dict[Path, Path] = {
                path.relative_to(self.workspace): path
                for path in scoped_candidates
            }
            for raw_ref in extra_refs:
                logical_ref = Path(raw_ref)
                if logical_ref.is_absolute() or ".." in logical_ref.parts:
                    raise ValueError(
                        f"request input ref is not project-relative: {logical_ref}"
                    )
                source = (self.workspace / logical_ref).resolve()
                if not source.is_relative_to(self.workspace) or not source.is_file():
                    raise FileNotFoundError(
                        f"request input does not exist: {logical_ref.as_posix()}"
                    )
                by_logical_ref[logical_ref] = source
            candidates = [
                (logical_ref, by_logical_ref[logical_ref])
                for logical_ref in sorted(
                    by_logical_ref,
                    key=lambda item: item.as_posix(),
                )
            ]
            frozen: list[FrozenProjectFile] = []
            source_stats: dict[Path, tuple[int, int, int, int]] = {}
            for logical_ref, source in candidates:
                resolved = source.resolve()
                if not resolved.is_relative_to(self.workspace):
                    raise ValueError("run input source escapes project workspace")
                stat_before = source.stat()
                blob = self.content_store.ingest_file(source)
                stat_after = source.stat()
                before = (
                    stat_before.st_dev,
                    stat_before.st_ino,
                    stat_before.st_size,
                    stat_before.st_mtime_ns,
                )
                after = (
                    stat_after.st_dev,
                    stat_after.st_ino,
                    stat_after.st_size,
                    stat_after.st_mtime_ns,
                )
                if before != after:
                    raise RuntimeError(
                        f"run input changed while freezing: {logical_ref.as_posix()}"
                    )
                source_stats[source] = after
                handle = self.content_store.issue_trusted_handle(
                    blob,
                    lineage_id=f"run-input:{run_id}:{logical_ref.as_posix()}",
                )
                snapshot_ref = (
                    Path("Work/runs")
                    / run_id
                    / "frozen-project"
                    / logical_ref
                )
                target = self.workspace / snapshot_ref
                if target.exists() or target.is_symlink():
                    if target.resolve() != blob.path:
                        raise RuntimeError(
                            "partial run input snapshot has conflicting bytes: "
                            f"{snapshot_ref.as_posix()}"
                        )
                else:
                    self.content_store.link_trusted_view(handle, target)
                frozen.append(
                    FrozenProjectFile(
                        logical_ref=logical_ref,
                        snapshot_ref=snapshot_ref,
                        sha256=blob.sha256,
                        size=blob.size,
                        blob_ref=blob.relative_path,
                        trusted_handle_ref=handle.manifest_ref,
                    )
                )
            for source, expected in source_stats.items():
                current = source.stat()
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_size,
                    current.st_mtime_ns,
                ) != expected:
                    raise RuntimeError(
                        "run input inventory changed before snapshot commit"
                    )
            snapshot = RunInputSnapshot(
                run_id=run_id,
                inventory_digest=self._inventory_digest(frozen),
                files=frozen,
            )
            self.store.write_json(
                manifest_path.relative_to(self.workspace).as_posix(),
                snapshot.model_dump(mode="json"),
            )
            return snapshot

    def load(self, run_id: str) -> RunInputSnapshot:
        path = self._manifest_path(run_id)
        if not path.is_file():
            raise FileNotFoundError(f"run input snapshot is missing: {run_id}")
        snapshot = RunInputSnapshot.model_validate_json(
            path.read_text(encoding="utf-8")
        )
        if snapshot.inventory_digest != self._inventory_digest(snapshot.files):
            raise ValueError("run input snapshot inventory digest mismatch")
        for item in snapshot.files:
            handle = self.content_store.load_trusted_handle(item.trusted_handle_ref)
            blob = self.content_store.resolve_trusted_handle(
                handle,
                expected_sha256=item.sha256,
                expected_size=item.size,
            )
            view = (self.workspace / item.snapshot_ref).resolve()
            if view != blob:
                raise ValueError(
                    f"run input snapshot view mismatch: {item.snapshot_ref}"
                )
        return snapshot
