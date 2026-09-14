"""Single-baseline revision is a new execution, not a copied execution cursor."""

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import (
    RunInputSnapshotStore,
)
from manyselves.capabilities.distribution_reporting.runtime.models.agentic import ModuleSubmission
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
    ReportRequest,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def seed_baseline(workspace: Path) -> dict:
    store = ReportingStore(workspace)
    modules = {key: ModuleSubmission(
        module_id=key,
        submodule_narratives={sid: "原有正文。" for sid in spec.submodules},
        claims=[], source_ids=[], unresolved_questions=[], revision=2,
    ).model_dump(mode="json") for key, spec in REPORT_TAXONOMY.items()}
    state = {
        "run_id": "baseline", "request": {"instruction": "原请求"},
        "module_submissions": modules, "evidence_items": [], "photo_assets": [],
        "module_dispatch": {"old": "must not run"},
        "delivery_completion_ref": "old completion", "resume": True,
        "preparation_refs": {
            "evidence": "Work/runs/baseline/preparation/evidence.jsonl",
            "coverage": "Work/runs/baseline/preparation/coverage.json",
            "manifest": "Work/runs/baseline/preparation/manifest.json",
        },
        "preparation_completion_ref": "Work/runs/baseline/preparation/completion.json",
    }
    store.write_json("Work/runs/baseline/runtime-state.json", {
        "status": "completed", "variables": {"reporting-state-with-tail": state},
        "outputs": {"result": state},
    })
    store.write_json("Inputs/example.json", {"original": True})
    RunInputSnapshotStore(workspace).freeze("baseline")
    store.write_json("Work/runs/baseline/ledgers/sources.json", [])
    store.write_json("Work/runs/baseline/usage.json", {"old_cost": 9})
    store.write_json("Work/runs/baseline/preparation/evidence.jsonl", {"id": "E-0001"})
    store.write_json("Work/runs/baseline/preparation/coverage.json", {"ok": True})
    store.write_json("Work/runs/baseline/preparation/manifest.json", {"ok": True})
    store.write_json("Work/runs/baseline/preparation/completion.json", {"ok": True})
    for key in REPORT_TAXONOMY:
        store.write_json(f"Work/runs/baseline/modules/{key}-r2.json", modules[key])
    return state


def test_new_revision_copies_business_baseline_not_old_execution(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    original = seed_baseline(tmp_path)
    before = (tmp_path / "Work/runs/baseline/runtime-state.json").read_bytes()
    ReportingStore(tmp_path).write_json("Inputs/example.json", {"original": False})
    request = ReportRequest(operation="revise_report", instruction="仅澄清措辞",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "澄清原有措辞"})
    state = prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    assert state["run_id"] == "revision"
    assert state["module_submissions"] == original["module_submissions"]
    assert "module_dispatch" not in state and "delivery_completion_ref" not in state
    assert state["resume"] is False
    assert "原请求" in state["report_instruction"]
    assert "仅澄清措辞" in state["report_instruction"]
    assert not (tmp_path / "Work/runs/revision/usage.json").exists()
    snapshot = RunInputSnapshotStore(tmp_path).load("revision")
    assert json.loads((tmp_path / snapshot.resolve(Path("Inputs/example.json"))).read_text()) == {"original": False}
    baseline_snapshot = RunInputSnapshotStore(tmp_path).load("baseline")
    assert json.loads((tmp_path / baseline_snapshot.resolve(Path("Inputs/example.json"))).read_text()) == {"original": True}
    assert (tmp_path / "Work/runs/baseline/runtime-state.json").read_bytes() == before
    assert prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path) == state


def test_revision_rebases_module_path_bindings(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    store = ReportingStore(tmp_path)
    seed_baseline(tmp_path)
    baseline_state = json.loads(
        (tmp_path / "Work/runs/baseline/runtime-state.json").read_text(encoding="utf-8")
    )
    baseline = baseline_state["outputs"]["result"]
    baseline["photo_assets"] = [
        {
            "id": "P-0001",
            "path": "Work/runs/baseline/assets/file-x/P-0001.jpeg",
            "sha256": "a" * 64,
            "media_type": "image/jpeg",
            "source_member": "member",
        }
    ]
    baseline_state["outputs"]["result"] = baseline
    baseline_state["variables"]["reporting-state-with-tail"] = baseline
    store.write_json("Work/runs/baseline/runtime-state.json", baseline_state)
    (tmp_path / "Work/runs/baseline/assets/file-x").mkdir(parents=True, exist_ok=True)
    (tmp_path / "Work/runs/baseline/assets/file-x/P-0001.jpeg").write_bytes(b"jpeg")

    request = ReportRequest(
        operation="revise_report",
        instruction="仅澄清措辞",
        baseline_run_id="baseline",
        requested_changes={"2.3.1": "澄清原有措辞"},
    )
    state = prepare_report_revision(
        {"run_id": "revision", "request": request}, workspace=tmp_path
    )
    assert str(state["photo_assets"][0]["path"]).replace("\\", "/") == (
        "Work/runs/revision/assets/file-x/P-0001.jpeg"
    )
    assert (tmp_path / "Work/runs/revision/assets/file-x/P-0001.jpeg").is_file()
    prep_photo = tmp_path / "Work/runs/revision/preparation/photo-manifest.json"
    # seed_baseline does not create this file; create one with an absolute baseline path.
    prep_photo.parent.mkdir(parents=True, exist_ok=True)
    # Re-run prepare after planting a preparation manifest on the baseline.
    store.write_json(
        "Work/runs/baseline/preparation/photo-manifest.json",
        {
            "assets": [
                {
                    "id": "P-0009",
                    "path": str(tmp_path / "Work/runs/baseline/assets/file-x/P-0001.jpeg"),
                    "sha256": "b" * 64,
                    "media_type": "image/jpeg",
                    "source_member": "member",
                }
            ]
        },
    )
    (tmp_path / "Work/runs/revision/baseline/business-state.json").unlink()
    state = prepare_report_revision(
        {"run_id": "revision", "request": request}, workspace=tmp_path
    )
    rebased_prep = json.loads(prep_photo.read_text(encoding="utf-8"))
    path = str(rebased_prep["assets"][0]["path"]).replace("\\", "/")
    assert "/Work/runs/baseline/" not in path
    assert "Work/runs/revision/assets/file-x/P-0001.jpeg" in path


def test_revision_requires_explicit_valid_scope():
    with pytest.raises(ValueError):
        ReportRequest(operation="revise_report", instruction="改", baseline_run_id="../outside", requested_changes={"2.3.1": "改"})
    with pytest.raises(ValueError):
        ReportRequest(operation="revise_report", instruction="改", baseline_run_id="baseline", requested_changes={"9.9": "改"})
    request = ReportRequest(operation="revise_report", instruction="改", baseline_run_id="baseline", requested_changes={"2.3.1": "改"})
    assert request.target_modules == ["2.3"]


def test_revision_current_inputs_add_replace_and_freeze_once(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    seed_baseline(tmp_path)
    (tmp_path / "Inputs/example.json").write_text('{"version": 2}')
    (tmp_path / "Inputs/new.txt").write_text("ACCEPTANCE ONLY: new source", encoding="utf-8")
    request = ReportRequest(operation="revise_report", instruction="update inputs",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "use current facts"})
    state = prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    assert state["input_changes"] == {"added": ["Inputs/new.txt"], "modified": ["Inputs/example.json"], "removed": []}
    (tmp_path / "Inputs/new.txt").write_text("later edit", encoding="utf-8")
    snapshot = RunInputSnapshotStore(tmp_path).load("revision")
    assert (tmp_path / snapshot.resolve(Path("Inputs/new.txt"))).read_text(encoding="utf-8") == "ACCEPTANCE ONLY: new source"
    assert (tmp_path / "Work/runs/revision/baseline/frozen-project/Inputs/example.json").read_text(encoding="utf-8").strip() != '{"version": 2}'


def test_successive_revisions_keep_retired_source_bytes(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    seed_baseline(tmp_path)
    store = ReportingStore(tmp_path)
    store.write_json("Work/runs/baseline/sources/original.json", {
        "original_path": "Work/runs/baseline/frozen-project/Inputs/example.json",
    })
    store.write_json("Inputs/example.json", {"version": 2})
    request = ReportRequest(operation="revise_report", instruction="update",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "new facts"})
    first = prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    store.write_json("Work/runs/revision/runtime-state.json", {"status": "completed", "outputs": {"result": first}})
    store.write_json("Inputs/example.json", {"version": 3})
    request = request.model_copy(update={"baseline_run_id": "revision"})
    prepare_report_revision({"run_id": "revision-2", "request": request}, workspace=tmp_path)
    retired = json.loads((tmp_path / "Work/runs/revision-2/sources/original.json").read_text(encoding="utf-8"))
    assert "Work/runs/revision-2/" in retired["original_path"]
    assert json.loads((tmp_path / retired["original_path"]).read_text(encoding="utf-8")) == {"original": True}
    assert json.loads((tmp_path / "Work/runs/revision-2/baseline/frozen-project/Inputs/example.json").read_text(encoding="utf-8")) == {"version": 2}


@pytest.mark.asyncio
async def test_revision_evidence_preserves_ids_and_supplies_new_facts_to_author_and_cross(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.models.inputs import (
        RequestedModuleChange,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
        PreparationContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        EvidenceItem,
        SourceLocation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.module_revision_tools import (
        prepare_module_revision,
    )
    from manyselves.capabilities.distribution_reporting.runtime.revision_inputs import (
        reconcile_revision_evidence,
    )

    def evidence(eid, fact, file_id="file-old", cell="A1"):
        return EvidenceItem(id=eid, subject="ACCEPTANCE ONLY", fact=fact, module_id="2.3", submodule_id="2.3.1",
                            source=SourceLocation(file_id=file_id, path=Path("Inputs/example.xlsx"), cell=cell))
    old = [evidence("E-0008", "old fact"), evidence("E-0009", "unchanged", cell="A2")]
    store = ReportingStore(tmp_path)
    request = ReportRequest(operation="revise_report", instruction="update inputs", baseline_run_id="baseline",
                            requested_changes={"2.3.1": "use current facts"})
    store.write_json("Work/runs/revision/baseline/business-state.json", {
        "evidence_items": [item.model_dump(mode="json") for item in old],
        "input_changes": {"modified": ["Inputs/example.xlsx"], "added": [], "removed": []},
    })
    current = PreparationContext(run_id="revision", request=request,
                                 evidence_items=[evidence("E-0001", "new fact", "file-new"),
                                                 evidence("E-0002", "unchanged", "file-new", "A2")])
    prepared = reconcile_revision_evidence(current, workspace=tmp_path)
    assert [(item.id, item.fact) for item in prepared.evidence_items] == [("E-0010", "new fact"), ("E-0009", "unchanged")]
    assert prepared.evidence_items[1] == old[1]
    changes = json.loads((tmp_path / "Work/runs/revision/revision-input-changes.json").read_text(encoding="utf-8"))
    assert changes["superseded_evidence_ids"] == ["E-0008"]
    assert changes["current_evidence"][0]["evidence_id"] == "E-0010"
    module = ModuleSubmission(module_id="2.3", submodule_narratives={sid: "baseline" for sid in REPORT_TAXONOMY["2.3"].submodules}, claims=[], source_ids=[], unresolved_questions=[], revision=2)
    state = {"run_id": "revision", "evidence_items": prepared.evidence_items, "revision_input_changes": changes}
    revision = await prepare_module_revision(workspace=tmp_path, store=store, state=state, workflow_id="revise-report",
        subject=module, requested_changes=[RequestedModuleChange(id="USER-2.3.1", instruction="update", target_submodule_ids=["2.3.1"])])
    assert {item.evidence_id for item in revision.revision_input.evidence} == {"E-0009", "E-0010"}
    assert revision.revision_input.input_changes.superseded_evidence_ids == ["E-0008"]
    assert "new fact" in revision.revision_input.input_changes.current_evidence[0].content
    from manyselves.capabilities.distribution_reporting.runtime.cross_owner_runtime import (
        CrossOwnerRuntime,
    )

    modules = {key: ModuleSubmission(module_id=key, submodule_narratives={sid: "baseline" for sid in spec.submodules},
                                    claims=[], source_ids=[], unresolved_questions=[], revision=2)
               for key, spec in REPORT_TAXONOMY.items()}
    cross = CrossOwnerRuntime(tmp_path).prepare({**state, "module_submissions": modules})
    assert all(packet.input_changes.superseded_evidence_ids == ["E-0008"] for packet in cross["cross_owner_inputs"].values())
    from manyselves.capabilities.distribution_reporting.runtime.public_entrypoints import (
        load_public_entrypoint_definitions,
    )
    from manyselves.capabilities.distribution_reporting.runtime.revision_inputs import (
        attach_revision_preparation,
    )
    from manyselves.kernel.contracts import build_contract_catalog

    _, registry = load_public_entrypoint_definitions()
    attachment = {"state": {**state, "module_submissions": modules}, "preparation": prepared.model_dump(mode="json")}
    attachment = build_contract_catalog(registry)["revision_preparation_attachment"].validate(attachment)
    attached = attach_revision_preparation(attachment, workspace=tmp_path)
    assert attached["module_submissions"] == modules
    assert attached["revision_input_changes"] == changes
    assert [item.id for item in attached["evidence_items"]] == ["E-0010", "E-0009"]


def _seed_reviewed_baseline(workspace: Path, *, refs_in_child: bool = False):
    seed_baseline(workspace)
    store = ReportingStore(workspace)
    prefix = "Work/runs/baseline"
    findings_ref = f"{prefix}/reviews/module/cross-r1/2.1/findings-r0.json"
    verdicts_ref = f"{prefix}/reviews/module/cross-r1/2.1/verdicts-r1.json"
    completion_ref = f"{prefix}/reviews/module/cross-r1/2.1/completion-r2.json"
    subject_ref = f"{prefix}/modules/2.1-r2.json"
    store.write_json(findings_ref, {"subject_ref": subject_ref, "findings": [{"id": "original"}]})
    store.write_json(verdicts_ref, {"finding_refs": [findings_ref], "verdicts": [{"id": "original"}]})
    store.write_json(completion_ref, {
        "kind": "review_completion_record", "review_protocol_version": 2,
        "lifecycle": "module", "run_id": "baseline",
        "reviewer_agent_id": "evidence-auditor", "reviewer_session_key": "module-auditor-2.1",
        "subject_refs": [subject_ref], "finding_refs": [findings_ref],
        "verdict_refs": [verdicts_ref], "resolved_finding_ids": ["original"],
    })
    path = workspace / prefix / "runtime-state.json"
    persisted = json.loads(path.read_text(encoding="utf-8"))
    refs = {"2.1": completion_ref}
    if refs_in_child:
        persisted["subworkflow_states"] = {
            "run-cross": {"status": "completed", "outputs": {"result": {
                **persisted["outputs"]["result"], "module_review_completion_refs": refs,
            }}},
        }
    else:
        persisted["outputs"]["result"]["module_review_completion_refs"] = refs
    store.write_json(f"{prefix}/runtime-state.json", persisted)
    return completion_ref


@pytest.mark.parametrize("refs_in_child", [False, True])
def test_revision_preserves_review_proof_without_cross_path_collisions(tmp_path, refs_in_child):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    original_ref = _seed_reviewed_baseline(tmp_path, refs_in_child=refs_in_child)
    original_bytes = (tmp_path / original_ref).read_bytes()
    request = ReportRequest(operation="revise_report", instruction="clarify",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "clarify"})
    state = prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    completion = json.loads((tmp_path / state["module_review_completion_refs"]["2.1"]).read_text(encoding="utf-8"))
    assert completion["run_id"] == "revision"
    assert completion["subject_refs"] == ["Work/runs/revision/modules/2.1-r2.json"]
    assert completion["resolved_finding_ids"] == ["original"]

    # A fresh Cross round reuses these active paths with different findings.
    ReportingStore(tmp_path).write_json(
        "Work/runs/revision/reviews/module/cross-r1/2.1/findings-r0.json",
        {"findings": [{"id": "new-cross"}]},
    )
    finding = json.loads((tmp_path / completion["finding_refs"][0]).read_text(encoding="utf-8"))
    verdict = json.loads((tmp_path / completion["verdict_refs"][0]).read_text(encoding="utf-8"))
    assert finding["findings"] == [{"id": "original"}]
    assert (tmp_path / finding["subject_ref"]).is_file()
    assert verdict["finding_refs"] == completion["finding_refs"]
    assert (tmp_path / original_ref).read_bytes() == original_bytes


def test_revision_does_not_invent_missing_review_completion(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    seed_baseline(tmp_path)
    request = ReportRequest(operation="revise_report", instruction="clarify",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "clarify"})
    state = prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    assert not state.get("module_review_completion_refs")
    assert not list((tmp_path / "Work/runs/revision/reviews").rglob("completion-*.json"))


@pytest.mark.parametrize("broken", ["missing", "invalid-json"])
def test_revision_does_not_replace_unreadable_review_with_empty_completion(tmp_path, broken):
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_report_revision,
    )

    ref = _seed_reviewed_baseline(tmp_path)
    if broken == "missing":
        (tmp_path / ref).unlink()
    else:
        (tmp_path / ref).write_text("not JSON", encoding="utf-8")
    request = ReportRequest(operation="revise_report", instruction="clarify",
                            baseline_run_id="baseline", requested_changes={"2.3.1": "clarify"})
    with pytest.raises((OSError, ValueError)):
        prepare_report_revision({"run_id": "revision", "request": request}, workspace=tmp_path)
    assert not list((tmp_path / "Work/runs/revision/reviews").rglob("completion-*.json"))


def test_revision_aggregate_retains_current_module_review_references(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.aggregate_existing import (
        project_aggregate_existing_tail_state,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        EditedReportSubmission,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.aggregate_existing import (
        AggregateExistingHandoff,
    )
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        prepare_revision_aggregate,
    )

    state = seed_baseline(tmp_path)
    state["request"] = ReportRequest(operation="revise_report", instruction="clarify",
                                    baseline_run_id="original", requested_changes={"2.3.1": "clarify"})
    refs = {"2.1": "Work/runs/baseline/reviews/module/cross-r1/2.1/completion-r2.json"}
    state["module_review_completion_refs"] = refs
    state["report_instruction"] = "Keep the labelled acceptance example and use updated facts."
    state["input_snapshot_ref"] = "Work/runs/baseline/input-snapshot.json"
    context = prepare_revision_aggregate(state, store=ReportingStore(tmp_path))
    edited = EditedReportSubmission(
        title="Report", assessment_background="Background", findings_overview="Findings",
        regional_executive_summary="Summary", risk_panorama="Risk", dimension_risk_analysis="Analysis",
        data_gap_analysis="Gaps", improvement_action_plan="Actions",
        module_narratives={m: ModuleSubmission.model_validate(v).markdown for m, v in state["module_submissions"].items()},
    )
    tail = project_aggregate_existing_tail_state(AggregateExistingHandoff(context=context, edited_report=edited))
    assert tail["module_review_completion_refs"] == refs
    assert tail["input_snapshot_ref"] == state["input_snapshot_ref"]
    assert tail["preparation_refs"] == state["preparation_refs"]
    assert tail["full_report"] is True
    assert tail["report_instruction"] == state["report_instruction"]


@pytest.mark.asyncio
async def test_requested_module_revision_paths_keep_untouched_baseline(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.entrypoint_tools import (
        attach_module_results,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.agentic import (
        ModuleRevisionSubmission,
        RevisionResponse,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.module_lane import (
        DeclarativeModuleRevisionAgentResult,
    )
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        accept_requested_module_revision,
        module_has_requested_revision,
        prepare_report_revision,
        prepare_requested_module_revision,
        prepare_revision_aggregate,
    )
    from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore

    store = ReportingStore(tmp_path)
    original = seed_baseline(tmp_path)
    request = ReportRequest(
        operation="revise_report",
        instruction="仅澄清 2.3.1",
        baseline_run_id="baseline",
        requested_changes={"2.3.1": "澄清原有措辞并补一句可执行建议"},
    )
    state = prepare_report_revision(
        {"run_id": "revision", "request": request}, workspace=tmp_path
    )
    assert state["revision_targets"] == {"2.3.1": "澄清原有措辞并补一句可执行建议"}
    assert state["preparation_refs"]["evidence"].startswith("Work/runs/revision/")
    assert (tmp_path / "Work/runs/revision/preparation/evidence.jsonl").is_file()
    assert (tmp_path / "Work/runs/revision/ledgers/sources.json").is_file()

    context_23 = {
        "module_id": "2.3",
        "workflow_id": "revise-report",
        "reporting_state": state,
        "status": "ready",
    }
    context_21 = {**context_23, "module_id": "2.1"}
    assert module_has_requested_revision(context_23) is True
    assert module_has_requested_revision(context_21) is False

    prepared = await prepare_requested_module_revision(context_23, store=store)
    assert prepared.status == "revision_ready"
    assert prepared.revision is not None
    assert prepared.revision.prepared.target_submodule_ids == ["2.3.1"]
    assert prepared.revision.prepared.required_finding_ids == ["USER-2.3.1"]

    patch = ModuleRevisionSubmission(
        module_id="2.3",
        base_revision=2,
        revision=3,
        submodule_narratives={"2.3.1": "澄清后的正文。"},
        source_ids=[],
        revision_responses=[
            RevisionResponse(
                finding_id="USER-2.3.1",
                action="implemented",
                summary="已按用户要求仅澄清 2.3.1 正文并补上可执行建议。",
                changed_target_ids=["2.3.1"],
            )
        ],
    )
    accepted = accept_requested_module_revision(
        {
            "context": prepared.model_dump(mode="json"),
            "result": DeclarativeModuleRevisionAgentResult(
                status="completed", submission=patch
            ).model_dump(mode="json"),
        },
        store=store,
    )
    revised = accepted.module
    assert revised is not None
    assert revised.revision == 3
    assert revised.submodule_narratives["2.3.1"] == "澄清后的正文。"
    other = next(sid for sid in revised.submodule_narratives if sid != "2.3.1")
    assert revised.submodule_narratives[other] == original["module_submissions"]["2.3"]["submodule_narratives"][other]

    attached = attach_module_results(
        {
            "reporting_state": accepted.reporting_state,
            "module_results": {
                "2.3": revised.model_dump(mode="json"),
            },
        }
    )
    assert set(attached["module_submissions"]) == set(REPORT_MODULE_IDS)
    assert attached["module_submissions"]["2.1"] == original["module_submissions"]["2.1"]
    revised_attached = attached["module_submissions"]["2.3"]
    assert revised_attached.revision == 3
    assert revised_attached.submodule_narratives["2.3.1"] == "澄清后的正文。"

    aggregate = prepare_revision_aggregate(attached, store=store)
    assert set(aggregate.structured_modules) == set(REPORT_MODULE_IDS)
    assert aggregate.structured_modules["2.3"].revision == 3
    assert aggregate.structured_modules["2.1"].revision == 2
    assert aggregate.evidence_items == []
    assert aggregate.source_format == "structured_module"


def test_revision_photo_ids_keep_duplicate_occurrences_and_reserve_retired_ids(tmp_path):
    from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
        PreparationContext,
    )
    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        EvidenceItem,
        PhotoAsset,
        SourceLocation,
    )
    from manyselves.capabilities.distribution_reporting.runtime.revision_inputs import (
        reconcile_revision_evidence,
    )
    from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger

    def evidence(eid, photo, row):
        return EvidenceItem(id=eid, subject="fixture", fact=f"fact {row}", photo_refs=[photo],
                            source=SourceLocation(file_id="file", path=Path("Inputs/image.xlsx"), row=row))
    def photo(pid, eid):
        return PhotoAsset(id=pid, path=Path(f"Work/assets/{pid}.png"), sha256="a" * 64,
                          media_type="image/png", source_member="same-image.png", primary_evidence_id=eid)
    old = [evidence("E-0007", "P-0004", 1), evidence("E-0008", "P-0005", 2)]
    old_photos = [photo("P-0004", "E-0007"), photo("P-0005", "E-0008")]
    ReportingStore(tmp_path).write_json("Work/runs/revision/baseline/business-state.json", {
        "evidence_items": [item.model_dump(mode="json") for item in old],
        "photo_assets": [item.model_dump(mode="json") for item in old_photos], "input_changes": {},
    })
    retired = evidence("E-0099", "P-0088", 99)
    SourceLedger(tmp_path, "revision").register_many([dict(kind="project_evidence", evidence_id=retired.id,
        title=retired.subject, locator="Inputs/old.xlsx", content=retired.model_dump_json())])
    SourceLedger(tmp_path, "revision").register_many([dict(kind="project_evidence", evidence_id="E-0098",
        title="plain source", locator="Inputs/plain.txt", content="plain-text project-source content")])
    request = ReportRequest(operation="revise_report", instruction="update", baseline_run_id="baseline",
                            requested_changes={"2.3.1": "update"})
    current = PreparationContext(run_id="revision", request=request,
        evidence_items=[evidence(f"E-{i:04d}", f"P-{i:04d}", i) for i in (1, 2, 3)],
        photo_assets=[photo(f"P-{i:04d}", f"E-{i:04d}") for i in (1, 2, 3)])
    result = reconcile_revision_evidence(current, workspace=tmp_path)
    assert [item.id for item in result.evidence_items] == ["E-0007", "E-0008", "E-0100"]
    assert [item.id for item in result.photo_assets] == ["P-0004", "P-0005", "P-0089"]
    assert [item.primary_evidence_id for item in result.photo_assets] == ["E-0007", "E-0008", "E-0100"]
