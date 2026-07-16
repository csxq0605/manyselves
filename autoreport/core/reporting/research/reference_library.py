"""Search the project-local ``Knowledge`` reference library."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from docx import Document


@dataclass(frozen=True)
class ReferenceDocument:
    title: str
    relative_path: str
    text: str
    content_sha256: str


@dataclass(frozen=True)
class ReferenceHit:
    title: str
    relative_path: str
    snippet: str
    score: int
    content_sha256: str


class ReferenceLibrary:
    """A workspace-bound reader which cannot escape the Knowledge directory."""

    TEXT_SUFFIXES = {".csv", ".html", ".htm", ".json", ".md", ".txt"}
    DOCUMENT_SUFFIXES = {".docx"}
    MAX_DOCUMENT_CHARS = 2_000_000

    def __init__(self, workspace: Path):
        self.workspace = Path(workspace).resolve()
        self.root = (self.workspace / "Knowledge").resolve()

    def _safe_path(self, value: str | Path) -> Path:
        candidate = Path(value)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        resolved = candidate.resolve()
        if not resolved.is_relative_to(self.root):
            raise ValueError("reference path must stay beneath project Knowledge")
        return resolved

    def _read_text(self, path: Path) -> str:
        suffix = path.suffix.casefold()
        if suffix in self.TEXT_SUFFIXES:
            text = path.read_text(encoding="utf-8", errors="ignore")
            if suffix in {".html", ".htm"}:
                text = re.sub(r"<[^>]+>", " ", text)
            return text[: self.MAX_DOCUMENT_CHARS]
        if suffix == ".docx":
            document = Document(path)
            text = "\n".join(paragraph.text for paragraph in document.paragraphs)
            return text[: self.MAX_DOCUMENT_CHARS]
        raise ValueError(f"unsupported reference file type: {suffix or '<none>'}")

    def open(self, relative_path: str) -> ReferenceDocument:
        path = self._safe_path(relative_path)
        if not path.is_file():
            raise ValueError("reference path is not a file in project Knowledge")
        text = self._read_text(path)
        return ReferenceDocument(
            title=path.stem,
            relative_path=path.relative_to(self.workspace).as_posix(),
            text=text,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )

    def search(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms or limit <= 0 or not self.root.is_dir():
            return []

        hits: list[ReferenceHit] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            try:
                safe_path = self._safe_path(path)
            except ValueError:
                # This blocks symlinks from Knowledge into any external directory.
                continue
            if safe_path.suffix.casefold() not in self.TEXT_SUFFIXES | self.DOCUMENT_SUFFIXES:
                continue
            try:
                document = self.open(safe_path.relative_to(self.workspace).as_posix())
            except (OSError, ValueError):
                continue
            lowered = document.text.casefold()
            score = sum(lowered.count(term) for term in terms)
            if not score:
                continue
            first = min(lowered.find(term) for term in terms if term in lowered)
            start = max(0, first - 160)
            snippet = " ".join(document.text[start : first + 440].split())
            hits.append(
                ReferenceHit(
                    title=document.title,
                    relative_path=document.relative_path,
                    snippet=snippet,
                    score=score,
                    content_sha256=document.content_sha256,
                )
            )
        return sorted(hits, key=lambda hit: (-hit.score, hit.relative_path))[:limit]
