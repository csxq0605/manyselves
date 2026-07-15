from pathlib import Path

import pytest

from autoreport.core.reporting.research.web import (
    DisabledWebResearchBackend,
    OpenedWebSource,
    WebSearchHit,
)
from autoreport.core.reporting.source_ledger import SourceLedger
from autoreport.core.tools.reporting_research_tools import (
    OpenWebSourceTool,
    WebSearchTool,
)


class PermissiveFakeWebBackend:
    def __init__(self):
        self.opened: list[str] = []

    async def search(self, query: str, limit: int = 5):
        return [
            WebSearchHit(
                title="机构技术说明",
                url="https://example.org/guide",
                snippet="连接温升机理",
                publisher="Example Institute",
            )
        ][:limit]

    async def open(self, url: str):
        self.opened.append(url)
        return OpenedWebSource(
            title="机构技术说明",
            url=url,
            text="完整技术说明",
            publisher="Example Institute",
            published_at="2026-01-02",
        )


@pytest.mark.asyncio
async def test_web_open_requires_prior_search_and_creates_w_source(tmp_path: Path):
    backend = PermissiveFakeWebBackend()
    ledger = SourceLedger(tmp_path, "run-1")
    search = WebSearchTool(backend)
    opener = OpenWebSourceTool(backend, ledger)

    with pytest.raises(ValueError, match="web_search"):
        await opener(url="https://untrusted.example/")
    assert backend.opened == []

    result = await search(query="配电连接温升", limit=3)
    opened = await opener(url=result["hits"][0]["url"])

    assert opened["source_id"] == "W-001"
    assert opened["publisher"] == "Example Institute"
    assert opened["accessed_at"]


@pytest.mark.asyncio
async def test_disabled_web_backend_explains_configuration():
    with pytest.raises(RuntimeError, match="BRAVE_SEARCH_API_KEY"):
        await DisabledWebResearchBackend().search("断路器", 3)
