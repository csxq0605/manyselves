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
from typing import Any, Mapping

from .aggregate_existing import AggregateExistingTools
from .input_snapshot import RunInputSnapshotStore
from .models.agentic import ModuleSubmission
from .models.aggregate_existing import AggregateExistingPreparationInput
from .models.entrypoint import ReportingRunInitializerInput
from .models.inputs import (
    RequestedModuleChange,
    ReviewCompletionRecord,
    RevisionInputChanges,
    report_instruction_from_state,
)
from .models.module_lane import (
    DeclarativeModuleRevisionAgentResult,
    DeclarativeModuleRevisionPreparation,
    DeclarativeModuleRuntimeLaneContext,
)
from .models.preparation import PreparationContext
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
                json.dumps(_rebase_business(payload, old, new), ensure_ascii=False),
                encoding="utf-8",
            )
        else:
            path.write_text(_rebase_lines(text, old, new), encoding="utf-8")


def _rebase_business(value: Any, old: str, new: str) -> Any:
    """Old source references stay bound to archived old bytes after an input edit."""
    value = _rebase(value, f"{old}/baseline", f"{new}/baseline/inherited")
    value = _rebase(value, f"{old}/frozen-project", f"{new}/baseline/frozen-project")
    return _rebase(value, old, new)


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
        out.append(json.dumps(_rebase_business(payload, old, new), ensure_ascii=False) + ("\n" if line.endswith("\n") else ""))
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
    modules = _rebase_business(
        {key: ModuleSubmission.model_validate(raw).model_dump(mode="json")
         for key, raw in baseline["module_submissions"].items()},
        old,
        new,
    )
    current_inputs = snapshots.freeze(run_id)
    baseline_inputs = snapshots.load(baseline_id)
    previous_files = {item.logical_ref.as_posix(): item.sha256 for item in baseline_inputs.files}
    current_files = {item.logical_ref.as_posix(): item.sha256 for item in current_inputs.files}
    input_changes = {
        "added": sorted(current_files.keys() - previous_files.keys()),
        "removed": sorted(previous_files.keys() - current_files.keys()),
        "modified": sorted(key for key in current_files.keys() & previous_files.keys()
                           if current_files[key] != previous_files[key]),
    }
    # Successive revisions retain earlier archived bytes as well as this baseline.
    inherited = workspace / old / "baseline"
    if inherited.is_dir():
        _copy_tree_with_rebase(inherited, workspace / new / "baseline/inherited", old=old, new=new)
    # Retain the old bytes as provenance; the active snapshot belongs to this request.
    for item in baseline_inputs.files:
        target = workspace / new / "baseline/frozen-project" / item.logical_ref
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(workspace / item.snapshot_ref, target)
    # These directories contain data, not execution cursors, reviews, usage, or conversations.
    for name in ("assets", "indexes", "sources", "templates", "preparation", "source-index"):
        source = workspace / old / name
        if source.is_dir():
            target_name = "baseline/preparation" if name == "preparation" and any(input_changes.values()) else name
            _copy_tree_with_rebase(source, workspace / new / target_name, old=old, new=new)
    for name in ("evidence.jsonl", "photo-manifest.json", "context/photo-manifest.json", "ledgers/sources.json", "template-provenance.json"):
        source = workspace / old / name
        if source.is_file():
            target = workspace / new / name
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix == ".json":
                store.write_json(f"{new}/{name}", _rebase_business(json.loads(source.read_text(encoding="utf-8")), old, new))
            elif source.suffix == ".jsonl":
                target.write_text(_rebase_lines(source.read_text(encoding="utf-8"), old, new), encoding="utf-8")
            else:
                shutil.copyfile(source, target)
    state = _rebase_business({key: deepcopy(baseline[key]) for key in BUSINESS_KEYS if key in baseline}, old, new)
    # Keep baseline prose; only workspace path bindings move to the new Run.
    state["module_submissions"] = modules
    state.update(run_id=run_id, request=request.model_dump(mode="json"), resume=False,
                 requested_modules=request.target_modules, full_report=True,
                 baseline_run_id=baseline_id, revision_targets=dict(request.requested_changes))
    state.update(input_changes=input_changes,
                 input_snapshot_ref=f"{new}/input-snapshot.json",
                 input_snapshot_digest=current_inputs.inventory_digest)
    baseline_instruction = report_instruction_from_state(baseline)
    state["report_instruction"] = (
        f"原报告要求（与本轮冲突时以本轮要求为准）：\n{baseline_instruction}\n"
        f"本轮修订要求：\n{request.instruction}"
        if baseline_instruction else request.instruction
    )
    for key, module in modules.items():
        store.write_json(f"{new}/modules/{key}-r{module['revision']}.json", module)
    completion_refs = _materialize_baseline_module_review_completions(
        workspace=workspace,
        store=store,
        baseline=baseline,
        persisted=persisted,
        modules=modules,
        old=old,
        new=new,
        run_id=run_id,
    )
    if completion_refs:
        state["module_review_completion_refs"] = completion_refs
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


def _materialize_baseline_module_review_completions(
    *,
    workspace: Path,
    store: ReportingStore,
    baseline: Mapping[str, Any],
    persisted: Mapping[str, Any],
    modules: Mapping[str, Any],
    old: str,
    new: str,
    run_id: str,
) -> dict[str, str]:
    """Bind already-reviewed baseline modules into this Run for Cross local regression."""

    refs = dict(baseline.get("module_review_completion_refs") or {})
    # Older aggregate handoffs omitted these refs from the final output. Recover
    # only proof for the same module payload from a completed child in this Run.
    for child in persisted.get("subworkflow_states", {}).values():
        if child.get("status") != "completed":
            continue
        result = child.get("outputs", {}).get("result", {})
        if not isinstance(result, dict):
            continue
        for module_id, ref in result.get("module_review_completion_refs", {}).items():
            if result.get("module_submissions", {}).get(module_id) == baseline["module_submissions"].get(module_id):
                refs.setdefault(module_id, ref)

    archive_root = f"{new}/baseline/review-artifacts"
    copied: dict[str, str] = {}

    def copy_value(value: Any) -> Any:
        if isinstance(value, dict):
            return {key: copy_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [copy_value(item) for item in value]
        if isinstance(value, str):
            rebased = _rebase(value, old, archive_root)
            if rebased != value and (workspace / value).is_file():
                return copy_artifact(value)
            return rebased
        return value

    def copy_artifact(ref: str) -> str:
        source = (workspace / ref).resolve()
        relative = source.relative_to((workspace / old).resolve()).as_posix()
        if relative in copied:
            return copied[relative]
        target_ref = f"{archive_root}/{relative}"
        copied[relative] = target_ref
        if source.suffix.casefold() == ".json":
            payload = json.loads(source.read_text(encoding="utf-8"))
            store.write_json(target_ref, copy_value(payload))
        else:
            target = store.workspace / target_ref
            target.parent.mkdir(parents=True, exist_ok=True)
            if source.suffix.casefold() == ".jsonl":
                lines = [copy_value(json.loads(line)) for line in source.read_text(encoding="utf-8").splitlines() if line.strip()]
                store.write_jsonl(target_ref, lines)
            else:
                shutil.copyfile(source, target)
        return target_ref

    completed: dict[str, str] = {}
    for module_id, module in modules.items():
        revision = module.get("revision", 0)
        subject_ref = f"{new}/modules/{module_id}-r{revision}.json"
        source_ref = refs.get(module_id)
        if not source_ref:
            # Absence remains absence; Cross owns the existing missing-review error.
            continue
        original = ReviewCompletionRecord.model_validate_json(
            (workspace / source_ref).read_text(encoding="utf-8")
        )
        payload = original.model_dump(mode="json")
        for key in ("finding_refs", "verdict_refs"):
            payload[key] = [copy_artifact(ref) for ref in payload[key]]
        copy_artifact(source_ref)
        payload["run_id"] = run_id
        payload["subject_refs"] = [subject_ref]
        target_ref = f"{new}/reviews/module/baseline/{module_id}/completion-r{revision}.json"
        store.write_json(target_ref, payload)
        completed[module_id] = target_ref
    return completed


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
    context = AggregateExistingTools(workspace=store.workspace, input_snapshot=None, store=store).prepare_from_modules(
        AggregateExistingPreparationInput(run_id=state["run_id"], request=request),
        {key: ModuleSubmission.model_validate(raw) for key, raw in state["module_submissions"].items()},
        module_review_completion_refs=dict(state.get("module_review_completion_refs") or {}),
    )
    preparation_fields = PreparationContext.model_fields
    preparation = PreparationContext.model_validate({key: value for key, value in state.items() if key in preparation_fields})
    return context.model_copy(update={
        "report_instruction": report_instruction_from_state(state),
        "preparation_context": preparation,
        "revision_input_changes": RevisionInputChanges.model_validate(state["revision_input_changes"])
        if state.get("revision_input_changes") else None,
    })


def build_report_revision_tools(workspace: Path) -> dict[str, Any]:
    from .revision_inputs import build_revision_input_tools

    store = ReportingStore(workspace)
    return {
        **build_revision_input_tools(workspace),
        "prepare-report-revision": partial(prepare_report_revision, workspace=workspace),
        "module-has-requested-revision": module_has_requested_revision,
        "prepare-requested-module-revision": partial(prepare_requested_module_revision, store=store),
        "accept-requested-module-revision": partial(accept_requested_module_revision, store=store),
        "prepare-revision-aggregate": partial(prepare_revision_aggregate, store=store),
    }
