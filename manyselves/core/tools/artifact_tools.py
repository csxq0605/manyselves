"""Bounded public tools backed by :mod:`manyselves.core.artifacts`."""

from __future__ import annotations

from ..artifacts.gateway import ArtifactGateway
from .registry import Tool


class OpenArtifactTool(Tool):
    name = "open_artifact"
    description = "Open a bounded page of an attached project artifact by path or opaque ref."

    def __init__(self, gateway: ArtifactGateway):
        self.gateway = gateway

    async def __call__(self, ref: str, offset: int = 0, limit: int = 4000) -> dict:
        return self.gateway.open(ref, offset=offset, limit=limit).as_dict()


class OpenToolResultTool(Tool):
    name = "open_tool_result"
    description = "Open a bounded page of a large tool result using its opaque reference."

    def __init__(self, gateway: ArtifactGateway):
        self.gateway = gateway

    async def __call__(self, ref: str, offset: int = 0, limit: int = 4000) -> dict:
        return self.gateway.open_internal(ref, offset=offset, limit=limit).as_dict()


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search an attached text artifact with bounded matches and context."

    def __init__(self, gateway: ArtifactGateway):
        self.gateway = gateway

    async def __call__(self, ref: str, query: str, max_matches: int = 20, context_lines: int = 2) -> dict:
        return self.gateway.search(ref, query, max_matches=max_matches, context_lines=context_lines)
