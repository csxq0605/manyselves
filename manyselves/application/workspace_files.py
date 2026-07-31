"""Canonical, revision-safe filesystem operations scoped to one project."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from urllib.parse import unquote


class WorkspaceFileError(RuntimeError):
    """Base class for stable workspace file failures."""

    code: str


class UnsafeWorkspacePath(WorkspaceFileError):  # noqa: N818 - locked application error name
    code = "UNSAFE_WORKSPACE_PATH"

    def __init__(self) -> None:
        super().__init__("Workspace path is unsafe")


class FileRevisionConflict(WorkspaceFileError):  # noqa: N818 - locked application error name
    code = "FILE_REVISION_CONFLICT"

    def __init__(self) -> None:
        super().__init__("File changed since the supplied base revision")


class DestinationExists(WorkspaceFileError):  # noqa: N818 - locked application error name
    code = "DESTINATION_EXISTS"

    def __init__(self) -> None:
        super().__init__("Destination already exists")


class WorkspaceEntryNotFound(WorkspaceFileError):  # noqa: N818 - stable application error
    code = "WORKSPACE_ENTRY_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("Workspace entry was not found")


class WorkspaceEntryTypeError(WorkspaceFileError):
    code = "WORKSPACE_ENTRY_TYPE_ERROR"

    def __init__(self) -> None:
        super().__init__("Workspace entry has the wrong type")


class TextFileTooLarge(WorkspaceFileError):  # noqa: N818 - stable application error
    code = "TEXT_FILE_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("Text file exceeds the configured read limit")


class UploadTooLarge(WorkspaceFileError):  # noqa: N818 - locked application error name
    code = "UPLOAD_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("Upload exceeds the configured size limit")


@dataclass(frozen=True, slots=True)
class TextFile:
    path: str
    revision: str
    size: int
    modified_at: datetime
    content: str


@dataclass(frozen=True, slots=True)
class FileEntry:
    path: str
    name: str
    kind: str
    size: int | None
    modified_at: datetime


class WorkspaceFiles:
    """Perform filesystem access only after canonical project-root validation."""

    def __init__(
        self,
        project_root: Path,
        *,
        max_text_bytes: int = 2 * 1024 * 1024,
        max_upload_bytes: int = 100 * 1024 * 1024,
        max_tree_entries: int = 20_000,
    ) -> None:
        root_input = Path(project_root)
        if root_input.is_symlink() or not root_input.is_dir():
            raise UnsafeWorkspacePath()
        if min(max_text_bytes, max_upload_bytes, max_tree_entries) <= 0:
            raise ValueError("Workspace limits must be positive")
        self.project_root = root_input.resolve()
        self.max_text_bytes = max_text_bytes
        self.max_upload_bytes = max_upload_bytes
        self.max_tree_entries = max_tree_entries
        self._mutation_lock = RLock()

    def resolve(self, relative_path: str, *, allow_root: bool = False) -> Path:
        """Resolve one untrusted portable relative path without following symlinks."""
        if not isinstance(relative_path, str) or "\x00" in relative_path:
            raise UnsafeWorkspacePath()
        if relative_path == "":
            if allow_root:
                return self.project_root
            raise UnsafeWorkspacePath()
        decoded = unquote(relative_path)
        if decoded != relative_path and (
            "/" in decoded or "\\" in decoded or ".." in PurePosixPath(decoded).parts
        ):
            raise UnsafeWorkspacePath()
        if (
            PurePosixPath(relative_path).is_absolute()
            or PureWindowsPath(relative_path).is_absolute()
            or "\\" in relative_path
        ):
            raise UnsafeWorkspacePath()
        parts = PurePosixPath(relative_path).parts
        if not parts or any(part in {"", ".", ".."} for part in parts):
            raise UnsafeWorkspacePath()

        lexical = self.project_root.joinpath(*parts)
        current = self.project_root
        for part in parts:
            current = current / part
            if current.is_symlink():
                raise UnsafeWorkspacePath()
            if not current.exists():
                break
        candidate = lexical.resolve(strict=False)
        if not candidate.is_relative_to(self.project_root):
            raise UnsafeWorkspacePath()
        return candidate

    def read_text(self, relative_path: str) -> TextFile:
        path = self._existing_file(relative_path)
        stat = path.stat()
        if stat.st_size > self.max_text_bytes:
            raise TextFileTooLarge()
        raw = path.read_bytes()
        if len(raw) > self.max_text_bytes:
            raise TextFileTooLarge()
        return TextFile(
            path=self.relative(path),
            revision=_revision(raw),
            size=len(raw),
            modified_at=_modified_at(stat.st_mtime),
            content=raw.decode("utf-8"),
        )

    def write_text(self, relative_path: str, content: str, base_revision: str) -> TextFile:
        path = self.resolve(relative_path)
        raw = content.encode("utf-8")
        if len(raw) > self.max_text_bytes:
            raise TextFileTooLarge()
        with self._mutation_lock:
            current = self._existing_file(relative_path)
            if _revision(current.read_bytes()) != base_revision:
                raise FileRevisionConflict()
            self._atomic_replace(path, raw)
        return self.read_text(relative_path)

    def create_file(self, relative_path: str, content: str = "") -> TextFile:
        path = self.resolve(relative_path)
        raw = content.encode("utf-8")
        if len(raw) > self.max_text_bytes:
            raise TextFileTooLarge()
        with self._mutation_lock:
            self._require_new_destination(path)
            self._atomic_replace(path, raw)
        return self.read_text(relative_path)

    def create_directory(self, relative_path: str) -> FileEntry:
        path = self.resolve(relative_path)
        with self._mutation_lock:
            self._require_new_destination(path)
            path.mkdir()
        return self.entry(relative_path)

    def rename(self, source_path: str, destination_path: str) -> FileEntry:
        source = self.resolve(source_path)
        destination = self.resolve(destination_path)
        with self._mutation_lock:
            if not source.exists():
                raise WorkspaceEntryNotFound()
            self._require_new_destination(destination)
            source.rename(destination)
        return self.entry(destination_path)

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        with self._mutation_lock:
            if not path.exists():
                raise WorkspaceEntryNotFound()
            if path.is_symlink():
                raise UnsafeWorkspacePath()
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    async def upload(self, relative_path: str, chunks: AsyncIterator[bytes]) -> FileEntry:
        destination = self.resolve(relative_path)
        self._require_new_destination(destination)
        self._require_existing_directory(destination.parent)
        fd, temp_name = tempfile.mkstemp(prefix=".manyselves-tmp-", dir=destination.parent)
        temp = Path(temp_name)
        size = 0
        try:
            with os.fdopen(fd, "wb") as stream:
                async for chunk in chunks:
                    size += len(chunk)
                    if size > self.max_upload_bytes:
                        raise UploadTooLarge()
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            with self._mutation_lock:
                self._require_new_destination(destination)
                temp.replace(destination)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return self.entry(relative_path)

    def entry(self, relative_path: str) -> FileEntry:
        path = self.resolve(relative_path)
        if not path.exists():
            raise WorkspaceEntryNotFound()
        if path.is_symlink():
            raise UnsafeWorkspacePath()
        stat = path.stat()
        return FileEntry(
            path=self.relative(path),
            name=path.name,
            kind="directory" if path.is_dir() else "file",
            size=None if path.is_dir() else stat.st_size,
            modified_at=_modified_at(stat.st_mtime),
        )

    def list_tree(self, relative_path: str = "") -> list[FileEntry]:
        root = self.resolve(relative_path, allow_root=True)
        if not root.exists():
            raise WorkspaceEntryNotFound()
        if not root.is_dir():
            raise WorkspaceEntryTypeError()
        entries: list[FileEntry] = []
        pending = [root]
        while pending:
            directory = pending.pop()
            for child in sorted(directory.iterdir(), key=lambda item: item.name.casefold()):
                if child.is_symlink():
                    continue
                entries.append(self.entry(self.relative(child)))
                if len(entries) > self.max_tree_entries:
                    raise WorkspaceFileError("File tree exceeds configured entry limit")
                if child.is_dir():
                    pending.append(child)
        return entries

    def file_path(self, relative_path: str) -> Path:
        """Return a validated existing file for trusted application consumers."""
        return self._existing_file(relative_path)

    def relative(self, path: Path) -> str:
        return path.relative_to(self.project_root).as_posix()

    def _existing_file(self, relative_path: str) -> Path:
        path = self.resolve(relative_path)
        if not path.exists():
            raise WorkspaceEntryNotFound()
        if not path.is_file():
            raise WorkspaceEntryTypeError()
        return path

    def _require_new_destination(self, path: Path) -> None:
        if path.exists() or path.is_symlink():
            raise DestinationExists()
        self._require_existing_directory(path.parent)

    def _require_existing_directory(self, path: Path) -> None:
        if not path.exists():
            raise WorkspaceEntryNotFound()
        if not path.is_dir() or path.is_symlink():
            raise UnsafeWorkspacePath()

    def _atomic_replace(self, destination: Path, raw: bytes) -> None:
        self._require_existing_directory(destination.parent)
        fd, temp_name = tempfile.mkstemp(prefix=".manyselves-tmp-", dir=destination.parent)
        temp = Path(temp_name)
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temp.replace(destination)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _modified_at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
