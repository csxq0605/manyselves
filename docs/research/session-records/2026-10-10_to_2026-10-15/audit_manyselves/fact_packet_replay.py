"""Replay frozen normalized evidence through current production code, locally."""
import json
import sys
from pathlib import Path

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "manyselves"))
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import (
    prepare_module_fact_context,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (
    ProjectEvidenceIndex,
)

source = root / "test/Work/runs/full-report-8c70828443b9499d80f8a6bda41c670c/preparation/evidence.jsonl"
workspace = root / "audit_manyselves/fact_packet_replay"
snapshot = workspace / "Work/runs/replay/preparation/evidence.jsonl"
snapshot.parent.mkdir(parents=True, exist_ok=True)
snapshot.write_bytes(source.read_bytes())
index = ProjectEvidenceIndex(workspace, "replay")
results = {"source_run": source.parent.parent.name, "provider_calls": 0, "modules": {}, "checks": {}}
for module_id in ("2.1", "2.2", "2.3", "2.4", "2.5"):
    packet = prepare_module_fact_context(workspace, "replay", module_id)
    items = index.module_items(module_id)
    text = (workspace / packet.ref).read_text(encoding="utf-8")
    results["modules"][module_id] = {
        "records": len(items), "packet_chars": len(text),
        "delivery_mode": "inline" if 'delivery_mode="inline"' in packet.inline_context else "reference",
        "all_ids_present": all(item.id in text for item in items),
        "template_skill_records": sum("report-template-role-skills" in str(item.source.path) for item in items),
    }
for name, module, query, predicate in (
    ("residual_current", "2.4", "剩余电流 实测 占比 变压器 低压侧", lambda e: e.fact.startswith("实测剩余电流=")),
    ("thermal", "2.2", "测点温度", lambda e: "测点温度=" in e.fact and e.source.sheet == "红外热成像检测记录表"),
    ("spd", "2.3", "电涌保护器 SPD 避雷器 过电压", lambda e: e.fact.startswith("电涌保护装置=") and e.source.sheet == "总配评估详情表"),
):
    items = [e for e in index.module_items(module) if predicate(e)]
    hits = index.search(query, 12)
    target_ids = {e.id for e in items}
    results["checks"][name] = {
        "records": len(items), "ids": [e.id for e in items],
        "keyword_probe": {
            "query": query, "limit": 12, "hits": len(hits),
            "matching_scope_records": len(target_ids & {e.id for e in hits}),
        },
    }
assert [results["checks"][name]["records"] for name in ("residual_current", "thermal", "spd")] == [14, 13, 9]
assert all(row["all_ids_present"] and not row["template_skill_records"] for row in results["modules"].values())
output = root / "audit_manyselves/fact_packet_replay_metrics.json"
output.write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
print(json.dumps(results, ensure_ascii=False, indent=2))
