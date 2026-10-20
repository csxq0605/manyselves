"""Compare three report-writing organizations by workload (F01, rebuilt).

The first version added the archived run's session seconds: the "one lead
author" option was charged every old author, review and revision session.
That kept work the lead option would restructure and could not support a
choice.  This version holds the *work* fixed and lets each organization
decide how it is split into sessions:

* the same content is generated in every organization: per-module authoring,
  review and revision output (tokens), and the joint review output;
* each session carries a mandatory input (facts, knowledge, drafts it must
  see) and grows by its own output and tool reads per request;
* when a session's input passes ``working_memory_tokens`` it compacts to the
  configured target (one summary request), exactly as the Agent loop does;
* request time = a * uncached input + c * cached input + b * output, with
  a, b, c fitted by least squares on the archived run's per-session usage.

Organizations (the three discussed for F01; the lead option in two forms):

* ``chapters`` (current): five module lanes in parallel, each author ->
  review -> revision, then the per-owner joint review on object dossiers;
* ``lead_module_by_module``: one author conversation writes the modules one
  after another (holding one module's facts at a time), then one reviewer
  and one reviser do the same; consistency is kept by the single author, so
  no joint review;
* ``lead_all_facts_at_once``: the same, but every module's facts stay in the
  input together (the literal reading of "one lead author");
* ``topics+lead_module_by_module``: five analysts in parallel write the
  judgments, one lead writes the prose module by module from facts and all
  judgments, per-module reviews run in parallel, the lead revises; no joint
  review.

The Chief/Final tail and photo reading are the same for every organization
and left out.  Unknown inputs are parameters with a stated range: tool-read
tokens per request, knowledge per module, and the joint-review input.
Results are model estimates for comparing organizations, not predictions of
a run.  Provider calls: 0.

    uv run python scripts/benchmarks/writing_organizations.py [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from loguru import logger

from manyselves.config.schema import AgentDefaults
from manyselves.runtime.context_budget import estimate_text_tokens, retained_input_budget_for

ROOT = Path(__file__).resolve().parents[2]
ARCHIVE = ROOT / "docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves"
PACKETS = ARCHIVE / "fact_packet_replay/Work/runs/replay/context"
JUDGMENTS = ROOT / "docs/research/experiments/2026-10-19-s06-assessment-burden-after.json"
REPORT = ARCHIVE / "配电安全专家咨询报告.docx.txt"
MODULES = ("2.1", "2.2", "2.3", "2.4", "2.5")
ROLES = {
    "author": r":specialist-([\d.]+)$",
    "revision": r":module-([\d.]+)$",
    "review": r":module-auditor-([\d.]+)$",
    "joint": r":cross-owner-([\d.]+)$",
}
BASELINE = "tool2500-knowledge9000-joint12000"
logger.disable("manyselves")


@dataclass(frozen=True)
class Throughput:
    uncached: float  # seconds per uncached input token
    cached: float  # seconds per cached input token
    output: float  # seconds per output token

    def seconds(self, uncached: float, cached: float, output: float) -> float:
        return self.uncached * uncached + self.cached * cached + self.output * output


@dataclass(frozen=True)
class Session:
    name: str
    mandatory: int  # tokens kept in every request
    output: int  # tokens generated over the session


def archive_usage() -> dict:
    data = json.loads((ARCHIVE / "context_growth_metrics.json").read_text(encoding="utf-8"))
    return data[0]


def fit_throughput(usage: dict) -> tuple[Throughput, dict]:
    rows, seconds = [], []
    for agent in usage["agents"]:
        for part in ("normal", "summary"):
            item = agent.get(part) or {}
            if not item.get("requests"):
                continue
            cached = item["input_sum"] - item["uncached_sum"]
            rows.append([item["uncached_sum"], cached, item["output_sum"]])
            seconds.append(item["provider_active_seconds_sum"])
    matrix, target = np.array(rows, dtype=float), np.array(seconds, dtype=float)
    coefficients, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    coefficients = np.clip(coefficients, 0.0, None)
    predicted = matrix @ coefficients
    r2 = 1 - float(((target - predicted) ** 2).sum() / ((target - target.mean()) ** 2).sum())
    fit = Throughput(*map(float, coefficients))
    return fit, {
        "points": len(rows),
        "r2": round(r2, 3),
        "ms_per_1k_uncached_input": round(fit.uncached * 1e6, 1),
        "ms_per_1k_cached_input": round(fit.cached * 1e6, 1),
        "ms_per_1k_output": round(fit.output * 1e6, 1),
    }


def role_outputs(usage: dict) -> dict[str, dict[str, int]]:
    """Content each role generated in the archived run (tokens), per module."""

    out: dict[str, dict[str, int]] = {role: {} for role in ROLES}
    per_request: dict[str, list[int]] = {role: [0, 0] for role in ROLES}
    for agent in usage["agents"]:
        for role, pattern in ROLES.items():
            match = re.search(pattern, agent["agent"])
            if match:
                normal = agent["normal"]
                out[role][match.group(1)] = normal["output_sum"]
                per_request[role][0] += normal["output_sum"]
                per_request[role][1] += normal["requests"]
    mean_output = {role: total / max(1, count) for role, (total, count) in per_request.items()}
    return {"output": out, "mean_output_per_request": mean_output}


def summary_output_per_request(usage: dict) -> float:
    item = usage["summary"]
    return item["output_sum"] / max(1, item["requests"])


def prose_tokens() -> dict[str, int]:
    lines = REPORT.read_text(encoding="utf-8").splitlines()
    starts = {}
    for index, line in enumerate(lines):
        for module in MODULES:
            if line.strip() == f"{module}. " + line.strip().split(". ", 1)[-1] and index > 100:
                starts.setdefault(module, index)
        if line.startswith("3. 结论与建议") and index > 100:
            starts.setdefault("end", index)
    order = [*MODULES, "end"]
    return {
        module: estimate_text_tokens("\n".join(lines[starts[module]:starts[order[i + 1]]]))
        for i, module in enumerate(MODULES)
    }


def simulate(
    session: Session,
    *,
    mean_output: float,
    tool_tokens: int,
    summary_output: float,
    cache_fraction: float,
    throughput: Throughput,
    config: AgentDefaults,
    history: int = 0,
) -> dict:
    """Request-by-request session cost with the Agent loop's compaction rule.

    ``history`` is what an earlier task of the same conversation left behind.
    """

    trigger = config.working_memory_tokens
    target = int(trigger * config.working_memory_compaction_target_ratio)
    requests = max(1, math.ceil(session.output / mean_output))
    seconds = input_tokens = 0.0
    compactions = 0
    for _ in range(requests):
        if session.mandatory + history > trigger:
            # One summary request reads everything, then history shrinks.
            seconds += throughput.seconds(session.mandatory + history, 0, summary_output)
            input_tokens += session.mandatory + history
            history = max(0, target - session.mandatory)
            compactions += 1
        sent = session.mandatory + history
        seconds += throughput.seconds(sent * (1 - cache_fraction), sent * cache_fraction, mean_output)
        input_tokens += sent
        history += int(mean_output + tool_tokens)
    return {
        "session": session.name,
        "mandatory_input_tokens": session.mandatory,
        "output_tokens": session.output,
        "requests": requests,
        "compactions": compactions,
        "input_tokens": int(input_tokens),
        "minutes": round(seconds / 60, 1),
        "mandatory_over_retained_budget": session.mandatory > retained_input_budget_for(config),
        "history_after": history,
    }


def organizations(
    work: dict,
    *,
    knowledge_per_module: int,
    joint_input_per_owner: int,
    run_session,
) -> dict:
    facts, prose, judgments = work["facts"], work["prose"], work["judgments"]
    author, review, revision, joint = (
        work["output"][role] for role in ("author", "review", "revision", "joint")
    )
    all_facts, all_prose = sum(facts.values()), sum(prose.values())
    all_knowledge = knowledge_per_module * len(MODULES)

    def lane(module: str) -> list[dict]:
        return [
            run_session(Session(f"author-{module}", facts[module] + knowledge_per_module, author[module]), "author"),
            run_session(Session(f"review-{module}", facts[module] + prose[module] + knowledge_per_module,
                                review[module]), "review"),
            run_session(Session(f"revision-{module}", facts[module] + prose[module] + knowledge_per_module,
                                revision[module]), "revision"),
        ]

    chapters_lanes = {module: lane(module) for module in MODULES}
    chapters_joint = [
        run_session(Session(f"joint-{module}", joint_input_per_owner, joint[module]), "joint")
        for module in MODULES
    ]
    chapters = {
        "critical_minutes": round(
            max(sum(s["minutes"] for s in sessions) for sessions in chapters_lanes.values())
            + max(s["minutes"] for s in chapters_joint), 1),
        "sessions": [s for sessions in chapters_lanes.values() for s in sessions] + chapters_joint,
    }

    # One author holding every module's facts at once (the literal option).
    held = [
        run_session(Session("lead-author", all_facts + all_knowledge, sum(author.values())), "author"),
        run_session(Session("lead-review", all_facts + all_prose + all_knowledge, sum(review.values())), "review"),
        run_session(Session("lead-revision", all_facts + all_prose + all_knowledge,
                            sum(revision.values())), "revision"),
    ]
    lead_all = {"critical_minutes": round(sum(s["minutes"] for s in held), 1), "sessions": held}

    def serial(role: str, mandatory, outputs) -> list[dict]:
        """One conversation working module by module; history carries over."""

        sessions, history = [], 0
        for module in MODULES:
            result = run_session(
                Session(f"lead-{role}-{module}", mandatory(module), outputs[module]), role, history,
            )
            history = result["history_after"]
            sessions.append(result)
        return sessions

    by_module = (
        serial("author", lambda m: facts[m] + knowledge_per_module, author)
        + serial("review", lambda m: facts[m] + prose[m] + knowledge_per_module, review)
        + serial("revision", lambda m: facts[m] + prose[m] + knowledge_per_module, revision)
    )
    lead = {"critical_minutes": round(sum(s["minutes"] for s in by_module), 1), "sessions": by_module}

    analysts = [
        run_session(Session(f"analyst-{m}", facts[m] + knowledge_per_module, judgments[m]), "author")
        for m in MODULES
    ]
    prose_share = {m: max(0, author[m] - judgments[m]) for m in MODULES}
    writer_sessions = serial(
        "author", lambda m: facts[m] + sum(judgments.values()) + knowledge_per_module, prose_share,
    )
    reviews = [
        run_session(Session(f"review-{m}", facts[m] + prose[m] + knowledge_per_module, review[m]), "review")
        for m in MODULES
    ]
    lead_revision = serial("revision", lambda m: facts[m] + prose[m] + knowledge_per_module, revision)
    topics = {
        "critical_minutes": round(
            max(s["minutes"] for s in analysts) + sum(s["minutes"] for s in writer_sessions)
            + max(s["minutes"] for s in reviews) + sum(s["minutes"] for s in lead_revision), 1),
        "sessions": [*analysts, *writer_sessions, *reviews, *lead_revision],
    }
    result = {
        "chapters": chapters,
        "lead_module_by_module": lead,
        "lead_all_facts_at_once": lead_all,
        "topics+lead_module_by_module": topics,
    }
    for value in result.values():
        sessions = value["sessions"]
        value["provider_minutes"] = round(sum(s["minutes"] for s in sessions), 1)
        value["input_tokens"] = sum(s["input_tokens"] for s in sessions)
        value["compactions"] = sum(s["compactions"] for s in sessions)
        value["largest_mandatory_input"] = max(s["mandatory_input_tokens"] for s in sessions)
        value["sessions_over_retained_budget"] = [
            s["session"] for s in sessions if s["mandatory_over_retained_budget"]
        ]
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    config = AgentDefaults()
    usage = archive_usage()
    throughput, fit = fit_throughput(usage)
    roles = role_outputs(usage)
    normal = usage["normal"]
    work = {
        "facts": {
            m: estimate_text_tokens((PACKETS / f"module-{m}-facts.md").read_text(encoding="utf-8"))
            for m in MODULES
        },
        "prose": prose_tokens(),
        "judgments": {
            m: v["assessment_output_tokens"]
            for m, v in json.loads(JUDGMENTS.read_text(encoding="utf-8"))["output"]["grouped"]["modules"].items()
        },
        "output": roles["output"],
    }
    summary_output = summary_output_per_request(usage)

    def runner(tool_tokens: int):
        def run_session(session: Session, role: str, history: int = 0) -> dict:
            return simulate(
                session,
                mean_output=roles["mean_output_per_request"][role],
                tool_tokens=tool_tokens,
                summary_output=summary_output,
                cache_fraction=normal["cache_read_fraction"],
                throughput=throughput,
                config=config,
                history=history,
            )
        return run_session

    scenarios = {}
    for tool_tokens in (1000, 2500, 4000):
        for knowledge in (4000, 9000):
            for joint_input in (12000, 30000):
                name = f"tool{tool_tokens}-knowledge{knowledge}-joint{joint_input}"
                scenarios[name] = organizations(
                    work,
                    knowledge_per_module=knowledge,
                    joint_input_per_owner=joint_input,
                    run_session=runner(tool_tokens),
                )
    summary = {
        name: {org: {k: v[k] for k in ("critical_minutes", "provider_minutes", "compactions",
                                       "largest_mandatory_input", "sessions_over_retained_budget")}
               for org, v in result.items()}
        for name, result in scenarios.items()
    }
    report = {
        "provider_calls": 0,
        "throughput_fit": fit,
        "workload": {**work, "mean_output_per_request": roles["mean_output_per_request"],
                     "summary_output_per_request": round(summary_output, 1),
                     "cache_read_fraction": round(normal["cache_read_fraction"], 3)},
        "config": {"working_memory_tokens": config.working_memory_tokens,
                   "compaction_target_ratio": config.working_memory_compaction_target_ratio,
                   "retained_input_budget": retained_input_budget_for(config)},
        "summary": summary,
        # Per-session detail for the middle scenario; the others are in "summary".
        "baseline_scenario": BASELINE,
        "baseline_sessions": scenarios[BASELINE],
    }
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(json.dumps({"throughput_fit": fit, "workload": {k: work[k] for k in ("facts", "prose", "judgments")},
                      "summary": summary}, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
