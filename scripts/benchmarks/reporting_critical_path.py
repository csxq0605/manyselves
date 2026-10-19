"""Offline critical path of the file-defined Reporting workflow (F03 / F01).

Walks the production workflow YAML exactly as declared (sequence, if, goto,
parallel/join, subworkflow) and counts Agent invocations.  Branch decisions are
supplied by named scenarios instead of a Provider, so the result is structural:

* ``serial_llm_depth`` — Agent sessions on the longest dependent chain
  (parallel branches contribute their maximum, not their sum);
* ``llm_sessions`` — every Agent session the workflow would start.

Observed timing of a real Run comes from ``run_metrics.py``.  Provider calls: 0.

    uv run python scripts/benchmarks/reporting_critical_path.py \
        [--workflows DIR] [--entry full-report] [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

import yaml

ROOT = Path(__file__).resolve().parents[2]
WORKFLOWS = ROOT / "manyselves/capabilities/distribution_reporting/workflows"
# Generated per-module lanes are instances of one template (see
# runtime/module_lane_definitions.py and cross_owner_definitions.py).
TEMPLATES = [
    (re.compile(r"^distribution-module-[\d.]+-runtime-lane$"), "distribution-module-runtime-lane"),
    (re.compile(r"^distribution-cross-owner-[\d.]+-repair-lane$"), "distribution-cross-owner-repair-lane"),
    (re.compile(r"^distribution-cross-owner-local-[\d.]+-module-review-lane$"),
     "distribution-cross-owner-local-module-review-lane"),
]
TERMINAL = {"end_workflow", "fail_workflow"}

Decide = Callable[[str, str, int, int], object]
"""(template_id, variable, visit_of_instance, call_index_within_visit) -> value"""


@dataclass
class Result:
    depth: int = 0
    sessions: list[tuple[str, str, str]] = field(default_factory=list)
    chain: list[str] = field(default_factory=list)
    inputs: int = 0
    decisions: Counter = field(default_factory=Counter)


class Simulator:
    def __init__(self, workflows_dir: Path, decide: Decide):
        self.defs = {}
        for path in sorted(workflows_dir.glob("*.yaml")):
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
            self.defs[data["id"]] = data
        self.decide = decide
        self.visits: Counter = Counter()

    def template(self, workflow_id: str) -> str:
        if workflow_id in self.defs:
            return workflow_id
        for pattern, template in TEMPLATES:
            if pattern.match(workflow_id):
                return template
        raise KeyError(f"unknown workflow {workflow_id}")

    @staticmethod
    def bind(instance: str, workflow_id: str) -> str:
        """Resolve a lane parameter the way the per-module definitions do."""

        if workflow_id == "{local_review_workflow_id}":
            module_id = re.search(r"-([\d.]+)-repair-lane$", instance).group(1)
            return f"distribution-cross-owner-local-{module_id}-module-review-lane"
        return workflow_id

    def run(self, workflow_id: str) -> Result:
        template = self.template(workflow_id)
        self.visits[workflow_id] += 1
        visit = self.visits[workflow_id]
        actions = self.defs[template]["actions"]
        index = {action["id"]: i for i, action in enumerate(actions)}
        calls: Counter = Counter()
        result = Result()
        self._walk(workflow_id, template, actions, index, 0, None, visit, calls, result)
        return result

    def _walk(self, instance, template, actions, index, start, stop, visit, calls, result) -> int:
        position, steps = start, 0
        while position < len(actions):
            steps += 1
            if steps > 5000:
                raise RuntimeError(f"{instance}: scenario does not terminate")
            action = actions[position]
            kind = action["kind"]
            if action["id"] == stop:
                return position
            if kind in TERMINAL:
                return len(actions)
            if kind == "invoke_agent":
                result.depth += 1
                label = f"{instance}:{action['id']}"
                result.sessions.append((instance, action["agent"], action["id"]))
                result.chain.append(label)
            elif kind == "request_input":
                result.inputs += 1
            elif kind == "if":
                variable = action["condition"]["variable"]
                calls[variable] += 1
                value = self.decide(template, variable, visit, calls[variable])
                result.decisions[f"{template}:{variable}={value}"] += 1
                condition = action["condition"]
                taken = value == condition["value"] if condition.get("operator") == "eq" else bool(value)
                position = index[action["then"] if taken else action["otherwise"]]
                continue
            elif kind == "goto":
                position = index[action["target"]]
                continue
            elif kind == "subworkflow":
                child = self.run(self.bind(instance, action["workflow"]))
                result.depth += child.depth
                result.sessions += child.sessions
                result.chain += child.chain
                result.inputs += child.inputs
                result.decisions += child.decisions
            elif kind == "parallel":
                join = action["join"]
                longest = None
                for branch_start in action["branches"].values():
                    branch = Result()
                    self._walk(instance, template, actions, index, index[branch_start], join,
                               visit, calls, branch)
                    result.sessions += branch.sessions
                    result.inputs += branch.inputs
                    result.decisions += branch.decisions
                    if longest is None or branch.depth > longest.depth:
                        longest = branch
                if longest is not None:
                    result.depth += longest.depth
                    result.chain += longest.chain
                position = index[join] + 1
                continue
            position += 1
        return position


def sequence(*values):
    """Return values in call order within one visit; the last value repeats."""

    return lambda n: values[min(n, len(values)) - 1]


def scenario(module_rounds: int, cross_rounds: int, local_rounds: int, final_rounds: int) -> Decide:
    """Build decisions from the number of rework rounds at each review boundary.

    ``*_rounds = 0`` means the reviewer accepts on first read.  Module and
    local Cross reviews reopen after each recheck until their round count is
    exhausted; Final runs ``final_rounds`` Chief-revision/recheck cycles.
    """

    revise_module = sequence(*([True] * module_rounds), False)
    module_lane = {
        "requested-revision": sequence(False),
        "skip-to-baseline": sequence(False),
        "should-retry-module-preflight-revision": sequence(False),
        "should-resume-module-main-exception": sequence(False),
        "should-invoke-module-author": sequence(True),
        "should-review-module-lane": sequence(True),
        "should-revise-module-preflight": sequence(False),
        "should-invoke-module-reviewer": sequence(True),
        "should-recheck-module": sequence(False),
        "should-revise-module": revise_module,
        "should-defer-module-author-exception": sequence(False),
        "should-revise-module-recheck-preflight": sequence(False),
        "should-invoke-module-recheck": sequence(True),
        "should-defer-module-recheck-exception": sequence(False),
    }
    # The post-Join resume of a finished lane finds nothing left to author/review.
    module_resume = {**{k: sequence(False) for k in module_lane}}
    cross = {
        "exception-required": sequence(False),
        "needs-user": sequence(False),
        "needs-repairs": sequence(*([True] * cross_rounds), False),
    }
    repair = {
        "needs-repair": sequence(True),
        "should-invoke-cross-owner-revision": sequence(True),
        "should-invoke-cross-owner-author-exception": sequence(False),
        "should-request-cross-owner-author-input": sequence(False),
        "should-return-cross-owner-to-author": sequence(False),
    }
    local = {
        "should-revise-local-preflight": sequence(False),
        "should-invoke-cross-owner-local-review": sequence(True),
        "should-revise-local-module": sequence(local_rounds > 0),
        "should-invoke-cross-owner-local-revision": sequence(True),
        "should-revise-local-recheck-preflight": sequence(False),
        "should-invoke-cross-owner-local-recheck": sequence(True),
        "should-revise-local-module-after-recheck": sequence(*([True] * max(0, local_rounds - 1)), False),
    }
    # Every chapter is treated as needing each Final round (upper bound on sessions).
    chapter = {"should-invoke-chief-chapter": sequence(True), "should-invoke-final-chapter": sequence(True),
               "should-invoke-final-chief-revision": sequence(True), "should-invoke-final-recheck": sequence(True)}
    final = {"needs-final-review-round": sequence(*([True] * final_rounds), False)}
    readiness = {"readiness-route": sequence("continue"), "decision-route": sequence("continue")}
    preparation = {"resume": sequence(False), "preparation-complete": sequence(False),
                   "full-report": sequence(True)}
    tables = {
        "distribution-cross-joint-review": cross,
        "distribution-cross-owner-repair-lane": repair,
        "distribution-cross-owner-local-module-review-lane": local,
        "distribution-final-review-cycle": final,
        "distribution-evidence-readiness": readiness,
    }

    def decide(template: str, variable: str, visit: int, n: int):
        if template == "distribution-module-runtime-lane":
            table = module_lane if visit == 1 else module_resume
        elif template in tables:
            table = tables[template]
        elif "chapter" in template or "chief" in template or "final" in template:
            table = chapter
        else:
            table = preparation
        if variable not in table:
            raise KeyError(f"scenario has no decision for {template}:{variable}")
        return table[variable](n)

    return decide


SCENARIOS = {
    "accept-first-read": dict(module_rounds=0, cross_rounds=0, local_rounds=0, final_rounds=0),
    "one-round-each": dict(module_rounds=1, cross_rounds=1, local_rounds=0, final_rounds=1),
    "one-round-each+local": dict(module_rounds=1, cross_rounds=1, local_rounds=1, final_rounds=1),
    "two-module-rounds": dict(module_rounds=2, cross_rounds=1, local_rounds=1, final_rounds=1),
    "bounded-final-max": dict(module_rounds=1, cross_rounds=1, local_rounds=1, final_rounds=3),
}


def stage_of(instance: str, action_id: str) -> str:
    task = action_id
    if action_id == "invoke-current-module-author":
        return "module-author"
    if instance.startswith("distribution-module-"):
        return "module-review-loop"
    if "cross" in instance or "cross" in task:
        return "cross"
    if "final" in instance or "final" in task:
        return "final"
    if "chief" in instance or "chief" in task:
        return "chief"
    return "other"


def summarize(result: Result) -> dict:
    by_stage = Counter(stage_of(instance, action) for instance, _, action in result.sessions)
    by_task = Counter(f"{agent}:{action}" for _, agent, action in result.sessions)
    chain_stages = [stage_of(*label.split(":", 1)) for label in result.chain]
    return {
        "serial_llm_depth": result.depth,
        "llm_sessions": len(result.sessions),
        "sessions_by_stage": dict(by_stage),
        "sessions_by_task": dict(sorted(by_task.items())),
        "critical_chain": result.chain,
        "critical_chain_stages": dict(Counter(chain_stages)),
        "request_input": result.inputs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--workflows", type=Path, default=WORKFLOWS)
    parser.add_argument("--entry", default="full-report")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = {"provider_calls": 0, "entry": args.entry, "workflows": str(args.workflows), "scenarios": {}}
    for name, rounds in SCENARIOS.items():
        result = Simulator(args.workflows, scenario(**rounds)).run(args.entry)
        report["scenarios"][name] = {"rounds": rounds, **summarize(result)}
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    for name, item in report["scenarios"].items():
        print(f"{name:24s} depth={item['serial_llm_depth']:3d} sessions={item['llm_sessions']:3d} "
              f"{item['sessions_by_stage']}")


if __name__ == "__main__":
    main()
