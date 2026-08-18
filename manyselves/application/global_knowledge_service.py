"""Fixed-root, revision-safe storage for server-managed global knowledge."""

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Literal

from .workspace_files import (
    FileEntry,
    OpenDownload,
    TextFile,
    UnsafeWorkspacePath,
    WorkspaceFiles,
)


class GlobalKnowledgeService:
    """Compose safe file operations beneath one trusted hidden data-root directory."""

    def __init__(
        self,
        root: Path,
        *,
        max_text_bytes: int = 2 * 1024 * 1024,
        max_upload_bytes: int = 100 * 1024 * 1024,
        max_tree_entries: int = 20_000,
    ) -> None:
        root_input = Path(root)
        if root_input.is_symlink() or not root_input.is_dir():
            raise UnsafeWorkspacePath()
        self._files = WorkspaceFiles(
            root_input,
            max_text_bytes=max_text_bytes,
            max_upload_bytes=max_upload_bytes,
            max_tree_entries=max_tree_entries,
        )

    @classmethod
    def from_data_root(
        cls,
        data_root: Path,
        *,
        max_text_bytes: int = 2 * 1024 * 1024,
        max_upload_bytes: int = 100 * 1024 * 1024,
        max_tree_entries: int = 20_000,
    ) -> "GlobalKnowledgeService":
        """Create only the fixed hidden library and reject symlinked storage parents."""
        base = Path(data_root)
        if base.is_symlink():
            raise UnsafeWorkspacePath()
        base.mkdir(parents=True, exist_ok=True)
        if not base.is_dir():
            raise UnsafeWorkspacePath()
        resolved_base = base.resolve()
        hidden = resolved_base / ".manyselves"
        root = hidden / "global-knowledge"
        for directory in (hidden, root):
            if directory.is_symlink():
                raise UnsafeWorkspacePath()
            directory.mkdir(exist_ok=True)
            if not directory.is_dir() or not directory.resolve().is_relative_to(resolved_base):
                raise UnsafeWorkspacePath()
        return cls(
            root,
            max_text_bytes=max_text_bytes,
            max_upload_bytes=max_upload_bytes,
            max_tree_entries=max_tree_entries,
        )

    @property
    def root(self) -> Path:
        return self._files.project_root

    @property
    def max_upload_bytes(self) -> int:
        return self._files.max_upload_bytes

    @property
    def workspace_files(self) -> WorkspaceFiles:
        """Expose the composed safe-file boundary to trusted preview adapters."""
        return self._files

    def resolve(self, relative_path: str, *, allow_root: bool = False) -> Path:
        return self._files.resolve(relative_path, allow_root=allow_root)

    def list_tree(self, relative_path: str = "") -> list[FileEntry]:
        return self._files.list_tree(relative_path)

    def read_text(self, relative_path: str) -> TextFile:
        return self._files.read_text(relative_path)

    def write_text(self, relative_path: str, content: str, base_revision: str) -> TextFile:
        return self._files.write_text(relative_path, content, base_revision)

    def create_file(self, relative_path: str, content: str = "") -> TextFile:
        return self._files.create_file(relative_path, content)

    async def upload(
        self,
        relative_path: str,
        chunks: AsyncIterator[bytes],
        *,
        conflict: Literal["reject", "replace", "keep-both"] = "reject",
        base_revision: str | None = None,
    ) -> FileEntry:
        return await self._files.upload(
            relative_path,
            chunks,
            conflict=conflict,
            base_revision=base_revision,
        )

    def delete(self, relative_path: str, base_revision: str) -> None:
        self._files.delete(relative_path, base_revision)

    def entry(self, relative_path: str) -> FileEntry:
        return self._files.entry(relative_path)

    def open_download(self, relative_path: str) -> OpenDownload:
        return self._files.open_download(relative_path)
