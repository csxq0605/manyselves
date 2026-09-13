"""Single-step baseline copy and requested-patch boundaries for revise-report.

Action ordering, Agent turns, review, and delivery are owned by the YAML graph.
Only business carriers cross Run boundaries; execution history never does.
"""

from __future__ import annotations

import json
import shutil
from copy import deepcopy
from functools import partial
from pathlib import Path
from typing import Any

from .aggregate_existing import AggregateExistingTools
from .input_snapshot import RunInputSnapshotStore
from .models.agentic import ModuleSubmission
from .models.aggregate_existing import AggregateExistingPreparationInput
from .models.entrypoint import ReportingRunInitializerInput
from .models.inputs import RequestedModuleChange
from .models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from .models.reporting import REPORT_MODULE_IDS, ReportRequest
from .module_revision_tools import accept_module_revision, prepare_module_revision
from .storage import ReportingStore


BUSINESS_KEYS = (
    "module_submissions", "evidence_items", "photo_assets", "photo_evidence_adjacency",
    "project_manifest", "parsed_artifacts", "mapping_gaps", "coverage_matrix",
    "report_taxonomy", "special_topic_plan", "evidence_index_ref", "source_ledger_ref",
    "template_skill_refs", "template_skill_text", "template_skill_boundary",
    "module_knowledge_refs", "template_ref", "template_provenance", "input_snapshot_ref",
    "input_snapshot_digest", "edited_report",
    "preparation_refs", "preparation_completion_ref",
)


def _rebase(value: Any, old: str, new: str) -> Any:
    """Rewrite baseline Run path bindings, including absolute Windows paths."""

    old_slash = old.replace("\\", "/")
    if isinstance(value, dict):
        return {k: _rebase(v, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_rebase(v, old, new) for v in value]
    if isinstance(value, str):
        normalized = value.replace("\\", "/")
        marker = f"/{old_slash}/"
        index = normalized.find(marker)
        if index >= 0:
            return normalized[: index + 1] + new + normalized[index + len(marker) - 1 :]
        if normalized.startswith(old_slash + "/"):
            return new + normalized[len(old_slash) :]
    return value


def _copy_tree_with_rebase(source: Path, target: Path, *, old: str, new: str) -> None:
    shutil.copytree(source, target, dirs_exist_ok=True)
    # Evidence snapshots are content-addressed manifests; keep filenames but
    # drop copied bodies so the new Run regenerates them from its own evidence.jsonl.
    for snapshot in (target / "evidence").glob("*.json") if (target / "evidence").is_dir() else []:
        snapshot.unlink()
    for path in target.rglob("*"):
        if not path.is_file() or path.suffix.casefold() not in {".json", ".jsonl"}:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
        # JSON escapes Windows separators, so search both raw spellings.
        if "Work/runs" not in text and "Work\\runs" not in text and "Work\\\\runs" not in text:
            continue
        if path.suffix.casefold() == ".json":
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                continue
            path.write_text(
                json.dumps(_rebase(payload, old, new), ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            path.write_text(_rebase_lines(text, old, new), encoding="utf-8")


def _rebase_lines(text: str, old: str, new: str) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        stripped = line.strip()
        if not stripped:
            out.append(line)
            continue
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            out.append(line)
            continue
        out.append(json.dumps(_rebase(payload, old, new), ensure_ascii=False) + ("\n" if line.endswith("\n") else ""))
    return "".join(out)


def prepare_report_revision(value: Any, *, workspace: Path) -> dict[str, Any]:
    """Copy one baseline business snapshot, idempotently, into a fresh Run."""
    initializer = ReportingRunInitializerInput.model_validate(value)
    request, run_id = initializer.request, initializer.run_id
    snapshots = RunInputSnapshotStore(workspace)
    baseline_id = request.baseline_run_id
    if not baseline_id or run_id == baseline_id:
        raise ValueError("revision requires a different baseline Run")
    # Reuse the existing safe Run-component validation at the snapshot boundary.
    snapshots._manifest_path(run_id)
    old, new = f"Work/runs/{baseline_id}", f"Work/runs/{run_id}"
    store = ReportingStore(workspace)
    saved = workspace / new / "baseline/business-state.json"
    if saved.is_file():
        return json.loads(saved.read_text(encoding="utf-8"))
    persisted = json.loads((workspace / old / "runtime-state.json").read_text(encoding="utf-8"))
    candidates = [*persisted.get("outputs", {}).values(), *reversed(list(persisted.get("variables", {}).values()))]
    baseline = next((item for item in candidates if isinstance(item, dict)
                     and set(item.get("module_submissions", {})) == set(REPORT_MODULE_IDS)), None)
    if baseline is None:
        raise ValueError("baseline Run has no complete five-module business snapshot")
    modules = _rebase(
        {key: ModuleSubmission.model_validate(raw).model_dump(mode="json")
         for key, raw in baseline["module_submissions"].items()},
        old,
        new,
    )
    snapshots.fork(baseline_id, run_id)
    # These directories contain data, not execution cursors, reviews, usage, or conversations.
    for name in ("assets", "indexes", "sources", "templates", "preparation", "source-index"):
        source = workspace / old / name
        if source.is_dir():
            _copy_tree_with_rebase(source, workspace / new / name, old=old, new=new)
    for name in ("evidence.jsonl", "photo-manifest.json", "context/photo-manifest.json", "ledgers/sources.json", "template-provenance.json"):
        source = workspace / old / name
        if source.is_file():
            target = workspace / new / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".json":
                store.write_json(f"{new}/{name}", _rebase(json.loads(source.read_text(encoding="utf-8")), old, new))
            elif source.suffix == ".jsonl":
                target.write_text(_rebase_lines(source.read_text(encoding="utf-8"), old, new), encoding="utf-8")
            else:
                shutil.copyfile(source, target)
    state = _rebase({key: deepcopy(baseline[key]) for key in BUSINESS_KEYS if key in baseline}, old, new)
    # Keep baseline prose; only workspace path bindings move to the new Run.
    state["module_submissions"] = modules
    state.update(run_id=run_id, request=request.model_dump(mode="json"), resume=False,
                 requested_modules=request.target_modules, full_report=True,
                 baseline_run_id=baseline_id, revision_targets=dict(request.requested_changes))
    for key, module in modules.items():
        store.write_json(f"{new}/modules/{key}-r{module['revision']}.json", module)
    photo_assets = state.get("photo_assets") or []
    if photo_assets:
        store.write_json(
            f"{new}/context/photo-manifest.json",
            {"assets": photo_assets},
        )
    # Preserve the full old business payload for provenance, not as executable state.
    store.write_json(f"{new}/baseline/original-business-state.json", baseline)
    store.write_json(f"{new}/baseline/business-state.json", state)
    return state


def module_has_requested_revision(value: Any) -> bool:
    context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
    return context.status == "ready" and any(
        target.startswith(context.module_id + ".")
        for target in context.reporting_state.get("revision_targets", {})
    )


async def prepare_requested_module_revision(value: Any, *, store: ReportingStore):
    context = DeclarativeModuleRuntimeLaneContext.model_validate(value)
    state = context.reporting_state
    subject = ModuleSubmission.model_validate(state["module_submissions"][context.module_id])
    changes = [RequestedModuleChange(id=f"USER-{target}", instruction=instruction,
                                   target_submodule_ids=[target])
               for target, instruction in state["revision_targets"].items()
               if target.startswith(context.module_id + ".")]
    prepared = await prepare_module_revision(
        workspace=store.workspace, store=store, state=state, workflow_id=context.workflow_id,
        subject=subject, requested_changes=changes,
        user_supplements=state["request"].get("user_supplements", []),
    )
    return context.model_copy(update={"module": subject, "status": "revision_ready",
                                      "revision": DeclarativeModuleRevisionPreparation(prepared=prepared)})


def accept_requested_module_revision(value: Any, *, store: ReportingStore):
    context = DeclarativeModuleRuntimeLaneContext.model_validate(value["context"])
    result = DeclarativeModuleRevisionAgentResult.model_validate(value["result"])
    if result.status != "completed" or result.submission is None:
        raise ValueError(result.error or "requested revision did not complete")
    if context.revision is None:
        raise ValueError("requested revision preparation is missing")
    revised, _ref = accept_module_revision(workspace=store.workspace, store=store,
                                          preparation=context.revision.prepared, result=result.submission)
    state = deepcopy(context.reporting_state)
    state.setdefault("specialist_submissions", {})[context.module_id] = revised.model_dump(mode="json")
    return context.model_copy(update={"reporting_state": state, "module": revised,
                                      "status": "authored", "revision": None})


def prepare_revision_aggregate(value: Any, *, store: ReportingStore):
    """Project the revised modules plus all untouched baseline modules to the editor."""
    state = dict(value)
    request = ReportRequest.model_validate(state["request"])
    return AggregateExistingTools(workspace=store.workspace, input_snapshot=None, store=store).prepare_from_modules(
        AggregateExistingPreparationInput(run_id=state["run_id"], request=request),
        {key: ModuleSubmission.model_validate(raw) for key, raw in state["module_submissions"].items()},
    )


def build_report_revision_tools(workspace: Path) -> dict[str, Any]:
    store = ReportingStore(workspace)
    return {
        "prepare-report-revision": partial(prepare_report_revision, workspace=workspace),
        "module-has-requested-revision": module_has_requested_revision,
        "prepare-requested-module-revision": partial(prepare_requested_module_revision, store=store),
        "accept-requested-module-revision": partial(accept_requested_module_revision, store=store),
        "prepare-revision-aggregate": partial(prepare_revision_aggregate, store=store),
    }
