"""Compare a generated report with the expert report, section by section.

Reads two DOCX files, counts Chinese characters per numbered section (prose
and tables separately), table rows and images, and lists the sentences the
generated report repeats most.  No model calls; nothing is written except the
optional Markdown output.

    .venv/bin/python scripts/maintenance/compare_report_length.py \
        --report path/to/generated.docx --expert path/to/expert.docx \
        --depth 2 --out length-compare.md
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from pathlib import Path

from manyselves.runtime.exemplar.outline import DocumentOutline, document_outline

_CJK = re.compile(r"[㐀-鿿]")
_SPLIT = re.compile(r"[。；;！!？?\n]")
_DIGITS = re.compile(r"[0-9０-９.．%]+")


def _cjk(text: str) -> int:
    return len(_CJK.findall(text))


def _stats(outline: DocumentOutline, depth: int) -> dict[str, dict]:
    """Totals per section at ``depth`` or above, each including its subsections."""

    out: dict[str, dict] = {}
    for section in outline.sections:
        key = ".".join(section.number.split(".")[:depth])
        row = out.setdefault(
            key, {"title": "", "prose": 0, "table": 0, "rows": 0, "tables": 0, "images": 0, "paras": 0}
        )
        if section.number == key:
            row["title"] = section.title
        for locator in section.blocks:
            block = outline.block(locator)
            if block is None:
                continue
            if block.kind == "table":
                row["table"] += _cjk(block.text)
                row["rows"] += block.rows
                row["tables"] += 1
            else:
                row["prose"] += _cjk(block.text)
                row["paras"] += 1
            row["images"] += block.images
    return out


def _repeats(outline: DocumentOutline, limit: int) -> list[tuple[str, int, int]]:
    """Clauses that occur three or more times, with digits masked, by characters spent."""

    counts: Counter[str] = Counter()
    for block in outline.blocks:
        for clause in _SPLIT.split(block.text):
            clause = _DIGITS.sub("#", clause.strip(" ，,|"))
            if _cjk(clause) >= 4:
                counts[clause] += 1
    rows = [(text, n, n * _cjk(text)) for text, n in counts.items() if n >= 3]
    return sorted(rows, key=lambda r: -r[2])[:limit]


def _key(number: str) -> tuple[int, ...]:
    return tuple(int(part) for part in number.split(".") if part.isdigit())


def render(report: Path, expert: Path, depth: int, limit: int) -> str:
    gen, exp = document_outline(report), document_outline(expert)
    a, b = _stats(gen, depth), _stats(exp, depth)
    lines = [
        "# 成稿与专家版按小节篇幅对比",
        "",
        f"- 成稿：{report.name}",
        f"- 专家版：{expert.name}",
        "- 字数只数汉字；表格字数含表头。小节按编号对齐，标题不同时两个都列出。",
        "",
        "| 小节 | 成稿标题 | 专家版标题 | 成稿正文 | 成稿表格 | 专家正文 | 专家表格 | 倍数 | 成稿表行/图 | 专家表行/图 |",
        "| --- | --- | --- | ---: | ---: | ---: | ---: | ---: | --- | --- |",
    ]
    for number in sorted(set(a) | set(b), key=_key):
        g = a.get(number, {})
        e = b.get(number, {})
        gsum = g.get("prose", 0) + g.get("table", 0)
        esum = e.get("prose", 0) + e.get("table", 0)
        ratio = f"{gsum / esum:.2f}" if esum else "—"
        lines.append(
            f"| {number} | {g.get('title', '')} | {e.get('title', '')} | {g.get('prose', 0)} | "
            f"{g.get('table', 0)} | {e.get('prose', 0)} | {e.get('table', 0)} | {ratio} | "
            f"{g.get('rows', 0)}/{g.get('images', 0)} | {e.get('rows', 0)}/{e.get('images', 0)} |"
        )
    total = {"gp": 0, "gt": 0, "ep": 0, "et": 0}
    for row in _stats(gen, 1).values():
        total["gp"] += row["prose"]
        total["gt"] += row["table"]
    for row in _stats(exp, 1).values():
        total["ep"] += row["prose"]
        total["et"] += row["table"]
    gall, eall = total["gp"] + total["gt"], total["ep"] + total["et"]
    lines += [
        "",
        f"合计：成稿 {gall}（正文 {total['gp']}，表格 {total['gt']}）；"
        f"专家版 {eall}（正文 {total['ep']}，表格 {total['et']}）；"
        f"倍数 {gall / eall:.2f}" if eall else "合计：专家版没有可数的字",
        "",
        "## 成稿里重复最多的句子（数字已替换为 #，按占用字数排序）",
        "",
        "| 句子 | 次数 | 占用汉字 |",
        "| --- | ---: | ---: |",
    ]
    for text, n, spent in _repeats(gen, limit):
        lines.append(f"| {text.replace('|', '／')} | {n} | {spent} |")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--report", type=Path, required=True, help="generated report DOCX")
    parser.add_argument("--expert", type=Path, required=True, help="expert report DOCX")
    parser.add_argument("--depth", type=int, default=2, help="section depth to group by (default 2)")
    parser.add_argument("--repeats", type=int, default=20, help="repeated sentences to list")
    parser.add_argument("--out", type=Path, help="write Markdown here instead of stdout")
    args = parser.parse_args(argv)
    for path in (args.report, args.expert):
        if not path.is_file():
            parser.error(f"not a file: {path}")
    text = render(args.report, args.expert, args.depth, args.repeats)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
