"""Search deterministic project and optional global knowledge references."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from docx import Document

KnowledgeNamespace = Literal["project", "global"]


@dataclass(frozen=True)
class ReferenceDocument:
    title: str
    relative_path: str
    text: str
    content_sha256: str
    namespace: KnowledgeNamespace = "project"


@dataclass(frozen=True)
class ReferenceHit:
    title: str
    relative_path: str
    snippet: str
    score: int
    content_sha256: str
    namespace: KnowledgeNamespace = "project"


@dataclass(frozen=True)
class _ReferenceRoot:
    namespace: KnowledgeNamespace
    logical_prefix: str
    path: Path


class ReferenceLibrary:
    """Read logical knowledge references without exposing either physical root."""

    TEXT_SUFFIXES = {".csv", ".html", ".htm", ".json", ".md", ".txt"}
    DOCUMENT_SUFFIXES = {".docx"}
    MAX_DOCUMENT_CHARS = 2_000_000

    def __init__(self, workspace: Path, *, global_root: Path | None = None):
        self.workspace = Path(workspace).resolve()
        project_input = self.workspace / "Knowledge"
        if project_input.is_symlink():
            raise ValueError("project Knowledge root must not be a symlink")
        self.root = project_input.resolve()
        roots = [_ReferenceRoot("project", "Knowledge", self.root)]
        self.global_root: Path | None = None
        if global_root is not None:
            global_input = Path(global_root)
            if global_input.is_symlink():
                raise ValueError("GlobalKnowledge root must not be a symlink")
            self.global_root = global_input.resolve()
            roots.append(_ReferenceRoot("global", "GlobalKnowledge", self.global_root))
        self._roots = tuple(roots)

    def _root_for(self, logical_path: str) -> tuple[_ReferenceRoot, tuple[str, ...]]:
        if not isinstance(logical_path, str) or not logical_path or "\x00" in logical_path:
            raise ValueError("reference path must use Knowledge or GlobalKnowledge")
        if "\\" in logical_path:
            raise ValueError("reference path must use Knowledge or GlobalKnowledge")
        portable = PurePosixPath(logical_path)
        parts = portable.parts
        if portable.is_absolute() or len(parts) < 2 or any(part in {"", ".", ".."} for part in parts):
            raise ValueError("reference path must use Knowledge or GlobalKnowledge")
        for root in self._roots:
            if parts[0] == root.logical_prefix:
                return root, parts[1:]
        raise ValueError("reference path must use Knowledge or GlobalKnowledge")

    def _safe_path(self, logical_path: str) -> tuple[_ReferenceRoot, Path]:
        root, parts = self._root_for(logical_path)
        resolved = root.path.joinpath(*parts).resolve()
        if not resolved.is_relative_to(root.path):
            raise ValueError(f"reference path must stay beneath {root.logical_prefix}")
        return root, resolved

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
        root, path = self._safe_path(relative_path)
        if not path.is_file():
            raise ValueError(f"reference path is not a file in {root.logical_prefix}")
        text = self._read_text(path)
        return ReferenceDocument(
            title=path.stem,
            relative_path=f"{root.logical_prefix}/{path.relative_to(root.path).as_posix()}",
            text=text,
            content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
            namespace=root.namespace,
        )

    def documents(self) -> tuple[ReferenceDocument, ...]:
        """Return safe, de-duplicated documents in project-before-global order."""
        supported = self.TEXT_SUFFIXES | self.DOCUMENT_SUFFIXES
        seen_subpaths: set[str] = set()
        seen_content: set[str] = set()
        documents: list[ReferenceDocument] = []
        for root in self._roots:
            if not root.path.is_dir():
                continue
            for path in sorted(root.path.rglob("*")):
                if not path.is_file() or path.suffix.casefold() not in supported:
                    continue
                try:
                    subpath = path.relative_to(root.path).as_posix()
                    path_key = subpath.casefold()
                    if path_key in seen_subpaths:
                        continue
                    document = self.open(f"{root.logical_prefix}/{subpath}")
                except (OSError, ValueError):
                    continue
                if document.content_sha256 in seen_content:
                    continue
                seen_subpaths.add(path_key)
                seen_content.add(document.content_sha256)
                documents.append(document)
        return tuple(documents)

    def search(self, query: str, limit: int = 5) -> list[ReferenceHit]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms or limit <= 0:
            return []

        hits: list[ReferenceHit] = []
        for document in self.documents():
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
                    namespace=document.namespace,
                )
            )
        priority = {"project": 0, "global": 1}
        return sorted(
            hits,
            key=lambda hit: (
                -hit.score,
                priority[hit.namespace],
                hit.relative_path.casefold(),
                hit.relative_path,
            ),
        )[:limit]
