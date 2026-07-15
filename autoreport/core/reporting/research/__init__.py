"""Opt-in project reference and web research primitives."""

from .project_evidence import ProjectEvidenceIndex
from .reference_library import ReferenceDocument, ReferenceHit, ReferenceLibrary
from .web import (
    BraveWebResearchBackend,
    DisabledWebResearchBackend,
    OpenedWebSource,
    WebResearchBackend,
    WebSearchHit,
)

__all__ = [
    "BraveWebResearchBackend",
    "DisabledWebResearchBackend",
    "OpenedWebSource",
    "ProjectEvidenceIndex",
    "ReferenceDocument",
    "ReferenceHit",
    "ReferenceLibrary",
    "WebResearchBackend",
    "WebSearchHit",
]
