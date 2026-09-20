"""Capability-owned project, reference, and web research runtime primitives."""

from .project_evidence import ProjectEvidenceIndex, project_evidence_locator
from .reference_library import (
    KnowledgeNamespace,
    ReferenceDocument,
    ReferenceHit,
    ReferenceLibrary,
)
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
    "KnowledgeNamespace",
    "OpenedWebSource",
    "ProjectEvidenceIndex",
    "ReferenceDocument",
    "ReferenceHit",
    "ReferenceLibrary",
    "WebResearchBackend",
    "WebSearchHit",
    "project_evidence_locator",
]
