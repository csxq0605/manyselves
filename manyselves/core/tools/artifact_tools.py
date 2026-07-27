"""Bounded public tools backed by :mod:`manyselves.core.artifacts`."""

from __future__ import annotations

from collections.abc import Callable

from ..artifacts.gateway import ArtifactGateway
from .registry import Tool


class OpenArtifactTool(Tool):
    name = "open_artifact"
    description = "Open a bounded page of an attached project artifact by path or opaque ref."

    def __init__(
        self,
        gateway: ArtifactGateway,
        *,
        default_limit: int = 4000,
        minimum_limit: int = 1,
        maximum_limit: int = 8000,
    ):
        self.gateway = gateway
        self.maximum_limit = max(maximum_limit, 1)
        self.minimum_limit = min(max(minimum_limit, 1), self.maximum_limit)
        self.default_limit = min(
            max(default_limit, self.minimum_limit), self.maximum_limit
        )
        self._opened_pages: set[tuple[str, int, int]] = set()

    async def __call__(
        self, ref: str, offset: int = 0, limit: int | None = None
    ) -> dict:
        bounded_limit = min(
            max(limit or self.default_limit, self.minimum_limit), self.maximum_limit
        )
        page_key = (ref, offset, bounded_limit)
        if page_key in self._opened_pages:
            return {
                "status": "already_read",
                "ref": ref,
                "offset": offset,
                "limit": bounded_limit,
                "content": "",
                "instruction": "该页已在本任务读取；使用已有工作记忆或读取 next_offset，不要重复读取。",
            }
        page = self.gateway.open(ref, offset=offset, limit=bounded_limit).as_dict()
        self._opened_pages.add(page_key)
        return page


class OpenToolResultTool(Tool):
    name = "open_tool_result"
    description = "Open a bounded page of a large tool result using its opaque reference."

    def __init__(self, gateway: ArtifactGateway):
        self.gateway = gateway
        self._opened_pages: set[tuple[str, int, int]] = set()

    async def __call__(self, ref: str, offset: int = 0, limit: int = 4000) -> dict:
        bounded_limit = min(max(limit, 1), 8000)
        page_key = (ref, offset, bounded_limit)
        if page_key in self._opened_pages:
            return {
                "status": "already_read",
                "ref": ref,
                "offset": offset,
                "limit": bounded_limit,
                "content": "",
            }
        page = self.gateway.open_internal(
            ref, offset=offset, limit=bounded_limit
        ).as_dict()
        self._opened_pages.add(page_key)
        return page


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search an attached text artifact with bounded matches and context."

    def __init__(
        self,
        gateway: ArtifactGateway,
        research_guard: Callable[[], None] | None = None,
    ):
        self.gateway = gateway
        self.research_guard = research_guard

    async def __call__(self, ref: str, query: str, max_matches: int = 20, context_lines: int = 2) -> dict:
        if self.research_guard is not None:
            self.research_guard()
        requested = {"max_matches": max_matches, "context_lines": context_lines}
        max_matches = min(max(max_matches, 1), 50)
        context_lines = min(max(context_lines, 0), 10)
        result = self.gateway.search(
            ref,
            query,
            max_matches=max_matches,
            context_lines=context_lines,
        )
        result["requested_bounds"] = requested
        result["applied_bounds"] = {
            "max_matches": max_matches,
            "context_lines": context_lines,
        }
        return result
