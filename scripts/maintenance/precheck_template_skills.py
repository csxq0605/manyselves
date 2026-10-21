"""Precheck the new Skill acceptance and the standards lookup on real files, without a model.

1. Re-checks the entries of an earlier extraction run under the current
   acceptance rules and counts refusals by kind.  Refusals for a site word
   alone (工厂、厂区…) are listed with every example, so a person can see
   whether they are ordinary wording or the expert report's own places.
2. Reads the knowledge sources and reports how many knowledge units carry
   a standards field the writer would receive.

    .venv/bin/python scripts/maintenance/precheck_template_skills.py \
        --workspace /path/to/project --run <extraction run id> \
        --knowledge /path/to/project/Knowledge --out precheck.md

Nothing is written except the optional Markdown output.
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.rule_standards import knowledge_units
from manyselves.capabilities.distribution_reporting.runtime.template_skills import (
    run_root,
    skill_targets,
    template_policy,
    template_roles,
)
from manyselves.runtime.exemplar.extraction import (
    ExemplarEntry,
    ExemplarSubmission,
    accept_submission,
    instance_terms,
    role_scope,
    site_words,
)
from manyselves.runtime.exemplar.outline import document_outline

KINDS = (
    ("专家版项目的实际", "实例词"),
    ("场地划分", "场地词"),
    ("数值或编号", "数值"),
    ("强制词", "强制词"),
    ("单位名称", "单位名称"),
    ("找不到", "原文对不上"),
    ("不在本角色", "坐标越界"),
)


def _kind(reason: str) -> str:
    return next((name for key, name in KINDS if key in reason), "其他")


def precheck(workspace: Path, run_id: str, knowledge: Path | None) -> str:
    root = workspace / run_root(run_id)
    outline = document_outline(root / "template.docx")
    policy = template_policy()
    lines = [f"# Skill 预检（提炼运行 {run_id}，不调用模型）", ""]
    totals: Counter[str] = Counter()
    site_only: list[str] = []
    for role in template_roles():
        path = root / role.id / "entries.json"
        if not role.extract or not path.is_file():
            continue
        record = json.loads(path.read_text(encoding="utf-8"))
        entries = [ExemplarEntry.model_validate(e) for e in record.get("accepted", [])]
        scope = role_scope(outline, role, skill_targets())
        result = accept_submission(outline, scope, ExemplarSubmission(entries=entries), policy)
        kinds: Counter[str] = Counter()
        for rejection in result.rejected:
            found = {_kind(reason) for reason in rejection.reasons}
            kinds.update(found)
            if found == {"场地词"}:
                site_only.append(f"- {role.id} {rejection.entry.locator}：{rejection.entry.text}")
        totals.update(kinds)
        folded = ", ".join(scope.folded) or "无"
        lines += [
            f"## {role.id}",
            f"- 原先通过 {len(entries)} 条，新规则下仍通过 {len(result.accepted)} 条，拒收 {len(result.rejected)} 条",
            "- 拒收原因：" + ("、".join(f"{k} {n}" for k, n in kinds.most_common()) or "无"),
            f"- 并入上级的专家版小节：{folded}",
            "- 对齐：" + "；".join(
                f"{s.number}→{scope.target(s).number}" for s in scope.sections
                if scope.target(s).number != s.number
            ),
            f"- 实例词 {len(instance_terms(outline, scope, policy))} 个，场地词：" + "、".join(site_words(scope, policy)),
            "",
        ]
        for rejection in result.rejected[:5]:
            lines.append(f"  - {rejection.entry.text} —— " + "；".join(rejection.reasons))
        lines.append("")
    lines += ["## 合计", "- " + ("、".join(f"{k} {n}" for k, n in totals.most_common()) or "无拒收"), ""]
    lines += ["## 只因场地词被拒的条目（逐条看是否误伤）", *(site_only or ["- 无"]), ""]
    if knowledge is not None:
        texts = [p.read_text(encoding="utf-8-sig") for p in sorted(knowledge.rglob("*.md"))]
        units = knowledge_units(texts)
        with_standards = {k: v for k, v in units.items() if v["standards"]}
        lines += [
            "## 知识单元的标准字段",
            f"- 知识单元 {len(units)} 个，带标准字段 {len(with_standards)} 个",
            *(f"- {k} {v['title']}：{v['standards']}" for k, v in list(with_standards.items())[:8]),
            "",
        ]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--workspace", type=Path, required=True)
    parser.add_argument("--run", required=True)
    parser.add_argument("--knowledge", type=Path)
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    text = precheck(args.workspace.resolve(), args.run, args.knowledge)
    if args.out:
        args.out.write_text(text, encoding="utf-8")
    sys.stdout.write(text + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
