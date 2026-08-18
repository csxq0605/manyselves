"""Filesystem-backed project discovery and lifecycle outside the legacy core."""

import re
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from ..core.project_structure import ensure_project_structure, is_project_workspace
from .project_metadata import ProjectMetadata, ProjectMetadataStore

# 项目 ID 格式：UUID 或传统格式（字母数字开头，可包含点、下划线、连字符）
_PROJECT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_UUID_PATTERN = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z", re.IGNORECASE)


class ProjectRegistryError(RuntimeError):
    """Base class for stable project registry failures."""

    code: str


class InvalidProjectId(ProjectRegistryError):  # noqa: N818 - stable application error
    code = "INVALID_PROJECT_ID"

    def __init__(self) -> None:
        super().__init__("Project ID is invalid")


class ProjectNotFound(ProjectRegistryError):  # noqa: N818 - stable application error
    code = "PROJECT_NOT_FOUND"

    def __init__(self) -> None:
        super().__init__("Project was not found")


class ProjectAlreadyExists(ProjectRegistryError):  # noqa: N818 - stable application error
    code = "PROJECT_ALREADY_EXISTS"

    def __init__(self) -> None:
        super().__init__("Project already exists")


class ActiveProjectMutation(ProjectRegistryError):  # noqa: N818 - stable application error
    code = "ACTIVE_PROJECT_MUTATION"

    def __init__(self) -> None:
        super().__init__("The active project cannot be renamed or deleted")


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    """Portable project metadata without a server filesystem path."""

    id: str
    display_name: str
    description: str
    revision: str
    active: bool


class ProjectRegistry:
    """Own project directories beneath one canonical configured data root."""

    def __init__(self, data_root: Path, initial_project_id: str) -> None:
        self._data_root_input = Path(data_root)
        self._data_root_input.mkdir(parents=True, exist_ok=True)
        if self._data_root_input.is_symlink():
            raise InvalidProjectId()
        self._data_root = self._data_root_input.resolve()
        self._active_project_id = self._validate_id(initial_project_id)
        self._metadata = ProjectMetadataStore()

    @property
    def active_project_id(self) -> str:
        return self._active_project_id

    def ensure_initial(self) -> ProjectRecord:
        """Create the configured initial project when it does not yet exist."""
        root = self._path_for(self._active_project_id)
        if root.exists() and (root.is_symlink() or not root.is_dir()):
            raise InvalidProjectId()
        ensure_project_structure(root)
        return self._record(root)

    def list(self) -> list[ProjectRecord]:
        """Discover canonical workspaces without following symlink entries."""
        records: list[ProjectRecord] = []
        for child in sorted(self._data_root.iterdir(), key=lambda item: item.name.casefold()):
            if child.is_symlink() or not child.is_dir() or not _PROJECT_ID.fullmatch(child.name):
                continue
            if child.name == self._active_project_id or is_project_workspace(child):
                records.append(self._record(child))
        return records

    def get(self, project_id: str) -> ProjectRecord:
        root = self.project_root(project_id)
        return self._record(root)

    def create(self, project_id: str, metadata: ProjectMetadata) -> ProjectRecord:
        project_id = self._validate_id(project_id)
        metadata = self._metadata.validate(metadata)
        root = self._path_for(project_id)
        if root.exists() or root.is_symlink():
            raise ProjectAlreadyExists()
        try:
            root.mkdir()
        except FileExistsError as error:
            raise ProjectAlreadyExists() from error
        try:
            ensure_project_structure(root)
            self._metadata.write(root, metadata, revision=None)
            return self._record(root)
        except BaseException:
            shutil.rmtree(root)
            raise

    def update_metadata(
        self,
        project_id: str,
        metadata: ProjectMetadata,
        revision: str,
    ) -> ProjectRecord:
        """Edit display fields while preserving the directory-backed project identity."""
        root = self.project_root(project_id)
        self._metadata.write(root, metadata, revision)
        return self._record(root)

    def rename(self, project_id: str, destination_id: str) -> ProjectRecord:
        source = self.project_root(project_id)
        if source.name == self._active_project_id:
            raise ActiveProjectMutation()
        destination_id = self._validate_id(destination_id)
        destination = self._path_for(destination_id)
        if destination.exists() or destination.is_symlink():
            raise ProjectAlreadyExists()
        source.rename(destination)
        return self._record(destination)

    def delete(self, project_id: str) -> None:
        root = self.project_root(project_id)
        if root.name == self._active_project_id:
            raise ActiveProjectMutation()
        shutil.rmtree(root)

    def activate(self, project_id: str) -> ProjectRecord:
        root = self.project_root(project_id)
        self._active_project_id = root.name
        return self._record(root)

    def restore_active(self, project_id: str) -> ProjectRecord:
        """Restore a previously validated active ID during activation rollback."""
        self._active_project_id = self._validate_id(project_id)
        return self._record(self.project_root(self._active_project_id))

    def project_root(self, project_id: str) -> Path:
        project_id = self._validate_id(project_id)
        root = self._path_for(project_id)
        if root.is_symlink() or not root.is_dir():
            raise ProjectNotFound()
        resolved = root.resolve()
        if not resolved.is_relative_to(self._data_root):
            raise ProjectNotFound()
        return resolved

    def _path_for(self, project_id: str) -> Path:
        return self._data_root / project_id

    def _record(self, root: Path) -> ProjectRecord:
        metadata = self._metadata.read(root)
        return ProjectRecord(
            id=root.name,
            display_name=metadata.display_name,
            description=metadata.description,
            revision=self._metadata.revision(root),
            active=root.name == self._active_project_id,
        )

    @staticmethod
    def _validate_id(project_id: str) -> str:
        """验证项目 ID 格式，支持 UUID 或传统格式"""
        if project_id in {".", ".."}:
            raise InvalidProjectId()
        # 支持 UUID 格式
        if _UUID_PATTERN.fullmatch(project_id):
            return project_id
        # 支持传统格式
        if not _PROJECT_ID.fullmatch(project_id):
            raise InvalidProjectId()
        return project_id

    @staticmethod
    def generate_id() -> str:
        """生成新的项目 ID (UUID)"""
        return str(uuid.uuid4())
