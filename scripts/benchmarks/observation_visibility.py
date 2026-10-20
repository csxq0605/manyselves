"""Where a known historical omission first becomes visible, before vs now (Q01/Q04).

Replays three reviewed defects of the archived report through the current
production preparation functions, without a Provider:

* residual current E-0071 (caption cites it, narrative says all values OK);
* four SPD ``NG`` records (captions cite them, narrative confirms two);
* the last two thermal rows (never cited; each routed to 2.2 and 2.4).

The historical section text becomes the author submission: Claims bind every
E-ID the section mentions (as the old whole-part binding did).  Two author
behaviours for the judgment records are compared:

* ``narrative`` — records only for E-IDs the narrative itself discusses;
* ``grouped`` — one ``no_concern`` record over every cited E-ID (worst case).

"Before" is the archived run's contract (reviewer evidence = cited E-IDs, no
judgment records, no source-observation groups).  "Now" calls
``_review_evidence``, ``module_assessment_views`` and
``source_observation_groups``.  Visibility is not detection: a reviewer can
still miss a visible record.  Only IDs and booleans are exported.

    uv run python scripts/benchmarks/observation_visibility.py [--output out.json]
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

from loguru import logger

sys.path.insert(0, str(Path(__file__).resolve().parent))
from reporting_quality_replay import ARCHIVE, section  # noqa: E402

from manyselves.capabilities.distribution_reporting.runtime.assessment_context import (  # noqa: E402
    module_assessment_views,
    source_observation_groups,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (  # noqa: E402
    REPORT_TAXONOMY,
    ClaimRecord,
    ModuleSubmission,
    ObservationAssessment,
    PartAssessment,
)
from manyselves.capabilities.distribution_reporting.runtime.module_review_preparation import (  # noqa: E402
    _review_evidence,
)
from manyselves.capabilities.distribution_reporting.runtime.research.project_evidence import (  # noqa: E402
    ProjectEvidenceIndex,
)

logger.disable("manyselves")
EVIDENCE = ARCHIVE / "fact_packet_replay/Work/runs/replay/preparation/evidence.jsonl"
DEFECTS = {
    "residual_current_E-0071": ["E-0071"],
    "spd_four_NG": ["E-0088", "E-0115", "E-0133", "E-0142"],
    "thermal_last_two_rows": ["E-0230", "E-0231", "E-0232", "E-0233"],
}
MARKER = re.compile(r"现状(?:描述)?：")
EID = re.compile(r"(?<![\w-])E-\d{4}(?![\w-])")


def historical_part(report: str, part_id: str) -> tuple[str, set[str], set[str]]:
    body = section(report, part_id)
    match = MARKER.search(body)
    narrative = body[match.end():] if match else body
    return body, set(EID.findall(body)), set(EID.findall(narrative))


def submission(index, module_id, part_id, body, cited, recorded, disposition) -> ModuleSubmission:
    in_module = {item.id for item in index.module_items(module_id, {part_id})}
    bound = sorted(cited & in_module)
    narratives = {pid: "（未回放）" for pid in REPORT_TAXONOMY[module_id].submodules}
    narratives[part_id] = body + (" [[CLAIM:C-1]]" if bound else "")
    claims = [ClaimRecord(id="C-1", origin="runtime_part_binding", module_id=module_id,
                          submodule_id=part_id, text="历史正文", claim_type="project_fact",
                          source_ids=bound)] if bound else []
    records = [ObservationAssessment(evidence_ids=sorted(recorded & set(bound)), disposition=disposition,
                                     narrative_anchor=body[:8], no_action_reason="历史正文未给出处置。")]
    return ModuleSubmission(
        module_id=module_id, submodule_narratives=narratives, claims=claims, source_ids=bound,
        unresolved_questions=[], revision=0,
        assessments={part_id: PartAssessment(
            expected_evidence_ids=sorted(in_module),
            records=records if records[0].evidence_ids else [])},
    )


def replay() -> dict:
    report = (ARCHIVE / "配电安全专家咨询报告.docx.txt").read_text(encoding="utf-8")
    out: dict = {"provider_calls": 0, "defects": {}}
    with TemporaryDirectory(prefix="visibility-") as directory:
        workspace = Path(directory)
        snapshot = workspace / "Work/runs/replay/preparation/evidence.jsonl"
        snapshot.parent.mkdir(parents=True)
        snapshot.write_bytes(EVIDENCE.read_bytes())
        index = ProjectEvidenceIndex(workspace, "replay")
        groups = source_observation_groups(workspace, "replay") or []
        for name, ids in DEFECTS.items():
            rows = []
            for eid in ids:
                item = index.get(eid)
                module_id, part_id = item.module_id, item.submodule_id
                body, cited, narrated = historical_part(report, part_id)
                row = {
                    "evidence_id": eid, "part": part_id,
                    "cited_in_section": eid in cited, "discussed_in_narrative": eid in narrated,
                    "before": {"in_reviewer_evidence": eid in cited, "omission_signal": False,
                               "cross_route_link": False},
                    "now": {},
                }
                scope = {part_id}
                for behaviour, recorded, disposition in (("narrative", narrated, "concern"),
                                                         ("grouped", cited, "no_concern")):
                    subject = submission(index, module_id, part_id, body, cited, recorded, disposition)
                    evidence = {e.evidence_id: e for e in _review_evidence(workspace, subject, "replay", scope)}
                    view = module_assessment_views(subject, scope)[part_id]
                    row["now"][behaviour] = {
                        "in_reviewer_evidence": eid in evidence,
                        "reviewer_sees_unbound": evidence[eid].bound_submodule_ids == [] if eid in evidence else None,
                        # Same view reaches the module auditor, Chief and Final.
                        "unassessed_signal": eid in (view["unassessed_evidence_ids"] or []),
                        "recorded_as": disposition if eid in recorded and eid in cited else None,
                    }
                links = [g for g in groups if any(r["evidence_id"] == eid for r in g["routes"])]
                row["now"]["cross_route_link"] = [
                    {"routes": [r["evidence_id"] + "@" + r["submodule_id"] for r in g["routes"]],
                     "exact_routed_copy": g["exact_routed_copy"]} for g in links]
                rows.append(row)
            out["defects"][name] = rows
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    text = json.dumps(replay(), ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
