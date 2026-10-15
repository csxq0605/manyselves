"""Read local usage only; no provider, app imports, or source-data writes.

Contiguous task-id segments are observable boundaries, not inferred invocations.
Summary pairs never cross these boundaries. Metrics describe observations only.
"""
import json
from collections import Counter, defaultdict
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "audit_manyselves"
RUNS = [
    "full-report-8c70828443b9499d80f8a6bda41c670c",
    "revise-report-8129b20dfb0d4bb9b1574645447375f0",
]


def normal(row):
    return row["phase"] != "context_compaction" and row["status"] == "success"


def bucket(rows):
    total = sum(r.get("input_tokens", 0) for r in rows)
    cached = sum(r.get("cached_input_tokens", 0) for r in rows)
    writes = sum(r.get("cache_write_input_tokens", 0) for r in rows)
    return {
        "requests": len(rows), "input_sum": total,
        "status_counts": dict(Counter(r["status"] for r in rows)),
        "usage_source_counts": dict(Counter(r.get("usage_source", "unknown") for r in rows)),
        "input_peak": max((r.get("input_tokens", 0) for r in rows), default=0),
        "cache_read_sum": cached, "cache_write_sum": writes,
        "uncached_sum": sum(r.get("uncached_input_tokens", 0) for r in rows),
        "cache_read_fraction": cached / total if total else None,
        "output_sum": sum(r.get("output_tokens", 0) for r in rows),
        "provider_active_seconds_sum": sum((r.get("provider_active_ms") or 0) / 1000 for r in rows),
    }


def cache_fraction(row):
    return row.get("cached_input_tokens", 0) / row["input_tokens"] if row["input_tokens"] else 0


def analyze(run):
    rows = [json.loads(line) for line in (ROOT / "test/.manyselves/usage" / f"{run}.jsonl").read_text(encoding="utf-8").splitlines() if line]
    rows = sorted((r for r in rows if r.get("provider_request_sent", True)), key=lambda r: r["timestamp"])
    groups = defaultdict(list)
    for row in rows:
        groups[row["agent_id"]].append(row)
    agents, segments, all_pairs = [], [], []
    for agent, history in groups.items():
        history_positions = {id(row): index for index, row in enumerate(history)}
        success = [r for r in history if normal(r)]
        summaries = [r for r in history if r["phase"] == "context_compaction"]
        agents.append({"agent": agent, "normal": bucket(success), "summary": bucket(summaries)})
        chunks = []
        for row in history:
            if not chunks or chunks[-1][-1].get("task_id") != row.get("task_id"):
                chunks.append([])
            chunks[-1].append(row)
        for number, chunk in enumerate(chunks, 1):
            normal_rows = [r for r in chunk if normal(r)]
            summary_rows = [r for r in chunk if r["phase"] == "context_compaction"]
            prefix = {"agent": agent, "segment": number, "task_id": chunk[0].get("task_id")}
            points = []
            for row in chunk:
                points.append({key: row.get(key) for key in (
                    "timestamp", "phase", "status", "input_tokens", "output_tokens",
                    "cached_input_tokens", "cache_write_input_tokens", "uncached_input_tokens",
                    "message_count", "request_chars", "provider_active_ms", "usage_source")})
            local_pairs = []
            # Collapse consecutive summary attempts, including retries, into an episode.
            i = 0
            while i < len(chunk):
                if chunk[i]["phase"] != "context_compaction":
                    i += 1
                    continue
                start = i
                while i < len(chunk) and chunk[i]["phase"] == "context_compaction":
                    i += 1
                episode = chunk[start:i]
                before = next((r for r in reversed(chunk[:start]) if normal(r)), None)
                after = next((r for r in chunk[i:] if normal(r)), None)
                future = [r for r in chunk[i:] if normal(r)]
                pair = {
                    **prefix, "timestamp": episode[0]["timestamp"],
                    "summary_attempts": len(episode),
                    "summary_success": any(r["status"] == "success" for r in episode),
                    "remaining_normal_requests_in_segment": len(future),
                    "remaining_normal_requests_for_agent": sum(normal(r) for r in history[history_positions[id(episode[-1])] + 1:]),
                    "summary": bucket(episode),
                    "before_input": before["input_tokens"] if before else None,
                    "after_input": after["input_tokens"] if after else None,
                    "before_cache_fraction": cache_fraction(before) if before else None,
                    "after_cache_fraction": cache_fraction(after) if after else None,
                    "observed_after_before_ratio": after["input_tokens"] / before["input_tokens"] if before and after and before["input_tokens"] else None,
                }
                local_pairs.append(pair)
            first_summary = next((i for i, r in enumerate(chunk) if r["phase"] == "context_compaction"), len(chunk))
            segments.append({
                **prefix, "initial_requests": sum(r["phase"] == "initial" for r in normal_rows),
                "normal": bucket(normal_rows), "summary": bucket(summary_rows),
                "normal_before_first_summary": bucket([r for r in chunk[:first_summary] if normal(r)]),
                "normal_after_first_summary": bucket([r for r in chunk[first_summary:] if normal(r)]),
                "points": points, "summary_episodes": local_pairs,
            })
            all_pairs.extend(local_pairs)
    valid = [p for p in all_pairs if p["summary_success"] and p["observed_after_before_ratio"] is not None]
    errors = [r for r in rows if r["status"] != "success"]
    sums = bucket([r for r in rows if normal(r)])
    return {
        "run": run, "provider_calls_by_this_script": 0,
        "model_counts": dict(Counter(r.get("model", "unknown") for r in rows)),
        "normal": sums, "summary": bucket([r for r in rows if r["phase"] == "context_compaction"]),
        "failed_requests": len(errors),
        "agent_count": len(agents), "segment_count": len(segments),
        "agents": agents, "segments": segments,
        "summary_episode_observations": {
            "episodes": len(all_pairs), "same_segment_successful_pairs": len(valid),
            "median_after_before_input_ratio": median(p["observed_after_before_ratio"] for p in valid) if valid else None,
            "after_input_lower_count": sum(p["after_input"] < p["before_input"] for p in valid),
            "before_cache_fraction_median": median(p["before_cache_fraction"] for p in valid) if valid else None,
            "after_cache_fraction_median": median(p["after_cache_fraction"] for p in valid) if valid else None,
            "episodes_with_at_most_two_remaining_normal_requests": sum(p["remaining_normal_requests_in_segment"] <= 2 for p in all_pairs),
            "episodes_with_at_most_five_remaining_normal_requests": sum(p["remaining_normal_requests_in_segment"] <= 5 for p in all_pairs),
            "episodes_with_at_most_two_remaining_agent_requests": sum(p["remaining_normal_requests_for_agent"] <= 2 for p in all_pairs),
            "remaining_calls_histogram": dict(sorted(Counter(p["remaining_normal_requests_in_segment"] for p in all_pairs).items())),
        },
        "limits": [
            "Usage rows describe already-compacted inputs, not an uncompressed counterfactual.",
            "A contiguous task-id segment may contain repeated invocations; no exact invocation identity is invented.",
            "No transcript-content attribution or exact unique-token count is available from token counts alone.",
            "Cache reads are included in input size; token totals are neither money nor wall-clock time.",
            "Before/after pairs exclude cross-task boundaries but still include intervening model/tool output.",
            "Remaining request count is hindsight, not information a running policy can reliably know.",
        ],
    }


def verify(result):
    for scope in result["agents"] + result["segments"]:
        for kind in ("normal", "summary"):
            b = scope[kind]
            assert b["input_sum"] == b["cache_read_sum"] + b["cache_write_sum"] + b["uncached_sum"], (scope.get("agent"), kind)
    for kind in ("normal", "summary"):
        assert result[kind]["input_sum"] == sum(s[kind]["input_sum"] for s in result["segments"])
        assert result[kind]["requests"] == sum(s[kind]["requests"] for s in result["segments"])


if __name__ == "__main__":
    results = [analyze(run) for run in RUNS]
    for result in results:
        verify(result)
    (OUT / "context_growth_metrics.json").write_text(json.dumps(results, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    for result in results:
        print(json.dumps({key: value for key, value in result.items() if key not in ("agents", "segments", "limits")}, ensure_ascii=False, indent=2))
