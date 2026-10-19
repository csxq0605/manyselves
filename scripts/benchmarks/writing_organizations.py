"""Compare three report-writing organizations from archived stage costs (F01).

Organizations (from the original discussion):

* ``lead`` — one lead author with tools writes all modules, one source review;
* ``topics+lead`` — topic analysts in parallel produce judgments, one lead
  author writes all prose from them; reviews stay per module;
* ``chapters`` (current) — module authors in parallel with shared facts and
  judgments, per-module review, then Cross for cross-module relations.

Stage costs are the archived run's per-session provider-active seconds
(``latency_compaction_metrics.json``, old orchestration, 525 requests).
Serial paths add stages; parallel branches take their maximum.  Module
revision sessions in that run also absorbed Cross repairs, so ``chapters``
counts them once inside the module lane and its Cross term is the reviewer
sessions only.  ``topics+lead`` depends on the unknown share ``s`` of
authoring spent on judgments, so it is reported for a range of ``s``.
Context size uses the production token estimator on the archived fact
packets.  These are lower-bound wall-clock models, not predictions.
Provider calls: 0.

    uv run python scripts/benchmarks/writing_organizations.py [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path

from loguru import logger

from manyselves.runtime.loops import agent_loop
from manyselves.runtime.providers.base import Message

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves"
PACKETS = ARCHIVE / "fact_packet_replay/Work/runs/replay/context"
JUDGMENTS = ROOT / "docs/research/experiments/2026-10-19-s06-assessment-burden-after.json"
MODULES = ("2.1", "2.2", "2.3", "2.4", "2.5")
ROLES = {
    "author": r":specialist-([\d.]+)$",
    "revision": r":module-([\d.]+)$",
    "review": r":module-auditor-([\d.]+)$",
    "cross": r":cross-owner-([\d.]+)$",
    "chief": r":chief-chapter-(\d+)$",
    "final": r":final-chapter-(\d+)$",
}
logger.disable("manyselves")


def stage_seconds(path: Path) -> dict[str, dict[str, float]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    run = data[0] if isinstance(data, list) else data
    out: dict[str, dict[str, float]] = {role: {} for role in ROLES}
    for agent in run["agents"]:
        for role, pattern in ROLES.items():
            match = re.search(pattern, agent["agent"])
            if match:
                out[role][match.group(1)] = round(agent["active_seconds_sum"], 1)
    return out


def tokens(text: str) -> int:
    return agent_loop._estimate_tokens([Message(role="user", content=text)]) - 4


def organizations(cost: dict[str, dict[str, float]], shares: list[float]) -> dict:
    author, revision, review = cost["author"], cost["revision"], cost["review"]
    tail = max(cost["chief"].values()) + max(cost["final"].values())
    lane = {m: author[m] + review[m] + revision[m] for m in MODULES}
    chapters = max(lane.values()) + max(cost["cross"].values()) + tail
    lead = sum(author.values()) + sum(review.values()) + sum(revision.values()) + tail
    topics = {}
    for share in shares:
        writing = max(share * author[m] for m in MODULES) + (1 - share) * sum(author.values())
        topics[str(share)] = round(
            (writing + max(review.values()) + sum(revision.values()) + tail) / 60, 1)
    return {
        "chapters": {
            "serial_minutes": round(chapters / 60, 1),
            "path": {"slowest_module_lane": max(lane, key=lane.get),
                     "module_lane_minutes": round(max(lane.values()) / 60, 1),
                     "cross_minutes": round(max(cost["cross"].values()) / 60, 1),
                     "chief_final_minutes": round(tail / 60, 1)},
        },
        "lead": {
            "serial_minutes": round(lead / 60, 1),
            "path": {"authoring_minutes": round(sum(author.values()) / 60, 1),
                     "single_review_minutes": round(sum(review.values()) / 60, 1),
                     "revision_minutes": round(sum(revision.values()) / 60, 1),
                     "chief_final_minutes": round(tail / 60, 1)},
        },
        "topics+lead": {"serial_minutes_by_judgment_share": topics},
        "cross_break_even_minutes": {
            # Cross time at which a Cross-free organization would match ``chapters``.
            name: round((value - (chapters - max(cost["cross"].values()))) / 60, 1)
            for name, value in {
                "lead": lead,
                **{f"topics+lead@{share}": minutes * 60 for share, minutes in topics.items()},
            }.items()
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--sessions", type=Path, default=ARCHIVE / "latency_compaction_metrics.json")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    cost = stage_seconds(args.sessions)
    facts = {m: tokens((PACKETS / f"module-{m}-facts.md").read_text(encoding="utf-8")) for m in MODULES}
    judgments = json.loads(JUDGMENTS.read_text(encoding="utf-8"))["output"]["grouped"]["modules"]
    judgment_tokens = {m: judgments[m]["assessment_output_tokens"] for m in MODULES}
    report = {
        "provider_calls": 0,
        "stage_active_seconds": cost,
        "organizations": organizations(cost, [0.25, 0.5, 0.75]),
        "context_tokens": {
            "fact_packet_by_module": facts,
            # Facts one writing session must hold before any prose or tool result.
            "chapters_largest_author_session": max(facts.values()),
            "lead_single_session": sum(facts.values()),
            "topics+lead_lead_session": sum(facts.values()) + sum(judgment_tokens.values()),
            "working_memory_tokens": 72000,
        },
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
