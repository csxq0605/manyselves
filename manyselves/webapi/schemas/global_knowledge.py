"""Global knowledge file DTOs with no physical storage-path fields."""

from pydantic import BaseModel, RootModel

from .files import FileContent, FileEntryResponse, PreviewUnion, SaveFileRequest


class GlobalKnowledgeFileEntryResponse(FileEntryResponse):
    """One logical global-library entry."""


class GlobalKnowledgeFileTreeResponse(BaseModel):
    entries: list[GlobalKnowledgeFileEntryResponse]


class GlobalKnowledgeFileContent(FileContent):
    """Revision-aware UTF-8 content from the global library."""


class GlobalKnowledgeSaveRequest(SaveFileRequest):
    """Revision-aware global-library text update."""


class GlobalKnowledgePreviewResponse(RootModel[PreviewUnion]):
    """Typed preview for a logical global-library file."""
