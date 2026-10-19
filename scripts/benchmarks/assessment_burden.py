"""Net burden of author ``assessments`` and Chief ``observation_links`` (S06/Q05).

Measures, without a Provider:

* the tool/submit Schema each request carries with and without the fields;
* the output an author must generate to cover every observation of its
  module, built from the archived per-module fact packets (one record per
  observation, and grouped by submodule + identical source fact);
* the downstream copy every Chief/Final request receives as
  ``module_assessments``.

Records are synthetic but use the real subject, source coordinate and fact
text of each observation, with short template reasoning, so they are a lower
bound for a model that writes real reasoning.  Provider calls: 0.

    uv run --with tokenizers python scripts/benchmarks/assessment_burden.py \
        --tokenizer PATH [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import tempfile
from collections import defaultdict
from pathlib import Path

import yaml
from loguru import logger

ROOT = Path(__file__).resolve().parents[2]
CAP = ROOT / "manyselves/capabilities/distribution_reporting"
PACKETS = (ROOT / "docs/research/session-records/2026-10-10_to_2026-10-15/audit_manyselves"
           "/fact_packet_replay/Work/runs/replay/context")
MODULES = ("2.1", "2.2", "2.3", "2.4", "2.5")
logger.disable("manyselves")


def author_tools(task_name: str, *, with_assessments: bool) -> list[dict]:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
    from manyselves.capabilities.distribution_reporting.runtime.module_provider import (
        ModuleProviderDependencies,
        build_module_provider_tools,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore
    from manyselves.runtime.loops.bus import MessageBus

    task = yaml.safe_load((CAP / "tasks" / f"{task_name}.yaml").read_text(encoding="utf-8"))
    tmp = Path(tempfile.mkdtemp())

    class _Opaque:  # schema construction only
        pass

    registry = build_module_provider_tools(
        tmp, envelope=TaskEnvelope(task_id="t", run_id="r", agent_id=task["agent"], revision=0,
                                   objective="x", allowed_outputs=["module_submission"]),
        module_id="2.4", session_id="s", workflow_id="w", bus=MessageBus(),
        store=ReportingStore(tmp), global_knowledge_root=None, tool_names=task["tools"],
        expected_part_ids=["2.4.1.1"],
        dependencies=ModuleProviderDependencies(artifact_gateway=_Opaque(), artifact_access=_Opaque(),
                                                result_index=_Opaque()),
    )
    definitions = registry.get_definitions()
    if not with_assessments:
        for item in definitions:
            parameters = item.get("input_schema") or item.get("function", item).get("parameters", {})
            if parameters.get("properties", {}).pop("assessments", None) is not None:
                for name in ("ObservationAssessment", "AssessmentRuleBasis"):
                    parameters.get("$defs", {}).pop(name, None)
    return definitions


def chief_schema(kind: str, *, with_links: bool) -> dict:
    from manyselves.capabilities.distribution_reporting.runtime.contracts.submissions import (
        submission_schema,
    )

    schema = submission_schema(kind)
    if not with_links:
        schema["properties"].pop("observation_links", None)
        schema.get("$defs", {}).pop("ReportObservationLink", None)
    return schema


def observations(module_id: str) -> list[list]:
    text = (PACKETS / f"module-{module_id}-facts.md").read_text(encoding="utf-8")
    return [json.loads(line) for line in text.splitlines() if line.startswith('["E-')]


def compact_contract() -> bool:
    """True once the contract makes E-*-derived scope and prose-stated basis optional."""

    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        ObservationAssessment,
    )

    fields = ObservationAssessment.model_fields
    return not (fields["scope"].is_required() or fields["basis"].is_required())


def record(group: list[list], compact: bool) -> dict:
    eid, submodule, subject, fact, coordinate, extra = group[0]
    coordinate = coordinate or {}
    location = coordinate.get("cell") or coordinate.get("page") or ""
    concern = bool(re.search(r"NG|异常|缺|超|不合格|未", fact))
    unknown = bool((extra or {}).get("needs_confirmation"))
    item = {
        "evidence_ids": [item[0] for item in group],
        "disposition": "concern" if concern else "no_concern",
        "rules": [],
        "narrative_anchor": subject.split("/")[-1][:16],
        "action_anchor": "建议补充资料并复核" if concern else "",
        **({} if concern else {"no_action_reason": "资料齐备且有效，暂无需处置。"}),
    }
    if not compact:
        # Required fields: object/location/time and the fact are restated.
        item["scope"] = f"{subject}；{location}；收资/检测时点"
        item["basis"] = f"{fact[:60]}；按现有资料判断，适用工况与额定基准未在资料中给出。"
    elif unknown:
        # Only what the prose and E-* record do not already state.
        item["basis"] = "适用工况与额定基准未在资料中给出。"
    return item


def records_for(module_id: str, grouped: bool, compact: bool) -> dict[str, list[dict]]:
    by_part: dict[str, list[dict]] = defaultdict(list)
    groups: dict[tuple, list] = defaultdict(list)
    for item in observations(module_id):
        key = (item[1], item[3]) if grouped else (item[0],)
        groups[key].append(item)
    for group in groups.values():
        by_part[group[0][1]].append(record(group, compact))
    return by_part


def downstream_context(grouped: bool, compact: bool) -> str:
    """Production Chief/Final ``module_assessments`` for synthetic submissions."""

    from manyselves.capabilities.distribution_reporting.runtime.assessment_context import (
        assessment_source_context,
    )

    return assessment_source_context(synthetic_modules(grouped, compact))


def synthetic_modules(grouped: bool, compact: bool) -> dict:
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        REPORT_TAXONOMY,
        ModuleSubmission,
        PartAssessment,
    )

    modules = {}
    for module_id in MODULES:
        parts = records_for(module_id, grouped, compact)
        expected = defaultdict(list)
        for item in observations(module_id):
            expected[item[1]].append(item[0])
        narratives = {
            part_id: "正文。" + "".join(f"{r['narrative_anchor']}。{r['action_anchor']}。"
                                       for r in parts.get(part_id, []))
            for part_id in REPORT_TAXONOMY[module_id].submodules
        }
        modules[module_id] = ModuleSubmission(
            module_id=module_id, submodule_narratives=narratives, claims=[], source_ids=[],
            unresolved_questions=[], revision=0,
            assessments={part_id: PartAssessment(expected_evidence_ids=expected[part_id], records=records)
                         for part_id, records in parts.items()},
        )
    return modules


def links_for(records: dict[str, list[dict]]) -> list[dict]:
    links = []
    for part in records.values():
        for item in part:
            if item["disposition"] == "concern":
                links.append({"evidence_ids": item["evidence_ids"], "section_id": "3.2",
                              "narrative_anchor": item["narrative_anchor"],
                              "action_anchor": item["action_anchor"]})
    return links


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tokenizer", type=Path, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    from tokenizers import Tokenizer

    tokenizer = Tokenizer.from_file(str(args.tokenizer))

    def tokens(value) -> int:
        text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
        return len(tokenizer.encode(text).ids)

    report: dict = {"provider_calls": 0, "tokenizer": args.tokenizer.name}
    schemas = {}
    for task in ("module-2.4-authoring", "module-2.4-revision"):
        if not (CAP / "tasks" / f"{task}.yaml").is_file():
            continue
        on, off = (author_tools(task, with_assessments=flag) for flag in (True, False))
        schemas[task] = {"tools_tokens_with": tokens(on), "tools_tokens_without": tokens(off),
                         "added_per_request": tokens(on) - tokens(off)}
    for kind in ("chief_chapter_lane_submission", "chief_chapter_lane_revision_submission"):
        on, off = (chief_schema(kind, with_links=flag) for flag in (True, False))
        schemas[kind] = {"schema_tokens_with": tokens(on), "schema_tokens_without": tokens(off),
                         "added_per_request": tokens(on) - tokens(off)}
    report["schema_per_request"] = schemas

    compact = compact_contract()
    report["contract"] = "compact" if compact else "full"
    output = {}
    for grouped in (False, True):
        modules = {}
        all_links = 0
        for module_id in MODULES:
            parts = records_for(module_id, grouped, compact)
            links = links_for(parts)
            all_links += tokens(links)
            modules[module_id] = {
                "observations": len(observations(module_id)),
                "records": sum(len(items) for items in parts.values()),
                "assessment_output_tokens": sum(tokens(items) for items in parts.values()),
                "largest_part_output_tokens": max(tokens(items) for items in parts.values()),
                "concern_records": len(links),
            }
        output["grouped" if grouped else "per_observation"] = {
            "modules": modules,
            "author_output_tokens_total": sum(m["assessment_output_tokens"] for m in modules.values()),
            "author_output_tokens_max_module": max(m["assessment_output_tokens"] for m in modules.values()),
            "chief_links_output_tokens_total": all_links,
            "downstream_module_assessments_tokens_per_request": tokens(downstream_context(grouped, compact)),
        }
    report["output"] = output
    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
