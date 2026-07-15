"""Agent-selectable reporting research and source tools."""

from __future__ import annotations

from dataclasses import asdict
from pathlib import Path
from uuid import uuid4

from ...interfaces.types import ResearchNotePublishedMessage
from ..loops.bus import MessageBus
from ..reporting.agentic_models import ResearchNote
from ..reporting.research.project_evidence import ProjectEvidenceIndex
from ..reporting.research.reference_library import ReferenceLibrary
from ..reporting.research.web import WebResearchBackend
from ..reporting.source_ledger import SourceLedger
from ..reporting.store import ReportingStore
from .registry import Tool


def _evidence_locator(item) -> str:
    source = item.source
    suffixes = []
    if source.sheet:
        suffixes.append(source.sheet)
    if source.cell:
        suffixes.append(source.cell)
    if source.page:
        suffixes.append(f"page={source.page}")
    suffix = "#" + "!".join(suffixes) if suffixes else ""
    return f"{source.path.as_posix()}{suffix}"


class SearchProjectEvidenceTool(Tool):
    name = "search_project_evidence"
    description = (
        "Search normalized customer project evidence. Returns only E-* facts and exact "
        "file locations; use external references only for interpretation."
    )

    def __init__(self, workspace: Path, ledger: SourceLedger | None = None):
        self.index = ProjectEvidenceIndex(workspace)
        self.ledger = ledger

    async def __call__(self, query: str, limit: int = 10) -> dict:
        """Search project evidence.

        Args:
            query: Terms describing the customer fact, asset, value, or location.
            limit: Maximum number of matching E-* items.
        """
        items = self.index.search(query, limit)
        if self.ledger:
            for item in items:
                self.ledger.register_project(
                    item.id,
                    item.subject,
                    _evidence_locator(item),
                    item.model_dump_json(),
                )
        return {"hits": [item.model_dump(mode="json") for item in items]}


class OpenProjectSourceTool(Tool):
    name = "open_project_source"
    description = "Open one normalized E-* project evidence record by its source id."

    def __init__(self, workspace: Path, ledger: SourceLedger | None = None):
        self.index = ProjectEvidenceIndex(workspace)
        self.ledger = ledger

    async def __call__(self, source_id: str) -> dict:
        """Open project evidence.

        Args:
            source_id: Existing E-* evidence identifier.
        """
        item = self.index.get(source_id)
        if self.ledger:
            self.ledger.register_project(
                item.id,
                item.subject,
                _evidence_locator(item),
                item.model_dump_json(),
            )
        return {"evidence": item.model_dump(mode="json")}


class SearchReferenceLibraryTool(Tool):
    name = "search_reference_library"
    description = (
        "Optionally search the allowed 01 local reference library for methods, terms, "
        "thresholds, or mechanisms. R-* results are not customer facts."
    )

    def __init__(self, library: ReferenceLibrary, ledger: SourceLedger):
        self.library = library
        self.ledger = ledger

    async def __call__(self, query: str, limit: int = 5) -> dict:
        """Search local references.

        Args:
            query: Current technical research question.
            limit: Maximum number of matching references.
        """
        hits = self.library.search(query, limit)
        payload = []
        for hit in hits:
            document = self.library.open(hit.relative_path)
            source = self.ledger.register_local(
                hit.title, hit.relative_path, document.text
            )
            payload.append(
                {
                    "source_id": source.id,
                    "title": hit.title,
                    "locator": hit.relative_path,
                    "snippet": hit.snippet,
                    "score": hit.score,
                }
            )
        return {"hits": payload}


class OpenReferenceTool(Tool):
    name = "open_reference"
    description = "Open a file beneath the allowed 01 library and register it as R-*."

    def __init__(self, library: ReferenceLibrary, ledger: SourceLedger):
        self.library = library
        self.ledger = ledger

    async def __call__(self, locator: str) -> dict:
        """Open a local reference.

        Args:
            locator: Workspace-relative path returned by search_reference_library.
        """
        document = self.library.open(locator)
        source = self.ledger.register_local(
            document.title, document.relative_path, document.text
        )
        return {
            "source_id": source.id,
            "title": document.title,
            "locator": document.relative_path,
            "text": document.text,
        }


def _web_allowlist(backend: WebResearchBackend) -> set[str]:
    allowed = getattr(backend, "_autoreport_search_result_urls", None)
    if allowed is None:
        allowed = set()
        setattr(backend, "_autoreport_search_result_urls", allowed)
    return allowed


class WebSearchTool(Tool):
    name = "web_search"
    description = (
        "Optionally search public web sources. Search snippets are leads, not customer "
        "facts; open a result before citing it."
    )

    def __init__(self, backend: WebResearchBackend):
        self.backend = backend

    async def __call__(self, query: str, limit: int = 5) -> dict:
        """Search the web.

        Args:
            query: Public-source research question.
            limit: Maximum number of search results.
        """
        hits = await self.backend.search(query, limit)
        _web_allowlist(self.backend).update(hit.url for hit in hits)
        return {"hits": [asdict(hit) for hit in hits]}


class OpenWebSourceTool(Tool):
    name = "open_web_source"
    description = (
        "Open a URL returned by this run's web_search and register the opened page as W-*."
    )

    def __init__(self, backend: WebResearchBackend, ledger: SourceLedger):
        self.backend = backend
        self.ledger = ledger

    async def __call__(self, url: str) -> dict:
        """Open a whitelisted web result.

        Args:
            url: Exact URL returned by an earlier web_search call.
        """
        if url not in _web_allowlist(self.backend):
            raise ValueError("URL was not returned by web_search")
        opened = await self.backend.open(url)
        source = self.ledger.register_web(
            opened.title,
            opened.url,
            opened.text,
            opened.publisher,
            opened.published_at,
        )
        return {
            "source_id": source.id,
            **asdict(opened),
            "accessed_at": source.accessed_at,
        }


class OpenSourceTool(OpenWebSourceTool):
    """Compatibility name used by existing packaged identities."""

    name = "open_source"


class PublishResearchNoteTool(Tool):
    name = "publish_research_note"
    description = (
        "Persist a reusable research synthesis and announce only its artifact reference "
        "to peers. Use when the finding can help other tasks."
    )

    def __init__(
        self,
        workspace: Path,
        bus: MessageBus,
        workflow_id: str,
        run_id: str,
        task_id: str,
        agent_id: str,
    ):
        self.store = ReportingStore(workspace)
        self.bus = bus
        self.workflow_id = workflow_id
        self.run_id = run_id
        self.task_id = task_id
        self.agent_id = agent_id

    async def __call__(
        self,
        question: str,
        synthesis: str,
        source_ids: list[str],
        applicability: str,
    ) -> dict:
        """Publish a research note.

        Args:
            question: Research question answered by the note.
            synthesis: Concise conclusion; long extracts stay in source artifacts.
            source_ids: E-*, R-*, or W-* records supporting the synthesis.
            applicability: Conditions and limits for applying the finding.
        """
        note = ResearchNote(
            id=f"RN-{uuid4().hex[:12]}",
            task_id=self.task_id,
            question=question,
            synthesis=synthesis,
            source_ids=source_ids,
            applicability=applicability,
        )
        path = self.store.write_run_model(
            self.run_id, f"research/{note.id}.json", note
        )
        artifact_ref = path.relative_to(self.store.workspace).as_posix()
        await self.bus.publish(
            ResearchNotePublishedMessage(
                workflow_id=self.workflow_id,
                task_id=self.task_id,
                sender=self.agent_id,
                recipient="workflow",
                artifact_refs=[artifact_ref],
                content=note.synthesis,
                note_id=note.id,
            )
        )
        return {
            "status": "published",
            "note_id": note.id,
            "artifact_ref": artifact_ref,
        }
