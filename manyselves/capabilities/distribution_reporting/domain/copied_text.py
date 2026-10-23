"""Text copied word for word from the instructions into what the reader sees.

The expert method's writing parts (how a section is written, how chapters
are drawn together, what an editor checks), the knowledge base's relation
method and the task requirements are written for the authors.  (Its judging
method is reasoning a row may follow, and its examples are patterns meant to
be reused; neither is compared.)  A run of their characters reaching the
report or a field the report prints ("再以条件句提出…") is an instruction left
in the prose.  The check needs no list of words: it
compares the text with the instruction texts themselves and reports every
stretch of ``MIN_CHARS`` or more characters they share.

A stretch that the run's own records or knowledge units also contain (an
object's name, a standard's title, a recorded phrase) is the project's
material and passes.  Whitespace is ignored on both sides, and value slots are
taken out first.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence

from .value_slots import strip_slots

MIN_CHARS = 12
_SPACE = re.compile(r"\s+")
# Source markers of the extracted method (〔模板 p-170〕) and Markdown marks are not text.
_MARKER = re.compile(r"〔[^〔〕]*〕|[#*|]")


def _plain(text: str) -> str:
    return _SPACE.sub("", _MARKER.sub("", strip_slots(str(text))))


def skill_parts(text: str, titles: Iterable[str]) -> str:
    """The parts of a Skill under the given use titles (lines ``写法：``), headings dropped.

    A Skill is rendered per section, then per use: a line holding only the
    use's title and a colon opens its part, which runs to the next such line
    or section heading.
    """

    wanted = set(titles)
    kept: list[str] = []
    keep = False
    for line in str(text).splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            keep = False
            continue
        header = re.fullmatch(r"(\S{1,12})[：:]\s*", stripped)
        if header:
            keep = header.group(1) in wanted
            continue
        if keep:
            kept.append(line)
    return "\n".join(kept)


def _grams(texts: Iterable[str], size: int) -> set[str]:
    grams: set[str] = set()
    for text in texts:
        plain = _plain(text)
        grams.update(plain[i : i + size] for i in range(len(plain) - size + 1))
    return grams


class CopiedText:
    """Instruction texts to compare with, and the project's own texts that pass."""

    def __init__(
        self,
        instructions: Iterable[str],
        allowed: Iterable[str] = (),
        *,
        min_chars: int = MIN_CHARS,
    ) -> None:
        self.size = min_chars
        self.instructions = _grams(instructions, min_chars)
        self.allowed = _grams(allowed, min_chars)

    def spans(self, text: str) -> list[str]:
        """The stretches of ``text`` that the instructions contain, longest form, each once."""

        if not self.instructions:
            return []
        plain = _plain(text)
        size = self.size
        hits = [
            i
            for i in range(len(plain) - size + 1)
            if (gram := plain[i : i + size]) in self.instructions and gram not in self.allowed
        ]
        spans: list[str] = []
        start = end = None
        for i in hits:
            if end is not None and i <= end:
                end = i + size
                continue
            if start is not None:
                spans.append(plain[start:end])
            start, end = i, i + size
        if start is not None:
            spans.append(plain[start:end])
        return list(dict.fromkeys(spans))

    def findings(self, texts: Mapping[str, str]) -> dict[str, list[str]]:
        """Key -> the copied stretches of its text, for the keys that have any."""

        return {key: found for key, text in texts.items() if (found := self.spans(text))}


def copied_problem(spans: Sequence[str]) -> str:
    return (
        "以下文字与专家版方法、关联方法或任务要求逐字相同（连续 "
        f"{MIN_CHARS} 字以上），是写给撰稿人的说明，不能进报告："
        + "、".join(f"“{span}”" for span in spans)
    )


COPIED_SUGGESTION = "用本项目的事实和自己的话重写这几处；写作说明（怎么写、可以写成什么）不写进正文。"


__all__ = ["COPIED_SUGGESTION", "CopiedText", "MIN_CHARS", "copied_problem", "skill_parts"]
