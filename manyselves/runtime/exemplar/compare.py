"""Compare a hand-kept Skill with a newly extracted one, item by item.

Before a newly extracted Skill replaces one people have been revising, each
item of the old one is matched against the new entries and against the rules
the capability already states elsewhere (its task contracts).  The match is a
character-bigram overlap, so it only sorts items for a person to read: an item
with no close match is listed for that person to decide whether it is a
lesson, a rule to add, or something to drop.  Nothing here knows the subject.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass

_FRONT = re.compile(r"\A---\n.*?\n---\n", re.S)
_LOCATOR = re.compile(r"\s*〔[^〕]*〕\s*$")
_BULLET = re.compile(r"^\s*(?:[-*+]|\d+[.、)])\s+")
_DROP = re.compile(r"[\s\W_]+", re.U)
MIN_ITEM_CHARS = 10


@dataclass(frozen=True)
class Match:
    item: str
    heading: str
    best: str
    best_score: float
    best_source: str
    rule: str
    rule_score: float
    rule_source: str


def skill_items(text: str) -> list[tuple[str, str]]:
    """(heading, item) for every list item or paragraph of a Markdown Skill.

    Headings, table rows, front matter and short lines are not items; a
    trailing source locator is dropped.
    """

    body = _FRONT.sub("", text)
    heading = ""
    items: list[tuple[str, str]] = []
    for raw in body.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#"):
            heading = line.lstrip("#").strip()
            continue
        if line.startswith("|"):
            continue
        line = _LOCATOR.sub("", _BULLET.sub("", line)).strip()
        if len(_normal(line)) >= MIN_ITEM_CHARS:
            items.append((heading, line))
    return items


def _normal(text: str) -> str:
    return _DROP.sub("", text)


def _bigrams(text: str) -> set[str]:
    value = _normal(text)
    return {value[i : i + 2] for i in range(len(value) - 1)} or ({value} if value else set())


def overlap(left: str, right: str) -> float:
    """How much of ``left`` appears in ``right``, by character bigrams (0 to 1)."""

    a, b = _bigrams(left), _bigrams(right)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a)


def _best(item: str, pool: Sequence[tuple[str, str]]) -> tuple[str, float, str]:
    best = ("", 0.0, "")
    for source, text in pool:
        score = overlap(item, text)
        if score > best[1]:
            best = (text, score, source)
    return best


def compare_items(
    old: Iterable[tuple[str, str]],
    new: Sequence[tuple[str, str]],
    rules: Sequence[tuple[str, str]],
) -> list[Match]:
    """For each old item, its closest new entry and its closest stated rule."""

    matches = []
    for heading, item in old:
        best, score, source = _best(item, new)
        rule, rule_score, rule_source = _best(item, rules)
        matches.append(Match(item, heading, best, score, source, rule, rule_score, rule_source))
    return matches


def classify(match: Match, *, threshold: float) -> str:
    if match.best_score >= threshold and match.best_score >= match.rule_score:
        return "新版有对应"
    if match.rule_score >= threshold:
        return "合同已有"
    if match.best_score >= threshold:
        return "新版有对应"
    return "未对应"


def render_comparison(
    by_role: Mapping[str, list[Match]], *, threshold: float
) -> tuple[str, dict[str, dict[str, int]]]:
    """A Markdown table per role, open items first, and the counts."""

    counts: dict[str, dict[str, int]] = {}
    lines = [
        "# 现用版与新提炼版逐条对照",
        "",
        f"按字面重合度（双字重合比例，阈值 {threshold:.2f}）给现用版每一条找最接近的新条目和任务合同条文，"
        "只用来排序给人看，不代表意思相同。“未对应”的条目要人决定：作为教训候选、补进合同，还是舍弃。",
    ]
    for role, matches in by_role.items():
        status = [(classify(m, threshold=threshold), m) for m in matches]
        tally: dict[str, int] = {}
        for label, _match in status:
            tally[label] = tally.get(label, 0) + 1
        counts[role] = tally
        lines.extend(
            [
                "",
                f"## {role}",
                "；".join(f"{label} {count}" for label, count in sorted(tally.items())),
                "",
                "| 结果 | 现用版条目 | 最接近的内容 | 重合 |",
                "| --- | --- | --- | --- |",
            ]
        )
        order = {"未对应": 0, "合同已有": 1, "新版有对应": 2}
        for label, match in sorted(status, key=lambda pair: order[pair[0]]):
            if label == "合同已有":
                near, score, source = match.rule, match.rule_score, match.rule_source
            else:
                near, score, source = match.best, match.best_score, match.best_source
            item = f"〔{match.heading}〕{match.item}" if match.heading else match.item
            lines.append(
                f"| {label} | {_cell(item)} | {_cell(f'{source}：{near}' if near else '')} | {score:.2f} |"
            )
    return "\n".join(lines) + "\n", counts


def _cell(text: str) -> str:
    return text.replace("|", "／").replace("\n", " ")


__all__ = [
    "Match",
    "classify",
    "compare_items",
    "overlap",
    "render_comparison",
    "skill_items",
]
