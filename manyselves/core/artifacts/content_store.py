"""Project-local content-addressed storage with compatibility file views."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
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


class ContentAddressedStore:
    """Store each byte sequence once and expose suffix-preserving file views."""

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = self.workspace / "Work" / "content" / "sha256"

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
            target = self.root / digest[:2] / digest[2:4] / digest
            if target.exists():
                self._verify_existing(target, digest, size)
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
        if actual != digest or target.name != digest:
            raise ValueError(f"content blob failed SHA-256 validation: {relative}")
        return target

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
