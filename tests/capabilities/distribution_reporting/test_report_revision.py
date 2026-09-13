"""Single-baseline revision is a new execution, not a copied execution cursor."""

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.input_snapshot import RunInputSnapshotStore
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
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import prepare_report_revision

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
    assert not (tmp_path / "Work/runs/revision/usage.json").exists()
    snapshot = RunInputSnapshotStore(tmp_path).load("revision")
    assert json.loads((tmp_path / snapshot.resolve(Path("Inputs/example.json"))).read_text()) == {"original": True}
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


@pytest.mark.asyncio
async def test_requested_module_revision_paths_keep_untouched_baseline(tmp_path):
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
    from manyselves.capabilities.distribution_reporting.runtime.entrypoint_tools import (
        attach_module_results,
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
