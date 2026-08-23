"""Optional web research backends with search-before-open enforcement."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Protocol

import httpx


@dataclass(frozen=True)
class WebSearchHit:
    title: str
    url: str
    snippet: str
    publisher: str | None = None
    published_at: str | None = None


@dataclass(frozen=True)
class OpenedWebSource:
    title: str
    url: str
    text: str
    publisher: str | None = None
    published_at: str | None = None


class WebResearchBackend(Protocol):
    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]: ...

    async def open(self, url: str) -> OpenedWebSource: ...


class DisabledWebResearchBackend:
    """Explicit backend used when no web-search credential is configured."""

    def _error(self) -> RuntimeError:
        return RuntimeError(
            "web research is disabled; set BRAVE_SEARCH_API_KEY to enable it"
        )

    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]:
        raise self._error()

    async def open(self, url: str) -> OpenedWebSource:
        raise self._error()


class BraveWebResearchBackend:
    """Small Brave Search adapter; callers may inject an HTTP client for tests."""

    def __init__(self, api_key: str, client: httpx.AsyncClient | None = None):
        if not api_key.strip():
            raise ValueError("Brave API key must not be blank")
        self.api_key = api_key
        self.client = client or httpx.AsyncClient(timeout=20.0, follow_redirects=True)
        self.allowed_urls: set[str] = set()

    async def search(self, query: str, limit: int = 5) -> list[WebSearchHit]:
        if not query.strip() or limit <= 0:
            return []
        response = await self.client.get(
            "https://api.search.brave.com/res/v1/web/search",
            params={"q": query, "count": min(limit, 10)},
            headers={
                "Accept": "application/json",
                "X-Subscription-Token": self.api_key,
            },
        )
        response.raise_for_status()
        hits = [
            WebSearchHit(
                title=item.get("title") or item["url"],
                url=item["url"],
                snippet=item.get("description", ""),
                publisher=(item.get("profile") or {}).get("long_name"),
                published_at=item.get("age"),
            )
            for item in response.json().get("web", {}).get("results", [])
            if item.get("url")
        ]
        self.allowed_urls.update(hit.url for hit in hits)
        return hits

    async def open(self, url: str) -> OpenedWebSource:
        if url not in self.allowed_urls:
            raise ValueError("URL was not returned by web_search")
        response = await self.client.get(url)
        response.raise_for_status()
        title_match = re.search(
            r"<title[^>]*>(.*?)</title>", response.text, flags=re.IGNORECASE | re.DOTALL
        )
        title = " ".join(title_match.group(1).split()) if title_match else url
        text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", response.text, flags=re.I | re.S)
        text = " ".join(re.sub(r"<[^>]+>", " ", text).split())[:30_000]
        return OpenedWebSource(title=title, url=url, text=text)
