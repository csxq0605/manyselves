"""Project-local content-addressed storage with compatibility file views."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal


@dataclass(frozen=True)
class ContentBlob:
    """One immutable SHA-256-addressed project blob."""

    sha256: str
    size: int
    path: Path
    relative_path: Path


@dataclass(frozen=True)
class ContentView:
    """A legacy-compatible file path backed by a canonical blob."""

    path: Path
    mode: Literal["symlink", "copy"]


@dataclass(frozen=True)
class TrustedBlobHandle:
    """Stat-bound proof issued immediately after a full trusted ingestion hash."""

    sha256: str
    size: int
    relative_path: Path
    manifest_ref: Path
    lineage_id: str
    device: int
    inode: int
    mtime_ns: int


class ContentAddressedStore:
    """Store each byte sequence once and expose suffix-preserving file views."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "Work" / "content" / "sha256"
        self._metrics = {
            "cas_ingest_source_bytes": 0,
            "cas_rehash_bytes": 0,
            "cas_hit_after_full_scan": 0,
            "cas_trusted_handle_hits": 0,
        }

    def ingest_file(self, source: Path) -> ContentBlob:
        """Atomically ingest *source*, reusing an existing verified blob."""

        source = Path(source)
        if not source.is_file():
            raise FileNotFoundError(f"content source is missing: {source}")
        staging_root = self.root.parent / ".staging"
        staging_root.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        target: Path | None = None
        created_target = False
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=".content-",
                suffix=".tmp",
                dir=staging_root,
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                digest_builder = hashlib.sha256()
                size = 0
                with source.open("rb") as source_handle:
                    for chunk in iter(
                        lambda: source_handle.read(1024 * 1024),
                        b"",
                    ):
                        handle.write(chunk)
                        digest_builder.update(chunk)
                        size += len(chunk)
                handle.flush()
                os.fsync(handle.fileno())
            digest = digest_builder.hexdigest()
            self._metrics["cas_ingest_source_bytes"] += size
            target = self.root / digest[:2] / digest[2:4] / digest
            if target.exists():
                self._verify_existing(target, digest, size)
                self._metrics["cas_hit_after_full_scan"] += 1
                return self._blob(digest, size, target)

            target.parent.mkdir(parents=True, exist_ok=True)
            os.chmod(temporary, 0o444)
            try:
                os.link(temporary, target)
                created_target = True
            except FileExistsError:
                self._verify_existing(target, digest, size)
            except OSError:
                if target.exists():
                    self._verify_existing(target, digest, size)
                else:
                    os.replace(temporary, target)
                    temporary = None
                    created_target = True
            try:
                if created_target:
                    # The canonical blob is the exact fsynced staging inode (or
                    # an atomic rename of it), whose bytes were hashed while
                    # copying. Re-reading it here would double all first-ingest
                    # I/O without crossing a new trust boundary.
                    stat = target.stat()
                    if (
                        target.is_symlink()
                        or not target.is_file()
                        or stat.st_size != size
                        or target.name != digest
                    ):
                        raise ValueError(
                            f"new content blob failed identity validation: {target}"
                        )
                else:
                    self._verify_existing(target, digest, size)
            except Exception:
                if created_target and target.exists():
                    target.unlink()
                raise
            return self._blob(digest, size, target)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def link_view(
        self,
        blob: ContentBlob,
        target: Path,
        *,
        final_path: Path | None = None,
    ) -> ContentView:
        """Create a compatibility view that survives an atomic staging rename.

        ``final_path`` is the view's path after its staging directory is moved.
        The relative symlink is calculated from that final location.
        """

        self.resolve_blob(blob.relative_path, expected_sha256=blob.sha256)
        return self._link_view_without_rehash(
            blob,
            target,
            final_path=final_path,
        )

    def _link_view_without_rehash(
        self,
        blob: ContentBlob,
        target: Path,
        *,
        final_path: Path | None = None,
    ) -> ContentView:
        target = Path(target)
        final_path = Path(final_path) if final_path is not None else target
        self._require_project_path(target, label="content view")
        self._require_project_path(final_path, label="final content view")
        if target.exists() or target.is_symlink():
            raise FileExistsError(f"content view already exists: {target}")
        target.parent.mkdir(parents=True, exist_ok=True)
        relative_target = os.path.relpath(blob.path, start=final_path.parent)
        try:
            target.symlink_to(relative_target)
            return ContentView(path=target, mode="symlink")
        except (NotImplementedError, OSError):
            shutil.copyfile(blob.path, target)
            return ContentView(path=target, mode="copy")

    def resolve_blob(
        self,
        relative: Path,
        *,
        expected_sha256: str | None = None,
    ) -> Path:
        """Resolve and verify one project-relative CAS reference."""

        relative = Path(relative)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("content blob reference must be project-relative")
        candidate = self.workspace / relative
        target = candidate.resolve()
        root = self.root.resolve()
        if (
            candidate.is_symlink()
            or not target.is_relative_to(root)
            or not target.is_file()
        ):
            raise FileNotFoundError(f"content blob is missing or invalid: {relative}")
        digest = expected_sha256 or target.name
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValueError(f"invalid content digest: {digest}")
        actual, _size = self._digest(target)
        self._metrics["cas_rehash_bytes"] += _size
        if actual != digest or target.name != digest:
            raise ValueError(f"content blob failed SHA-256 validation: {relative}")
        return target

    def issue_trusted_handle(
        self,
        blob: ContentBlob,
        *,
        lineage_id: str,
    ) -> TrustedBlobHandle:
        """Persist a stat-bound handle for bytes just verified by this lineage."""

        if not lineage_id.strip():
            raise ValueError("trusted blob lineage id must be non-empty")
        expected = self.root / blob.sha256[:2] / blob.sha256[2:4] / blob.sha256
        if blob.path != expected or blob.relative_path != expected.relative_to(self.workspace):
            raise ValueError("trusted handle requires one canonical project blob")
        stat = blob.path.stat()
        if not blob.path.is_file() or stat.st_size != blob.size:
            raise ValueError("trusted handle blob size changed after ingestion")
        lineage_digest = hashlib.sha256(lineage_id.encode("utf-8")).hexdigest()
        manifest_ref = (
            Path("Work/content/verified")
            / blob.sha256[:2]
            / blob.sha256
            / f"{lineage_digest}.json"
        )
        handle = TrustedBlobHandle(
            sha256=blob.sha256,
            size=blob.size,
            relative_path=blob.relative_path,
            manifest_ref=manifest_ref,
            lineage_id=lineage_id,
            device=stat.st_dev,
            inode=stat.st_ino,
            mtime_ns=stat.st_mtime_ns,
        )
        payload = {
            "handle_version": 1,
            "sha256": handle.sha256,
            "size": handle.size,
            "relative_path": handle.relative_path.as_posix(),
            "manifest_ref": handle.manifest_ref.as_posix(),
            "lineage_id": handle.lineage_id,
            "device": handle.device,
            "inode": handle.inode,
            "mtime_ns": handle.mtime_ns,
            "issued_at_ns": time.time_ns(),
        }
        path = self.workspace / manifest_ref
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_file():
            existing = json.loads(path.read_text(encoding="utf-8"))
            comparable = {key: existing.get(key) for key in payload if key != "issued_at_ns"}
            expected_comparable = {
                key: value for key, value in payload.items() if key != "issued_at_ns"
            }
            if comparable != expected_comparable:
                raise ValueError("trusted blob handle already exists with different identity")
            return handle
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            prefix=f".{lineage_digest}-",
            suffix=".tmp",
            dir=path.parent,
            delete=False,
        ) as temporary:
            temporary_path = Path(temporary.name)
            json.dump(payload, temporary, ensure_ascii=False, sort_keys=True, indent=2)
            temporary.write("\n")
            temporary.flush()
            os.fsync(temporary.fileno())
        try:
            os.replace(temporary_path, path)
        finally:
            temporary_path.unlink(missing_ok=True)
        return handle

    def load_trusted_handle(self, manifest_ref: Path) -> TrustedBlobHandle:
        relative = Path(manifest_ref)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("trusted blob manifest must be project-relative")
        path = (self.workspace / relative).resolve()
        verified_root = (self.workspace / "Work/content/verified").resolve()
        if not path.is_relative_to(verified_root) or not path.is_file():
            raise FileNotFoundError("trusted blob manifest is missing")
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("handle_version") != 1:
            raise ValueError("unsupported trusted blob handle version")
        handle = TrustedBlobHandle(
            sha256=str(payload["sha256"]),
            size=int(payload["size"]),
            relative_path=Path(payload["relative_path"]),
            manifest_ref=Path(payload["manifest_ref"]),
            lineage_id=str(payload["lineage_id"]),
            device=int(payload["device"]),
            inode=int(payload["inode"]),
            mtime_ns=int(payload["mtime_ns"]),
        )
        if handle.manifest_ref != relative:
            raise ValueError("trusted blob manifest identity mismatch")
        return handle

    def resolve_trusted_handle(
        self,
        handle: TrustedBlobHandle,
        *,
        expected_sha256: str | None = None,
        expected_size: int | None = None,
    ) -> Path:
        """Resolve a same-lineage handle without a second full content scan."""

        loaded = self.load_trusted_handle(handle.manifest_ref)
        if loaded != handle:
            raise ValueError("trusted blob handle does not match its manifest")
        if expected_sha256 is not None and handle.sha256 != expected_sha256:
            raise ValueError("trusted blob handle digest mismatch")
        if expected_size is not None and handle.size != expected_size:
            raise ValueError("trusted blob handle size mismatch")
        target = (self.workspace / handle.relative_path).resolve()
        root = self.root.resolve()
        if not target.is_relative_to(root) or target.name != handle.sha256:
            raise ValueError("trusted blob handle points outside canonical CAS")
        stat = target.stat()
        if (
            not target.is_file()
            or stat.st_size != handle.size
            or stat.st_dev != handle.device
            or stat.st_ino != handle.inode
            or stat.st_mtime_ns != handle.mtime_ns
        ):
            raise ValueError("trusted blob changed since its verified handle was issued")
        self._metrics["cas_trusted_handle_hits"] += 1
        return target

    def link_trusted_view(
        self,
        handle: TrustedBlobHandle,
        target: Path,
        *,
        final_path: Path | None = None,
    ) -> ContentView:
        blob_path = self.resolve_trusted_handle(
            handle,
            expected_sha256=handle.sha256,
            expected_size=handle.size,
        )
        blob = self._blob(handle.sha256, handle.size, blob_path)
        return self._link_view_without_rehash(blob, target, final_path=final_path)

    def scrub_trusted_handle(self, handle: TrustedBlobHandle) -> Path:
        """Perform the periodic/full trust-boundary hash verification."""

        return self.resolve_blob(
            handle.relative_path,
            expected_sha256=handle.sha256,
        )

    def metrics_snapshot(self) -> dict[str, int]:
        return dict(self._metrics)

    def _blob(self, digest: str, size: int, path: Path) -> ContentBlob:
        return ContentBlob(
            sha256=digest,
            size=size,
            path=path,
            relative_path=path.relative_to(self.workspace),
        )

    def _require_project_path(self, path: Path, *, label: str) -> None:
        resolved = path.resolve()
        if not resolved.is_relative_to(self.workspace):
            raise ValueError(f"{label} must stay inside the project workspace")

    def _verify_existing(self, target: Path, digest: str, size: int) -> None:
        if target.is_symlink() or not target.is_file():
            raise ValueError(f"content blob path is not a regular file: {target}")
        actual_digest, actual_size = self._digest(target)
        self._metrics["cas_rehash_bytes"] += actual_size
        if actual_digest != digest or actual_size != size:
            raise ValueError(f"existing content blob failed validation: {target}")

    @staticmethod
    def _digest(path: Path) -> tuple[str, int]:
        digest = hashlib.sha256()
        size = 0
        with Path(path).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
        return digest.hexdigest(), size
