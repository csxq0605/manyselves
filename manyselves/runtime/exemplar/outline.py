"""A deterministic outline of an exemplar document.

The outline is what a program can say about a finished exemplar without a
model: its heading tree (numbered by position, empty headings skipped), the
blocks under each heading with a stable locator, the tables and their column
names, images, list items, and the short label paragraphs that open each part
of a section (for example "结论："). Nothing here knows what the document is
about; a capability decides which sections matter for which role.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path

from docx import Document
from docx.oxml.ns import qn
from docx.table import Table
from docx.text.paragraph import Paragraph

_CJK = re.compile(r"[一-鿿]")
_HEADING = re.compile(r"^(?:Heading|标题)\s*(\d)$", re.IGNORECASE)
_COLONS = ("：", ":")


@dataclass(frozen=True)
class OutlineBlock:
    """One paragraph or table, addressed by its position in the document body."""

    locator: str
    kind: str  # paragraph | table
    text: str
    section: str | None
    style: str = ""
    label: str = ""
    list_item: bool = False
    images: int = 0
    columns: tuple[str, ...] = ()
    rows: int = 0


@dataclass(frozen=True)
class OutlineSection:
    number: str
    title: str
    level: int
    locator: str
    blocks: tuple[str, ...] = ()
    chars: int = 0
    tables: int = 0
    images: int = 0
    labels: tuple[str, ...] = ()


@dataclass(frozen=True)
class DocumentOutline:
    sections: tuple[OutlineSection, ...]
    blocks: tuple[OutlineBlock, ...]
    front_matter: tuple[str, ...] = field(default=())

    def section(self, number: str) -> OutlineSection | None:
        return next((s for s in self.sections if s.number == number), None)

    def block(self, locator: str) -> OutlineBlock | None:
        return self._by_locator().get(locator)

    def _by_locator(self) -> dict[str, OutlineBlock]:
        cache = getattr(self, "_locator_cache", None)
        if cache is None:
            cache = {block.locator: block for block in self.blocks}
            object.__setattr__(self, "_locator_cache", cache)
        return cache

    def subtree(self, number: str) -> list[OutlineSection]:
        """The section and every section numbered under it, in document order."""

        return [s for s in self.sections if s.number == number or s.number.startswith(number + ".")]

    def section_numbers(self) -> set[str]:
        return {s.number for s in self.sections}


def _level(paragraph: Paragraph) -> int | None:
    match = _HEADING.match(paragraph.style.name if paragraph.style is not None else "")
    return int(match.group(1)) if match else None


def _label(text: str) -> str:
    """The lead of a paragraph that names a part of the section, e.g. 结论.

    A label is the text before the first colon when it is plain Chinese words
    (no digits, no sentence punctuation): a whole label paragraph ("建议：")
    or a lead-in ("结论：……").
    """

    for colon in _COLONS:
        head, found, _rest = text.partition(colon)
        head = head.strip()
        if found and head and len(_CJK.findall(head)) == len(head):
            return head
    return ""


def _table_text(table: Table) -> tuple[str, tuple[str, ...], int, int]:
    """Rows as " | "-joined cells, the header, the row count and its Chinese characters.

    A merged cell is shown once per row and counted once per table.
    """

    lines: list[str] = []
    # Keyed by the cell element itself: holding it keeps one proxy per cell,
    # so a merged cell is recognised however often python-docx yields it.
    unique: dict[object, str] = {}
    header: tuple[str, ...] = ()
    for index, row in enumerate(table.rows):
        cells: list[str] = []
        seen: set[object] = set()
        for cell in row.cells:
            if cell._tc in seen:
                continue
            seen.add(cell._tc)
            unique.setdefault(cell._tc, cell.text)
            cells.append(" ".join(cell.text.split()))
        if index == 0:
            header = tuple(cells)
        lines.append(" | ".join(cells))
    chars = sum(len(_CJK.findall(text)) for text in unique.values())
    return "\n".join(lines), header, len(table.rows), chars


def _images(element) -> int:
    return len(element.findall(".//" + qn("w:drawing"))) + len(element.findall(".//" + qn("w:pict")))


def document_outline(path: Path) -> DocumentOutline:
    """Outline one DOCX: numbered headings, located blocks and section statistics."""

    document = Document(str(path))
    counters: list[int] = []
    sections: list[dict] = []
    blocks: list[OutlineBlock] = []
    front: list[str] = []
    for index, element in enumerate(document.element.body.iterchildren()):
        current = sections[-1] if sections else None
        if element.tag == qn("w:p"):
            paragraph = Paragraph(element, document)
            text = paragraph.text.strip()
            level = _level(paragraph)
            style = paragraph.style.name if paragraph.style is not None else ""
            if level is not None:
                if not text:
                    continue
                counters = counters[:level] + [0] * max(0, level - len(counters))
                counters[level - 1] += 1
                sections.append(
                    {
                        "number": ".".join(map(str, counters)),
                        "title": text,
                        "level": level,
                        "locator": f"p-{index}",
                        "blocks": [],
                        "chars": 0,
                        "tables": 0,
                        "images": 0,
                        "labels": [],
                    }
                )
                continue
            images = _images(element)
            if not text and not images:
                continue
            if current is None:
                if text and not style.lower().startswith("toc"):
                    front.append(text)
                continue
            list_item = "list" in style.lower()
            label = "" if list_item else _label(text)
            block = OutlineBlock(
                locator=f"p-{index}",
                kind="paragraph",
                text=text,
                section=current["number"],
                style=style,
                label=label,
                list_item=list_item,
                images=images,
            )
            blocks.append(block)
            current["blocks"].append(block.locator)
            current["chars"] += len(_CJK.findall(text))
            current["images"] += images
            if label:
                current["labels"].append(label)
        elif element.tag == qn("w:tbl") and current is not None:
            table = Table(element, document)
            text, header, rows, chars = _table_text(table)
            block = OutlineBlock(
                locator=f"t-{index}",
                kind="table",
                text=text,
                section=current["number"],
                images=_images(element),
                columns=header,
                rows=rows,
            )
            blocks.append(block)
            current["blocks"].append(block.locator)
            current["chars"] += chars
            current["tables"] += 1
            current["images"] += block.images
    return DocumentOutline(
        sections=tuple(
            OutlineSection(
                number=s["number"],
                title=s["title"],
                level=s["level"],
                locator=s["locator"],
                blocks=tuple(s["blocks"]),
                chars=s["chars"],
                tables=s["tables"],
                images=s["images"],
                labels=tuple(s["labels"]),
            )
            for s in sections
        ),
        blocks=tuple(blocks),
        front_matter=tuple(front),
    )


def similar(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def resolve_section(outline: DocumentOutline, number: str, title: str = "") -> OutlineSection | None:
    """Find a declared section by number, checked by title when one is given.

    A number whose title is unlike the declared one falls back to the most
    similar title; nothing is returned without a reasonable match.
    """

    candidate = outline.section(number)
    if candidate is not None and (not title or similar(candidate.title, title) >= 0.5):
        return candidate
    if not title:
        return None
    best = max(outline.sections, key=lambda s: similar(s.title, title), default=None)
    return best if best is not None and similar(best.title, title) >= 0.6 else None


def section_blocks(outline: DocumentOutline, numbers: Iterable[str]) -> list[OutlineBlock]:
    wanted = set(numbers)
    return [block for block in outline.blocks if block.section in wanted]


def label_sequence(labels: Sequence[str]) -> list[str]:
    """Labels in order of first use, e.g. 现状描述 → 风险分析 → 建议."""

    return list(dict.fromkeys(labels))


__all__ = [
    "DocumentOutline",
    "OutlineBlock",
    "OutlineSection",
    "document_outline",
    "label_sequence",
    "resolve_section",
    "section_blocks",
    "similar",
]
