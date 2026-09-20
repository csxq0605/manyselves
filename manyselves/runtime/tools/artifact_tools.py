"""Bounded public tools backed by :mod:`manyselves.runtime.artifacts`."""

from __future__ import annotations

from collections.abc import Callable, Iterable

from ..artifacts.gateway import ArtifactGateway, ToolContractError, contract_error_result
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
        allowed_refs: Iterable[str] | None = None,
    ):
        self.gateway = gateway
        self.maximum_limit = max(maximum_limit, 1)
        self.minimum_limit = min(max(minimum_limit, 1), self.maximum_limit)
        self.default_limit = min(
            max(default_limit, self.minimum_limit), self.maximum_limit
        )
        self.allowed_refs = frozenset(allowed_refs) if allowed_refs is not None else None
        self._opened_pages: set[tuple[str, int, int]] = set()

    async def __call__(
        self, ref: str, offset: int = 0, limit: int | None = None
    ) -> dict:
        if self.allowed_refs is not None and ref not in self.allowed_refs:
            raise PermissionError("artifact was not delivered by reference for this task")
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
        try:
            page = self.gateway.open(ref, offset=offset, limit=bounded_limit).as_dict()
        except ToolContractError as error:
            return contract_error_result(error, ref=ref, offset=offset, limit=bounded_limit)
        self._opened_pages.add(page_key)
        return page


class OpenToolResultTool(Tool):
    name = "open_tool_result"
    description = (
        "Continue a truncated tool result using its opaque reference. The default and "
        "maximum page size are both 8000 characters. Start at the prior result's "
        "next_offset, omit limit unless a smaller final page is necessary, and never "
        "reopen text already included in the truncation preview."
    )

    def __init__(self, gateway: ArtifactGateway):
        self.gateway = gateway
        self._opened_pages: set[tuple[str, int, int]] = set()

    async def __call__(self, ref: str, offset: int = 0, limit: int = 8000) -> dict:
        """Open the next page of a truncated tool result.

        Args:
            ref: Opaque full_result_ref returned by the truncation envelope.
            offset: Character offset; use the prior envelope or page's next_offset.
            limit: Page size in characters. Defaults to the maximum 8000 characters.
        """
        bounded_limit = min(max(limit, 1), 8000)
        page_key = (ref, offset, bounded_limit)
        if page_key in self._opened_pages:
            return {
                "status": "already_read",
                "ref": ref,
                "offset": offset,
                "limit": bounded_limit,
                "content": "",
                "instruction": (
                    "该页已读取；使用已有工作记忆并从上一页 next_offset 继续，"
                    "不要重新读取预览或已读分页。"
                ),
            }
        try:
            page = self.gateway.open_internal(
                ref, offset=offset, limit=bounded_limit
            ).as_dict()
        except ToolContractError as error:
            return contract_error_result(error, ref=ref, offset=offset, limit=bounded_limit)
        self._opened_pages.add(page_key)
        return page


class SearchTextTool(Tool):
    name = "search_text"
    description = "Search an attached text artifact with bounded matches and context."

    def __init__(
        self,
        gateway: ArtifactGateway,
        research_guard: Callable[[], None] | None = None,
        *,
        allowed_refs: Iterable[str] | None = None,
    ):
        self.gateway = gateway
        self.research_guard = research_guard
        self.allowed_refs = frozenset(allowed_refs) if allowed_refs is not None else None

    async def __call__(self, ref: str, query: str, max_matches: int = 20, context_lines: int = 2) -> dict:
        if self.allowed_refs is not None and ref not in self.allowed_refs:
            raise PermissionError("artifact was not delivered by reference for this task")
        if self.research_guard is not None:
            self.research_guard()
        requested = {"max_matches": max_matches, "context_lines": context_lines}
        max_matches = min(max(max_matches, 1), 50)
        context_lines = min(max(context_lines, 0), 10)
        try:
            result = self.gateway.search(
                ref,
                query,
                max_matches=max_matches,
                context_lines=context_lines,
            )
        except ToolContractError as error:
            return contract_error_result(error, ref=ref, query=query)
        result["requested_bounds"] = requested
        result["applied_bounds"] = {
            "max_matches": max_matches,
            "context_lines": context_lines,
        }
        return result
