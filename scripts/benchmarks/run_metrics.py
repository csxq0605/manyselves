"""Per-stage cost and critical path of one Run from its usage ledger (F03).

Reads ``<workspace>/.manyselves/usage/<run_id>.jsonl`` (rows written by
``manyselves.runtime.usage_ledger``) and reports, per stage and in total:
provider requests, input / cached / output tokens, context-compaction share,
provider-active *union* time (parallel time is never summed as wall-clock), and
the observed critical chain of Agent sessions.

A session is one ``agent_id`` (profile:agent:conversation-key) within one
stage.  Stages are assigned by ``--stage NAME=REGEX`` rules matched against
``"<task_id> <agent_id>"``, first match wins.  The defaults follow the Reporting
task ids and conversation keys; Cross repairs reuse the module conversations,
so they are recognised by task id.  Provider calls: 0.

    uv run python scripts/benchmarks/run_metrics.py --ledger PATH [--output out.json]
    uv run python scripts/benchmarks/run_metrics.py --sessions latency.json   # archived agent windows
"""

from __future__ import annotations

import argparse
import json
import re
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

DEFAULT_STAGES = [
    ("cross", r"(^\S*cross\S* |:cross-|:main-cross)"),
    ("final", r"(^\S*final\S* |:final-)"),
    ("chief", r":chief-"),
    ("module-author", r"(^\S*authoring\S* |:specialist-)"),
    ("module-review-loop", r":module-(auditor-)?[\d.]+$"),
]


def _time(value: str) -> datetime:
    # Archived rows mix naive and offset timestamps written by one local clock.
    return datetime.fromisoformat(value).replace(tzinfo=None)


def union_seconds(intervals: list[tuple[datetime, datetime]]) -> float:
    total, end = 0.0, None
    for start, stop in sorted(intervals):
        if end is None or start > end:
            total += (stop - start).total_seconds()
            end = stop
        elif stop > end:
            total += (stop - end).total_seconds()
            end = stop
    return total


def critical_chain(requests: list[tuple[datetime, datetime, str, str]]) -> list[dict]:
    """Observed critical chain at request granularity.

    Walk back from the request that ended last; each predecessor is the request
    that ended last before the current one started.  Consecutive requests of the
    same stage/session are merged into one segment.  This is the observed
    waiting chain, not a proof of logical dependency.
    """

    if not requests:
        return []
    ordered = sorted(requests, key=lambda item: item[1])
    current = ordered[-1]
    chain = [current]
    while True:
        start = current[0]
        before = [item for item in ordered if item[1] <= start]
        if not before:
            break
        current = before[-1]
        chain.append(current)
    segments: list[dict] = []
    for start, end, stage, session in reversed(chain):
        if segments and segments[-1]["stage"] == stage and segments[-1]["session"] == session:
            segments[-1]["requests"] += 1
            segments[-1]["_end"] = end
        else:
            segments.append({"stage": stage, "session": session, "requests": 1, "_start": start, "_end": end})
    return segments


def stage_for(session: str, rules: list[tuple[str, re.Pattern]]) -> str:
    for name, pattern in rules:
        if pattern.search(session):
            return name
    return "other"


def from_ledger(path: Path, rules) -> dict:
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    rows = [row for row in rows if row.get("provider_request_sent", True)]
    stages: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    intervals: dict[str, list] = defaultdict(list)
    requests = []
    all_intervals = []
    for row in rows:
        end = _time(row["timestamp"])
        start = end - timedelta(milliseconds=row.get("provider_active_ms") or row.get("duration_ms") or 0)
        agent = str(row.get("agent_id") or row.get("task_id") or "unknown")
        stage = stage_for(f"{row.get('task_id') or ''} {agent}", rules)
        bucket = stages[stage]
        bucket["requests"] += 1
        bucket["compaction_requests"] += row.get("phase") == "context_compaction"
        bucket["errors"] += row.get("status") == "error"
        for key in ("input_tokens", "cached_input_tokens", "output_tokens"):
            bucket[key] += int(row.get(key) or 0)
        intervals[stage].append((start, end))
        all_intervals.append((start, end))
        requests.append((start, end, stage, agent))
    origin = min(start for start, _ in all_intervals) if all_intervals else None
    return finish(stages, intervals, critical_chain(requests), all_intervals, origin, rules)


def from_sessions(path: Path, rules) -> dict:
    """Archived runs keep only per-session windows (latency_compaction_metrics.json)."""

    data = json.loads(path.read_text(encoding="utf-8"))
    run = data[0] if isinstance(data, list) else data
    stages: dict[str, dict] = defaultdict(lambda: defaultdict(float))
    intervals: dict[str, list] = defaultdict(list)
    for agent in run["agents"]:
        window = (_time(agent["first_start"]), _time(agent["last_end"]))
        stage = stage_for(f" {agent['agent']}", rules)
        stages[stage]["requests"] += agent["requests"]
        stages[stage]["compaction_requests"] += agent["summary_requests"]
        intervals[stage].append(window)
    all_intervals = [w for ws in intervals.values() for w in ws]
    report = finish(stages, intervals, None, all_intervals, min(s for s, _ in all_intervals), rules)
    report["note"] = ("Archived per-session windows only: stage time is session-window union, not "
                      "provider-active union; tokens and the request-level chain are unavailable.")
    report["run"] = run.get("run")
    return report


def finish(stages, intervals, chain, all_intervals, origin, rules) -> dict:
    def minute(value):
        return round((value - origin).total_seconds() / 60, 1)

    out = {}
    for name in [rule[0] for rule in rules] + ["other"]:
        if name not in stages:
            continue
        bucket = {k: int(v) for k, v in stages[name].items()}
        spans = intervals[name]
        bucket["active_union_minutes"] = round(union_seconds(spans) / 60, 1)
        bucket["window_minutes"] = [minute(min(s for s, _ in spans)), minute(max(e for _, e in spans))]
        if bucket.get("input_tokens"):
            bucket["cached_share"] = round(bucket["cached_input_tokens"] / bucket["input_tokens"], 3)
        out[name] = bucket
    totals = defaultdict(int)
    for bucket in out.values():
        for key in ("requests", "compaction_requests", "input_tokens", "cached_input_tokens", "output_tokens"):
            totals[key] += bucket.get(key, 0)
    span = (max(e for _, e in all_intervals) - origin).total_seconds() / 60 if all_intervals else 0
    return {
        "provider_calls": 0,
        "totals": {**totals, "span_minutes": round(span, 1),
                   "active_union_minutes": round(union_seconds(all_intervals) / 60, 1)},
        "stages": out,
        "critical_chain": None if chain is None else [
            {"stage": item["stage"], "session": item["session"], "requests": item["requests"],
             "window_minutes": [minute(item["_start"]), minute(item["_end"])]}
            for item in chain
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--ledger", type=Path, help="usage ledger JSONL of one run")
    source.add_argument("--sessions", type=Path, help="archived latency_compaction_metrics.json")
    parser.add_argument("--stage", action="append", default=[], metavar="NAME=REGEX")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    raw = [tuple(item.split("=", 1)) for item in args.stage] or DEFAULT_STAGES
    rules = [(name, re.compile(pattern)) for name, pattern in raw]
    report = from_ledger(args.ledger, rules) if args.ledger else from_sessions(args.sessions, rules)
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
