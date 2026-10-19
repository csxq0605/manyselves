"""Program steps around the joint (Cross) review.

* ``build-joint-dossiers`` runs before the joint review: per observed object,
  the facts and every module's judgments with prose excerpts
  (``reviews/joint-dossiers.json``).  The per-owner Cross lanes read these
  dossiers instead of the other modules' full text.
* ``consolidate-observation-ledger`` runs after it: ledger, program checks and
  the Chapter 3 relation context (``reviews/observation-ledger.json``) for
  Chief/Final.  When no joint review ran, it also writes an empty Cross
  completion/decision pack so Chief/Final/Delivery contracts stay intact.
"""

from __future__ import annotations

import json
from functools import partial
from typing import Any

from .assessment_context import source_observation_groups
from .cross_owner_runtime import (
    REPORT_MODULE_IDS,
    _metadata_map,
    _module_subject_ref,
    _module_submissions,
    _state_from,
)
from .joint_dossiers import build_joint_dossiers, dossiers_by_module
from .models.agentic import CrossDecisionPack
from .models.inputs import ReviewCompletionRecord
from .observation_ledger import build_observation_ledger, check_ledger, observation_relations
from .research.project_evidence import ProjectEvidenceIndex
from .storage import ReportingStore

LEDGER_REVIEWER_ID = "observation-ledger"


def build_dossiers(runtime, value: Any) -> dict:
    state = _state_from(value)
    run_id = state["run_id"]
    dossiers = build_joint_dossiers(runtime.store.workspace, run_id, _module_submissions(state))
    ref = f"Work/runs/{run_id}/reviews/joint-dossiers.json"
    runtime.store.write_json(ref, {
        "kind": "joint_dossiers",
        "run_id": run_id,
        "dossiers": dossiers,
    })
    state.update(
        joint_dossiers_ref=ref,
        joint_dossier_modules=dossiers_by_module(dossiers),
    )
    return state


def consolidate(runtime, value: Any) -> dict:
    state = _state_from(value)
    run_id = state["run_id"]
    workspace = runtime.store.workspace
    modules = _module_submissions(state)
    subject_refs = [
        _module_subject_ref(state, _metadata_map(state), modules[key])
        for key in REPORT_MODULE_IDS
    ]
    ledger = build_observation_ledger(workspace, run_id)
    index = ProjectEvidenceIndex(workspace, run_id)
    subjects = {item.id: item.subject for item in index.items()} if index.path.is_file() else {}
    checks = check_ledger(ledger, modules) if ledger is not None else {}
    checks["module_review_open"] = _module_review_open(runtime.store, state)
    payload = {
        "kind": "observation_ledger_consolidation",
        "run_id": run_id,
        "subject_refs": subject_refs,
        "ledger": ledger.to_json() if ledger is not None else None,
        "checks": checks,
        "relations": observation_relations(ledger, modules, subjects),
    }
    ledger_ref = f"Work/runs/{run_id}/reviews/observation-ledger.json"
    runtime.store.write_json(ledger_ref, payload)
    state["observation_ledger_ref"] = ledger_ref
    if state.get("cross_review_completion_ref"):
        return state  # The joint review already wrote its completion and synthesis.

    completion_ref = f"Work/runs/{run_id}/reviews/cross-completion.json"
    completion = ReviewCompletionRecord(
        review_protocol_version=3,
        lifecycle="cross",
        run_id=run_id,
        reviewer_agent_id=LEDGER_REVIEWER_ID,
        reviewer_session_key=LEDGER_REVIEWER_ID,
        subject_refs=subject_refs,
        finding_refs=[],
        verdict_refs=[],
        resolved_finding_ids=[],
        deferred_advisory_ids=[],
    )
    runtime.store.write_json(completion_ref, completion.model_dump(mode="json"))
    pack = CrossDecisionPack(
        version=2,
        run_id=run_id,
        module_ids=list(REPORT_MODULE_IDS),
        cross_review_completion_ref=completion_ref,
        synthesis_inputs=[],
        deferred_advisories=[],
    )
    pack_ref = f"Work/runs/{run_id}/reviews/cross-decision-pack.json"
    runtime.store.write_json(pack_ref, pack.model_dump(mode="json"))
    state.update(
        cross_review_completion_ref=completion_ref,
        cross_decision_pack_ref=pack_ref,
        cross_synthesis_inputs=[],
    )
    return state


def _module_review_open(store: ReportingStore, state: dict) -> list[dict]:
    """Module findings left open by the bounded rework, for Chief/Final."""

    found: list[dict] = []
    for module_id, ref in sorted(dict(state.get("module_review_completion_refs", {})).items()):
        path = store.workspace / str(ref)
        if not path.is_file():
            continue
        completion = ReviewCompletionRecord.model_validate_json(path.read_text(encoding="utf-8"))
        open_ids = set(completion.open_at_round_limit_ids)
        if not open_ids:
            continue
        for finding_ref in completion.finding_refs:
            finding_path = store.workspace / finding_ref
            if not finding_path.is_file():
                continue
            for finding in json.loads(finding_path.read_text(encoding="utf-8")).get("findings", []):
                if finding.get("id") in open_ids:
                    found.append({
                        "module_id": module_id,
                        "finding_id": finding["id"],
                        "target_submodule_id": finding.get("target_submodule_id"),
                        "observation": finding.get("observation"),
                    })
    return found


def ledger_source_context(
    state: dict, store: ReportingStore, *, relations: bool,
) -> dict[str, str]:
    """Chief/Final context from the consolidation artifact.

    Runs that still went through the old Cross path have no ledger artifact;
    they keep the plain routed-copy groups they had before.
    """

    ref = state.get("observation_ledger_ref")
    if not ref or not (store.workspace / ref).is_file():
        return {"source_observation_groups": json.dumps(
            source_observation_groups(store.workspace, state["run_id"]), ensure_ascii=False,
        )}
    payload = json.loads((store.workspace / ref).read_text(encoding="utf-8"))
    context = {
        "observation_ledger": json.dumps(payload.get("ledger"), ensure_ascii=False),
        "observation_ledger_checks": json.dumps(payload.get("checks"), ensure_ascii=False),
    }
    if relations:
        context["observation_relations"] = json.dumps(
            payload.get("relations", []), ensure_ascii=False,
        )
    return context


PORTS = {
    "build-joint-dossiers": build_dossiers,
    "consolidate-observation-ledger": consolidate,
}


def build_observation_consolidation_tools(runtime) -> dict:
    return {name: partial(function, runtime) for name, function in PORTS.items()}


__all__ = [
    "LEDGER_REVIEWER_ID",
    "build_dossiers",
    "build_observation_consolidation_tools",
    "consolidate",
    "ledger_source_context",
]
