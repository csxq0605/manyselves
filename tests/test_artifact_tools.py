from __future__ import annotations

import pytest

from manyselves.core.tools.artifact_tools import (
    OpenArtifactTool,
    OpenToolResultTool,
    SearchTextTool,
)


class _Gateway:
    def __init__(self) -> None:
        self.open_calls: list[tuple[str, int, int]] = []

    def open(self, ref: str, *, offset: int, limit: int):
        self.open_calls.append((ref, offset, limit))

        class Page:
            @staticmethod
            def as_dict() -> dict:
                return {"ref": ref, "offset": offset, "limit": limit, "content": "正文"}

        return Page()

    def search(
        self,
        ref: str,
        query: str,
        *,
        max_matches: int,
        context_lines: int,
    ) -> dict:
        return {"ref": ref, "query": query}

    def open_internal(self, ref: str, *, offset: int, limit: int):
        return self.open(ref, offset=offset, limit=limit)


@pytest.mark.asyncio
async def test_search_text_obeys_shared_research_guard() -> None:
    def exhausted() -> None:
        raise RuntimeError("RESEARCH_PHASE_COMPLETE")

    tool = SearchTextTool(_Gateway(), research_guard=exhausted)  # type: ignore[arg-type]

    with pytest.raises(RuntimeError, match="RESEARCH_PHASE_COMPLETE"):
        await tool("artifact-ref", "关键词")


@pytest.mark.asyncio
async def test_search_text_clamps_model_bounds_instead_of_triggering_retry_loop() -> None:
    gateway = _Gateway()
    tool = SearchTextTool(gateway)  # type: ignore[arg-type]

    result = await tool(
        "artifact-ref",
        "关键词",
        max_matches=500,
        context_lines=80,
    )

    assert result["requested_bounds"] == {"max_matches": 500, "context_lines": 80}
    assert result["applied_bounds"] == {"max_matches": 50, "context_lines": 10}


@pytest.mark.asyncio
async def test_open_artifact_clamps_oversized_pages_and_suppresses_duplicate_reads() -> None:
    gateway = _Gateway()
    tool = OpenArtifactTool(gateway)  # type: ignore[arg-type]

    first = await tool("module.json", limit=80_000)
    duplicate = await tool("module.json", limit=80_000)

    assert first["limit"] == 8000
    assert duplicate["status"] == "already_read"
    assert gateway.open_calls == [("module.json", 0, 8000)]


@pytest.mark.asyncio
async def test_open_artifact_can_enforce_large_pages_for_audit_bundles() -> None:
    gateway = _Gateway()
    tool = OpenArtifactTool(  # type: ignore[arg-type]
        gateway,
        default_limit=8000,
        minimum_limit=8000,
    )

    page = await tool("cross-review-input.json", offset=8000, limit=600)

    assert page["limit"] == 8000
    assert gateway.open_calls == [("cross-review-input.json", 8000, 8000)]


@pytest.mark.asyncio
async def test_open_artifact_can_read_a_complete_audit_bundle_in_one_call() -> None:
    gateway = _Gateway()
    tool = OpenArtifactTool(  # type: ignore[arg-type]
        gateway,
        default_limit=160_000,
        minimum_limit=160_000,
        maximum_limit=160_000,
    )

    page = await tool("cross-review-input.json", limit=600)

    assert page["limit"] == 160_000
    assert gateway.open_calls == [("cross-review-input.json", 0, 160_000)]


@pytest.mark.asyncio
async def test_open_tool_result_starts_with_8000_character_page_by_default() -> None:
    gateway = _Gateway()
    tool = OpenToolResultTool(gateway)  # type: ignore[arg-type]

    page = await tool("tool-result-ref", offset=5488)

    assert page["limit"] == 8000
    assert gateway.open_calls == [("tool-result-ref", 5488, 8000)]
