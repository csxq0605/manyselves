"""Revision-safe display metadata stored beside each project workspace."""

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ProjectMetadataError(RuntimeError):
    """Base class for stable project display metadata failures."""

    code: str


class InvalidProjectMetadata(ProjectMetadataError):  # noqa: N818 - stable application error
    code = "INVALID_PROJECT_METADATA"

    def __init__(self) -> None:
        super().__init__("Project metadata is invalid")


class ProjectMetadataRevisionConflict(ProjectMetadataError):  # noqa: N818 - stable application error
    code = "PROJECT_METADATA_REVISION_CONFLICT"

    def __init__(self) -> None:
        super().__init__("Project metadata changed since the supplied revision")


class UnsafeProjectMetadataPath(ProjectMetadataError):  # noqa: N818 - stable application error
    code = "UNSAFE_PROJECT_METADATA_PATH"

    def __init__(self) -> None:
        super().__init__("Project metadata path is unsafe")


@dataclass(frozen=True, slots=True)
class ProjectMetadata:
    """Human-readable fields that never replace a project directory ID."""

    display_name: str
    description: str


class ProjectMetadataStore:
    """Read and atomically update one project's display sidecar."""

    _directory_name = ".manyselves"
    _file_name = "project.json"

    def read(self, project_root: Path) -> ProjectMetadata:
        """Return legacy fallback metadata when no sidecar has been created yet."""
        root = self._root(project_root)
        path = self._metadata_path(root)
        if not path.exists():
            return self._fallback(root)
        if not path.is_file():
            raise InvalidProjectMetadata()
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            metadata = ProjectMetadata(
                display_name=payload["displayName"],
                description=payload["description"],
            )
        except (KeyError, TypeError, json.JSONDecodeError, UnicodeDecodeError) as error:
            raise InvalidProjectMetadata() from error
        return self._normalized(metadata)

    def revision(self, project_root: Path) -> str:
        """Hash the actual sidecar or the deterministic legacy fallback representation."""
        root = self._root(project_root)
        path = self._metadata_path(root)
        raw = self._fallback_bytes(root) if not path.exists() else path.read_bytes()
        return hashlib.sha256(raw).hexdigest()

    def write(self, project_root: Path, metadata: ProjectMetadata, revision: str | None) -> str:
        """Replace metadata atomically after optional optimistic-concurrency validation."""
        root = self._root(project_root)
        normalized = self.validate(metadata)
        path = self._metadata_path(root)
        current_revision = self.revision(root)
        if revision is not None and revision != current_revision:
            raise ProjectMetadataRevisionConflict()

        directory = path.parent
        if directory.exists() and (directory.is_symlink() or not directory.is_dir()):
            raise UnsafeProjectMetadataPath()
        directory.mkdir(exist_ok=True)
        raw = self._encode(normalized)
        descriptor, temporary_name = tempfile.mkstemp(prefix=".manyselves-tmp-", dir=directory)
        temporary = Path(temporary_name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.replace(path)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return hashlib.sha256(raw).hexdigest()

    def validate(self, metadata: ProjectMetadata) -> ProjectMetadata:
        """Normalize fields before a caller creates filesystem state for them."""
        return self._normalized(metadata)

    def _root(self, project_root: Path) -> Path:
        root = Path(project_root)
        if root.is_symlink() or not root.is_dir():
            raise UnsafeProjectMetadataPath()
        return root.resolve()

    def _metadata_path(self, root: Path) -> Path:
        directory = root / self._directory_name
        path = directory / self._file_name
        if directory.is_symlink() or path.is_symlink():
            raise UnsafeProjectMetadataPath()
        return path

    @staticmethod
    def _normalized(metadata: ProjectMetadata) -> ProjectMetadata:
        if not isinstance(metadata.display_name, str) or not isinstance(metadata.description, str):
            raise InvalidProjectMetadata()
        display_name = metadata.display_name.strip()
        if not display_name or len(display_name) > 120 or len(metadata.description) > 1000:
            raise InvalidProjectMetadata()
        return ProjectMetadata(display_name=display_name, description=metadata.description)

    @staticmethod
    def _encode(metadata: ProjectMetadata) -> bytes:
        return json.dumps(
            {"displayName": metadata.display_name, "description": metadata.description},
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")

    def _fallback(self, root: Path) -> ProjectMetadata:
        return ProjectMetadata(display_name=root.name, description="")

    def _fallback_bytes(self, root: Path) -> bytes:
        return self._encode(self._fallback(root))
