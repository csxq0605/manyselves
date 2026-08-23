"""Opt-in project reference and web research primitives."""

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
    "ReferenceDocument",
    "ReferenceHit",
    "ReferenceLibrary",
    "WebResearchBackend",
    "WebSearchHit",
]
