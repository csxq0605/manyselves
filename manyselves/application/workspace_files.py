"""Canonical, revision-safe filesystem operations scoped to one project."""

import hashlib
import os
import shutil
import tempfile
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timezone
from io import BufferedReader
from pathlib import Path, PurePosixPath, PureWindowsPath
from threading import RLock
from typing import Literal
from urllib.parse import unquote

_UPLOAD_CONFLICTS = frozenset({"reject", "replace", "keep-both"})


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


class FileAlreadyExists(WorkspaceFileError):  # noqa: N818 - stable upload conflict
    code = "FILE_ALREADY_EXISTS"

    def __init__(self, path: str) -> None:
        self.path = path
        super().__init__("A file already exists at the requested workspace path")


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


class WorkspaceTreeTooLarge(WorkspaceFileError):  # noqa: N818 - stable application error
    code = "WORKSPACE_TREE_TOO_LARGE"

    def __init__(self) -> None:
        super().__init__("Workspace tree exceeds the configured entry limit")


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
    revision: str


@dataclass(slots=True)
class OpenDownload:
    """One validated descriptor plus metadata derived from that same descriptor."""

    stream: BufferedReader
    path: str
    name: str
    size: int
    modified_at: datetime
    revision: str


@dataclass(frozen=True, slots=True)
class _TreeNode:
    kind: str
    stat: os.stat_result
    revision: str


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

    def rename(
        self,
        source_path: str,
        destination_path: str,
        base_revision: str,
    ) -> FileEntry:
        source = self.resolve(source_path)
        destination = self.resolve(destination_path)
        with self._mutation_lock:
            if not source.exists():
                raise WorkspaceEntryNotFound()
            if self._entry_revision(source) != base_revision:
                raise FileRevisionConflict()
            self._require_new_destination(destination)
            source.rename(destination)
        return self.entry(destination_path)

    def delete(self, relative_path: str, base_revision: str) -> None:
        path = self.resolve(relative_path)
        with self._mutation_lock:
            if not path.exists():
                raise WorkspaceEntryNotFound()
            if path.is_symlink():
                raise UnsafeWorkspacePath()
            if self._entry_revision(path) != base_revision:
                raise FileRevisionConflict()
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink()

    async def upload(
        self,
        relative_path: str,
        chunks: AsyncIterator[bytes],
        *,
        conflict: Literal["reject", "replace", "keep-both"] = "reject",
        base_revision: str | None = None,
    ) -> FileEntry:
        if conflict not in _UPLOAD_CONFLICTS:
            raise ValueError("Unsupported upload conflict mode")
        destination = self.resolve(relative_path)
        self._require_existing_directory(destination.parent)
        if conflict == "reject":
            self._require_upload_destination(destination)
        elif conflict == "replace":
            self._require_matching_file(relative_path, base_revision)
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
                final_destination = self._final_upload_destination(
                    relative_path,
                    conflict=conflict,
                    base_revision=base_revision,
                )
                temp.replace(final_destination)
        except BaseException:
            temp.unlink(missing_ok=True)
            raise
        return self.entry(self.relative(final_destination))

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
            revision=self._entry_revision(path),
        )

    def list_tree(self, relative_path: str = "") -> list[FileEntry]:
        root = self.resolve(relative_path, allow_root=True)
        if not root.exists():
            raise WorkspaceEntryNotFound()
        if not root.is_dir():
            raise WorkspaceEntryTypeError()
        _, entries = self._scan_directory(root, include_entries=True)
        return entries

    def file_path(self, relative_path: str) -> Path:
        """Return a validated existing file for trusted application consumers."""
        return self._existing_file(relative_path)

    def open_download(self, relative_path: str) -> OpenDownload:
        """Open a validated file and derive all response metadata from its descriptor."""
        path = self._existing_file(relative_path)
        stream = path.open("rb")
        try:
            stat = os.fstat(stream.fileno())
            revision = _revision_stream(stream)
            stream.seek(0)
            return OpenDownload(
                stream=stream,
                path=self.relative(path),
                name=path.name,
                size=stat.st_size,
                modified_at=_modified_at(stat.st_mtime),
                revision=revision,
            )
        except BaseException:
            stream.close()
            raise

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

    def _require_upload_destination(self, destination: Path) -> None:
        if destination.exists() or destination.is_symlink():
            raise FileAlreadyExists(self.relative(destination))
        self._require_existing_directory(destination.parent)

    def _require_matching_file(
        self,
        relative_path: str,
        base_revision: str | None,
    ) -> Path:
        current = self._existing_file(relative_path)
        if base_revision is None or self._entry_revision(current) != base_revision:
            raise FileRevisionConflict()
        return current

    def _final_upload_destination(
        self,
        relative_path: str,
        *,
        conflict: str,
        base_revision: str | None,
    ) -> Path:
        destination = self.resolve(relative_path)
        self._require_existing_directory(destination.parent)
        if conflict == "reject":
            self._require_upload_destination(destination)
            return destination
        if conflict == "replace":
            return self._require_matching_file(relative_path, base_revision)
        return self._keep_both_destination(destination)

    def _keep_both_destination(self, destination: Path) -> Path:
        if not destination.exists() and not destination.is_symlink():
            return destination
        stem = destination.stem
        suffix = destination.suffix
        index = 1
        while True:
            candidate = destination.with_name(f"{stem} ({index}){suffix}")
            if not candidate.exists() and not candidate.is_symlink():
                return candidate
            index += 1

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

    def _entry_revision(self, path: Path) -> str:
        if path.is_file():
            with path.open("rb") as stream:
                return _revision_stream(stream)
        if not path.is_dir():
            raise WorkspaceEntryTypeError()
        revision, _ = self._scan_directory(path, include_entries=False)
        return revision

    def _scan_directory(
        self,
        root: Path,
        *,
        include_entries: bool,
    ) -> tuple[str, list[FileEntry]]:
        """Hash a tree bottom-up, reusing every child digest exactly once."""
        nodes: dict[Path, _TreeNode] = {}
        children_by_directory: dict[Path, list[Path]] = {}
        entry_paths: list[Path] = []
        pending: list[tuple[Path, bool]] = [(root, False)]
        entry_count = 0
        while pending:
            directory, expanded = pending.pop()
            if not expanded:
                children = sorted(
                    directory.iterdir(),
                    key=lambda item: (item.name.casefold(), item.name),
                )
                children_by_directory[directory] = children
                pending.append((directory, True))
                child_directories: list[Path] = []
                for child in children:
                    entry_count += 1
                    if entry_count > self.max_tree_entries:
                        raise WorkspaceTreeTooLarge()
                    stat = child.lstat()
                    if child.is_symlink():
                        nodes[child] = _TreeNode(
                            kind="symlink",
                            stat=stat,
                            revision=_revision(os.fsencode(os.readlink(child))),
                        )
                    elif child.is_dir():
                        nodes[child] = _TreeNode(kind="directory", stat=stat, revision="")
                        entry_paths.append(child)
                        child_directories.append(child)
                    elif child.is_file():
                        with child.open("rb") as stream:
                            revision = _revision_stream(stream)
                        nodes[child] = _TreeNode(kind="file", stat=stat, revision=revision)
                        entry_paths.append(child)
                pending.extend((child, False) for child in reversed(child_directories))
                continue

            digest = hashlib.sha256(b"directory-v2\0")
            for child in children_by_directory[directory]:
                node = nodes.get(child)
                if node is None:
                    continue
                digest.update(node.kind[0].upper().encode("ascii"))
                digest.update(b"\0" + child.name.encode("utf-8") + b"\0")
                digest.update(bytes.fromhex(node.revision))
            directory_revision = digest.hexdigest()
            if directory == root:
                root_revision = directory_revision
            else:
                node = nodes[directory]
                nodes[directory] = _TreeNode(
                    kind=node.kind,
                    stat=node.stat,
                    revision=directory_revision,
                )

        if not include_entries:
            return root_revision, []
        entries = [
            FileEntry(
                path=self.relative(path),
                name=path.name,
                kind=nodes[path].kind,
                size=None if nodes[path].kind == "directory" else nodes[path].stat.st_size,
                modified_at=_modified_at(nodes[path].stat.st_mtime),
                revision=nodes[path].revision,
            )
            for path in entry_paths
        ]
        return root_revision, entries


def _revision(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _revision_stream(stream: BufferedReader) -> str:
    digest = hashlib.sha256()
    while chunk := stream.read(64 * 1024):
        digest.update(chunk)
    return digest.hexdigest()


def _modified_at(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc)
