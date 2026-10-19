"""Read-only timing and compaction analysis of existing local usage ledgers."""
import json
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
RUNS = ["full-report-8c70828443b9499d80f8a6bda41c670c",
        "revise-report-8129b20dfb0d4bb9b1574645447375f0"]


def ts(row):
    return datetime.fromisoformat(row["timestamp"]).timestamp()


def stats(values):
    values = sorted(values)
    return {"min": min(values), "median": median(values),
            "p90": values[min(len(values)-1, int(len(values)*.9))], "max": max(values)} if values else {}


def analyze(run):
    path = ROOT / f"test/.manyselves/usage/{run}.jsonl"
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]
    rows = sorted((row for row in rows if row.get("provider_request_sent", True)), key=ts)
    phases, agents = defaultdict(list), defaultdict(list)
    edges = []
    for row in rows:
        phases[row["phase"]].append(row)
        agents[row["agent_id"]].append(row)
        duration = max(0, row.get("provider_active_ms", row.get("duration_ms", 0)) or 0) / 1000
        edges.extend([(ts(row)-duration, 1, row["phase"] == "context_compaction"),
                      (ts(row), -1, row["phase"] == "context_compaction")])
    edges.sort()
    active = compact = 0
    previous = edges[0][0]
    concurrency = defaultdict(float)
    compact_only = compact_any = 0
    gaps = []
    for at, delta, is_compact in edges:
        elapsed = at - previous
        concurrency[active] += elapsed
        if compact:
            compact_any += elapsed
            if active == compact:
                compact_only += elapsed
        if not active and elapsed > 30:
            gaps.append({"start": datetime.fromtimestamp(previous).isoformat(), "seconds": elapsed})
        active += delta
        compact += delta * int(is_compact)
        previous = at
    phase_stats = {}
    for phase, group in phases.items():
        phase_stats[phase] = {
            "requests": len(group),
            "input_tokens": sum(r.get("input_tokens", 0) for r in group),
            "output_tokens": sum(r.get("output_tokens", 0) for r in group),
            "cached_input_tokens": sum(r.get("cached_input_tokens", 0) for r in group),
            "active_seconds_sum": sum((r.get("provider_active_ms", 0) or 0)/1000 for r in group),
            "duration_seconds": stats([(r.get("provider_active_ms", 0) or 0)/1000 for r in group]),
            "input_tokens_distribution": stats([r.get("input_tokens", 0) for r in group]),
        }
    pairs = []
    agent_stats = []
    for agent, group in agents.items():
        for i, row in enumerate(group):
            if row["phase"] != "context_compaction":
                continue
            before = next((r for r in reversed(group[:i]) if r["phase"] != "context_compaction"), None)
            following = group[i+1:]
            next_compact = next((j for j, r in enumerate(following) if r["phase"] == "context_compaction"), len(following))
            normal = following[:next_compact]
            after = normal[0] if normal else None
            pairs.append({"agent": agent, "timestamp": row["timestamp"],
                          "before_input": before.get("input_tokens") if before else None,
                          "summary_input": row["input_tokens"], "summary_output": row["output_tokens"],
                          "after_input": after.get("input_tokens") if after else None,
                          "normal_calls_until_next_summary": len(normal),
                          "has_next_summary": next_compact < len(following),
                          "active_seconds": (row.get("provider_active_ms", 0) or 0)/1000})
        agent_stats.append({"agent": agent, "requests": len(group),
                            "summary_requests": sum(r["phase"] == "context_compaction" for r in group),
                            "active_seconds_sum": sum((r.get("provider_active_ms",0) or 0)/1000 for r in group),
                            "first_start": datetime.fromtimestamp(min(ts(r)-(r.get("provider_active_ms",0) or 0)/1000 for r in group)).isoformat(),
                            "last_end": group[-1]["timestamp"]})
    paired = [p for p in pairs if p["before_input"] and p["after_input"]]
    return {
        "run": run, "requests": len(rows),
        "provider_observation_span_seconds": edges[-1][0]-edges[0][0],
        "provider_active_union_seconds": sum(v for k,v in concurrency.items() if k > 0),
        "summary_active_union_seconds": compact_any,
        "summary_only_active_seconds": compact_only,
        "concurrency_seconds": dict(sorted(concurrency.items())),
        "queue_wait_seconds_sum": sum((r.get("queue_wait_ms",0) or 0)/1000 for r in rows),
        "tool_time_seconds_sum": sum((r.get("tool_time_ms",0) or 0)/1000 for r in rows),
        "errors": [{"timestamp":r["timestamp"], "phase":r["phase"], "class":r.get("error_class"),
                    "active_seconds":(r.get("provider_active_ms",0) or 0)/1000} for r in rows if r["status"] != "success"],
        "usage_sources": dict(Counter(r.get("usage_source") for r in rows)),
        "retry_requests": sum(bool(r.get("retry")) for r in rows),
        "phases": phase_stats, "agents": sorted(agent_stats, key=lambda x:-x["active_seconds_sum"]),
        "summary_pairs": pairs,
        "paired_count": len(paired),
        "post_pre_input_ratio": stats([p["after_input"]/p["before_input"] for p in paired]),
        "summary_spacing": dict(Counter(p["normal_calls_until_next_summary"] for p in pairs if p["has_next_summary"])),
        "gaps_over_30s": gaps,
        "caveats": ["Intervals estimated from completion timestamp minus provider_active_ms; not exact workflow critical path.",
                    "Before/after input ratios are observed neighboring calls, not a no-compaction counterfactual.",
                    "Overlapping active seconds cannot be subtracted directly from wall time."]}


results = [analyze(run) for run in RUNS]
(ROOT / "audit_manyselves/latency_compaction_metrics.json").write_text(
    json.dumps(results, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
for result in results:
    print(json.dumps({k:v for k,v in result.items() if k not in ["summary_pairs", "caveats"]}, ensure_ascii=False, indent=2))
