"""Local context sizing evidence; does not infer an uncompressed counterfactual."""
import json
import sys
from collections import defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "manyselves"))
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import ProjectEvidenceIndex
from manyselves.capabilities.distribution_reporting.runtime.research.fact_context import render_fact_packet

run = "full-report-8c70828443b9499d80f8a6bda41c670c"
groups = defaultdict(list)
for line in (ROOT / f"test/.manyselves/usage/{run}.jsonl").read_text(encoding="utf-8").splitlines():
    row = json.loads(line)
    if row.get("provider_request_sent", True):
        groups[row["agent_id"]].append(row)
profiles = []
for agent, rows in groups.items():
    normal = [r for r in rows if r["phase"] != "context_compaction" and r["status"] == "success"]
    summaries = sum(r["phase"] == "context_compaction" for r in rows)
    profiles.append({"agent": agent, "normal_requests": len(normal),
                     "initial_requests": sum(r["phase"] == "initial" for r in normal),
                     "summary_requests": summaries,
                     "first_input_tokens": normal[0]["input_tokens"],
                     "max_observed_input_tokens": max(r["input_tokens"] for r in normal),
                     "normal_generated_tokens_sum": sum(r["output_tokens"] for r in normal),
                     "no_summary_observed": summaries == 0})

index = ProjectEvidenceIndex(ROOT / "audit_manyselves/fact_packet_replay", "replay")
packets = []
for module in ("2.1", "2.2", "2.3", "2.4", "2.5"):
    items = index.module_items(module)
    raw = "\n".join(item.model_dump_json() for item in items) + "\n"
    packed = render_fact_packet(items, module)
    packets.append({"module": module, "records": len(items), "raw_jsonl_chars": len(raw),
                    "fact_packet_chars": len(packed), "character_reduction": 1-len(packed)/len(raw)})
result = {"source_run": run, "provider_calls": 0, "agents": profiles, "fact_packets": packets,
          "limits": ["Observed token peaks include the existing compression policy and cannot prove the no-compression peak.",
                     "Fact-packet character reductions compare equivalent normalized-record serialization, not raw original file size or tokenizer counts.",
                     "No generated-summary faithfulness or complete report quality is measured here."]}
(ROOT / "audit_manyselves/context_strategy_profile.json").write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n",encoding="utf-8")
print(json.dumps(result,ensure_ascii=False,indent=2))
