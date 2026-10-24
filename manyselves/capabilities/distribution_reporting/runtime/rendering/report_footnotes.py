"""Citation footnotes shared by the delivered Markdown and DOCX.

The claim ledger binds each cited claim to a numeric ``[[CITE:n]]`` token.
Delivery turns those tokens into Markdown footnotes (``[^n]`` in the text and
one ``[^n]: …`` definition per citation at the end).  The DOCX renderer reads
the same Markdown, so a Word footnote and its Markdown counterpart always carry
the same number and the same source text.
"""

from __future__ import annotations

import re

from manyselves.capabilities.distribution_reporting.domain.claim_ledger import ClaimLedger

CITATION_TOKEN = re.compile(r"\[\[CITE:(\d+)\]\]")
FOOTNOTE_REFERENCE = re.compile(r"\[\^(\d+)\]")
_FOOTNOTE_DEFINITION = re.compile(r"^\[\^(\d+)\]:\s*(.*)$")
_CITATION_ONLY_LINE = re.compile(r"^(?:\s*\[\[CITE:\d+\]\])+\s*$")


def citation_footnote_texts(ledger: ClaimLedger) -> dict[int, str]:
    """Describe the sources behind each citation marker of the ledger plan."""

    sources = {source.id: source for source in ledger.sources}
    texts: dict[int, str] = {}
    for entry in ledger.build_citation_plan().entries:
        parts = []
        for source_id in entry.source_ids:
            source = sources.get(source_id)
            if source is None:
                parts.append(source_id)
                continue
            parts.append(f"{source.id} {_single_line(source.title)}（{_single_line(source.locator)}）")
        texts[entry.marker] = "；".join(parts)
    return texts


def markdown_with_footnotes(markdown: str, footnote_texts: dict[int, str]) -> str:
    """Replace ``[[CITE:n]]`` tokens with Markdown footnotes.

    A citation written on its own line follows the paragraph it cites, so it
    joins that paragraph's last line unless that line is a heading or a table
    row.  Markdown without citation tokens is returned unchanged.
    """

    if not CITATION_TOKEN.search(markdown):
        return markdown
    lines: list[str] = []
    for line in markdown.splitlines():
        if _CITATION_ONLY_LINE.match(line):
            references = CITATION_TOKEN.sub(r"[^\1]", line.strip()).replace(" ", "")
            previous = _previous_text_line(lines)
            if previous is not None and _joinable(lines[previous]):
                del lines[previous + 1 :]
                lines[previous] = lines[previous].rstrip() + references
                continue
            lines.append(references)
            continue
        lines.append(CITATION_TOKEN.sub(r"[^\1]", line))
    body = "\n".join(lines).rstrip()
    # Word numbers footnotes by position; number the Markdown the same way.
    order: dict[int, int] = {}
    for number in FOOTNOTE_REFERENCE.findall(body):
        order.setdefault(int(number), len(order) + 1)
    body = FOOTNOTE_REFERENCE.sub(lambda m: f"[^{order[int(m.group(1))]}]", body)
    definitions = [
        f"[^{position}]: {footnote_texts.get(marker) or f'来源{marker}'}"
        for marker, position in order.items()
    ]
    return body + "\n\n" + "\n".join(definitions) + "\n"


def split_footnote_definitions(markdown: str) -> tuple[str, dict[int, str]]:
    """Separate ``[^n]: text`` definition lines from the report body."""

    body: list[str] = []
    definitions: dict[int, str] = {}
    for line in markdown.splitlines():
        match = _FOOTNOTE_DEFINITION.match(line.strip())
        if match:
            definitions[int(match.group(1))] = match.group(2).strip()
            continue
        body.append(line)
    return "\n".join(body).rstrip() + "\n", definitions


def _previous_text_line(lines: list[str]) -> int | None:
    for index in range(len(lines) - 1, -1, -1):
        if lines[index].strip():
            return index
    return None


def _joinable(line: str) -> bool:
    stripped = line.strip()
    return not (stripped.startswith("#") or stripped.startswith("|") or stripped.endswith("|"))


def _single_line(value: object) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


__all__ = [
    "CITATION_TOKEN",
    "FOOTNOTE_REFERENCE",
    "citation_footnote_texts",
    "markdown_with_footnotes",
    "split_footnote_definitions",
]
