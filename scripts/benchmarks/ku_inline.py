"""Which module KUs reach the agent inline under the 14k target (Q07).

Run against a project copy made by ``prepare_reporting_references.py``.  For
each module 2.1-2.5 it counts the module's own KUs present as whole units in
``build_module`` output, and repeats with the inline target lowered by each
``--shrink`` value (equivalent to the library growing by that many chars).
Only counts and IDs are written; no source text.  Provider calls: 0.

    uv run python scripts/benchmarks/ku_inline.py --project PREPARED \\
        [--shrink 0,89,1000] [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.research.knowledge_context import (
    KnowledgeContextBuilder,
)

METHODOLOGY = "Knowledge/配电运维专家知识库/01_评估方法论与风险判断.md"
MODULES = ("2.1", "2.2", "2.3", "2.4", "2.5")


def units(project: Path) -> dict[str, str]:
    text = (project / METHODOLOGY).read_text(encoding="utf-8-sig")
    pattern = r"(?ms)^### (KU-[\d.]+-\d+)\s.*?(?=^#{1,3} |\Z)"
    return {match[1]: match[0].strip() for match in re.finditer(pattern, text)}


def measure(project: Path, shrink: int) -> dict:
    class Builder(KnowledgeContextBuilder):
        MAX_MODULE_CHARS = KnowledgeContextBuilder.MAX_MODULE_CHARS - shrink

    known = units(project)
    result = {}
    for module in MODULES:
        text = Builder(project, f"ku-inline-{shrink}").build_module(module).text
        own = [unit for unit in known if unit.startswith(f"KU-{module}-")]
        entries = re.split(r"(?m)^### (?=R-)", text)[1:]
        result[module] = {
            "chars": len(text),
            "own_whole": sum(known[unit] in text for unit in own),
            "own_total": len(own),
            "missing": [unit for unit in own if known[unit] not in text],
            "sources": {
                entry.splitlines()[0]: (
                    "full" if re.search(r"完整(原文|已提取文本)（", entry)
                    else "partial" if "其余原文未展开" in entry else "unexpanded"
                )
                for entry in entries
            },
        }
    return result


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--shrink", default="0,89,1000,2000,4000")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    payload = {
        "inline_target": KnowledgeContextBuilder.MAX_MODULE_CHARS,
        "runs": {shrink: measure(args.project, int(shrink)) for shrink in args.shrink.split(",")},
        "provider_calls": 0,
    }
    for shrink, modules in payload["runs"].items():
        print(shrink, {m: f"{v['own_whole']}/{v['own_total']} {v['chars']}" for m, v in modules.items()})
    if args.output:
        args.output.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
