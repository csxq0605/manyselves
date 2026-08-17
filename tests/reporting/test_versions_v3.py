import json
from pathlib import Path

import pytest

from manyselves.core.reporting.delivery import ProjectDelivery
from manyselves.core.reporting.versions import (
    ReportVersionStore,
    SkillProvenance,
)

from test_delivery import _package


def _skill() -> SkillProvenance:
    return SkillProvenance(
        skill_id="test.skill",
        version="1.0.0",
        sha256="a" * 64,
        scope="project",
    )


def _delivery(tmp_path: Path):
    workspace = tmp_path / "project"
    receipt = ProjectDelivery(workspace / "Outputs").deliver(_package(tmp_path))
    receipt_ref = workspace / "Work/runs/v1/delivery-receipt.json"
    receipt_ref.parent.mkdir(parents=True)
    receipt_ref.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")
    return workspace, receipt, receipt_ref


def test_publish_from_delivery_separates_version_views_and_reuses_cas(
    tmp_path: Path,
) -> None:
    workspace, receipt, receipt_ref = _delivery(tmp_path)
    extra = workspace / "Work/claims.json"
    extra.parent.mkdir(parents=True, exist_ok=True)
    extra.write_text('{"claim": "E-001"}\n', encoding="utf-8")

    store = ReportVersionStore(workspace)
    published = store.publish_from_delivery(
        receipt,
        receipt_ref,
        {"claim_ledger": extra},
        [],
        [_skill()],
    )

    assert published.storage_version == 3
    assert set(published.artifact_refs) == set(published.artifact_sha256)
    assert set(published.artifact_refs) == set(published.artifact_storage)
    assert set(published.artifact_blob_refs) == {
        key for key, mode in published.artifact_storage.items() if mode == "cas"
    }
    version_root = workspace / "Work/report-versions/report-001"
    for key, relative in published.artifact_refs.items():
        view = workspace / relative
        assert view.parent.parent == version_root
        assert view.is_file()
        if published.artifact_storage[key] == "materialized":
            assert not view.is_symlink()
    assert published.artifact_refs["source_index"].is_relative_to(
        Path("Work/report-versions/report-001")
    )
    assert "source_index" not in published.artifact_blob_refs
    assert store.content_store.metrics_snapshot()["cas_rehash_bytes"] == 0
    assert store.load("report-001") == published


def test_publish_from_delivery_rejects_typed_receipt_source_outside_delivery(
    tmp_path: Path,
) -> None:
    workspace, receipt, receipt_ref = _delivery(tmp_path)
    foreign = tmp_path / "foreign.docx"
    foreign.write_bytes(b"not-delivery")
    forged = receipt.model_copy(update={"final_docx": foreign})

    with pytest.raises(ValueError, match="outside its delivery directory"):
        ReportVersionStore(workspace).publish_from_delivery(
            forged,
            receipt_ref,
            {},
            [],
            [_skill()],
        )


def test_v3_manifest_rejects_blob_ref_for_materialized_source_index(
    tmp_path: Path,
) -> None:
    workspace, receipt, receipt_ref = _delivery(tmp_path)
    store = ReportVersionStore(workspace)
    published = store.publish_from_delivery(receipt, receipt_ref, {}, [], [_skill()])
    manifest = workspace / published.artifact_refs["source_index"]
    payload_path = workspace / "Work/report-versions/report-001/version.json"
    payload = json.loads(payload_path.read_text(encoding="utf-8"))
    payload["artifact_blob_refs"]["source_index"] = receipt.artifact_refs.get(
        "source_index", "Work/content/sha256/" + "0" * 64
    )
    payload_path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="blob manifest"):
        store.load("report-001")
    assert manifest.is_file()
