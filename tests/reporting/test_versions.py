import json
from pathlib import Path

import pytest

from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.core.reporting.versions import ReportVersion, ReportVersionStore, SkillProvenance


def _source_artifacts(root: Path, suffix: str = "one") -> dict[str, Path]:
    artifacts = {
        "final_docx": root / "Outputs" / f"report-{suffix}.docx",
        "report_state": root / "Work" / f"state-{suffix}.json",
        "claim_ledger": root / "Work" / f"claims-{suffix}.json",
        "source_ledger": root / "Work" / f"sources-{suffix}.json",
        "evidence": root / "Work" / f"evidence-{suffix}.jsonl",
        "delivery_receipt": root / "Work" / f"receipt-{suffix}.json",
    }
    for name, path in artifacts.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{name}:{suffix}", encoding="utf-8")
    return {name: path.relative_to(root) for name, path in artifacts.items()}


def _version(version_id: str, refs: dict[str, Path], parent: str | None = None) -> ReportVersion:
    return ReportVersion(
        version_id=version_id,
        run_id=f"run-{version_id}",
        parent_version_id=parent,
        artifact_refs=refs,
        skill_provenance=[
            SkillProvenance(
                skill_id="pds.module24.device-risk",
                version="1.0.0",
                sha256="a" * 64,
                scope="packaged",
            )
        ],
        session_summary_refs=[],
    )


def test_report_version_publish_snapshots_artifacts_and_keeps_parent_immutable(
    tmp_path: Path,
) -> None:
    store = ReportVersionStore(tmp_path)
    first_sources = _source_artifacts(tmp_path, "one")
    first = store.publish(_version("version-001", first_sources))
    (tmp_path / first_sources["report_state"]).write_text("mutated", encoding="utf-8")
    second = store.publish(
        _version("version-002", _source_artifacts(tmp_path, "two"), parent="version-001")
    )

    reloaded_first = store.load("version-001")
    first_state = tmp_path / reloaded_first.artifact_refs["report_state"]
    assert first_state.read_text(encoding="utf-8") == "report_state:one"
    assert reloaded_first.storage_version == 2
    assert first_state.suffix == ".json"
    assert second.parent_version_id == first.version_id
    assert store.latest().version_id == "version-002"
    assert [item.version_id for item in store.list_versions()] == [
        "version-001",
        "version-002",
    ]
    assert not list((tmp_path / "Work/report-versions").glob(".latest-*.tmp"))


def test_report_version_publish_is_idempotent_only_for_identical_sources(
    tmp_path: Path,
) -> None:
    refs = _source_artifacts(tmp_path, "resume")
    version = _version("version-resume", refs)
    store = ReportVersionStore(tmp_path)
    first = store.publish(version)

    resumed = store.publish(version)

    assert resumed == first
    (tmp_path / refs["report_state"]).write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="differs"):
        store.publish(version)


def test_report_version_rejects_missing_artifact_reference(tmp_path: Path) -> None:
    store = ReportVersionStore(tmp_path)
    version = _version("version-001", {"report_state": Path("Work/missing.json")})

    with pytest.raises(FileNotFoundError, match="missing.json"):
        store.publish(version)


def test_report_version_requires_existing_parent(tmp_path: Path) -> None:
    store = ReportVersionStore(tmp_path)
    version = _version("version-002", _source_artifacts(tmp_path), parent="version-does-not-exist")

    with pytest.raises(ValueError, match="parent report version"):
        store.publish(version)


def test_report_version_snapshots_session_summaries(tmp_path: Path) -> None:
    summary = tmp_path / "Work/runs/run-version/session-summaries/summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text('{"context_only": true}', encoding="utf-8")
    version = _version("version-001", _source_artifacts(tmp_path)).model_copy(
        update={"session_summary_refs": [summary.relative_to(tmp_path)]}
    )

    published = ReportVersionStore(tmp_path).publish(version)

    assert len(published.session_summary_refs) == 1
    snapshot = tmp_path / published.session_summary_refs[0]
    assert snapshot.read_text(encoding="utf-8") == '{"context_only": true}'
    assert "Work/report-versions/version-001/session-summaries" in snapshot.as_posix()
    assert published.storage_version == 2
    assert len(published.session_summary_blob_refs) == 1
    assert len(published.session_summary_sha256) == 1


def test_report_versions_reuse_blobs_across_immutable_views(tmp_path: Path) -> None:
    refs = _source_artifacts(tmp_path, "shared")
    store = ReportVersionStore(tmp_path)

    first = store.publish(_version("version-001", refs))
    second = store.publish(_version("version-002", refs, parent="version-001"))

    assert first.artifact_blob_refs == second.artifact_blob_refs
    assert first.artifact_sha256 == second.artifact_sha256
    assert first.artifact_refs != second.artifact_refs
    blob_files = [
        path for path in (tmp_path / "Work/content/sha256").rglob("*") if path.is_file()
    ]
    assert len(blob_files) == len(set(first.artifact_sha256.values()))
    for key, relative in first.artifact_refs.items():
        view = tmp_path / relative
        assert view.read_text(encoding="utf-8") == f"{key}:shared"


def test_report_version_preserves_suffix_when_source_is_already_a_cas_view(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Outputs/report.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx-content")
    content_store = ContentAddressedStore(tmp_path)
    blob = content_store.ingest_file(source)
    delivery_view = tmp_path / "Work/runs/run/delivery/report.docx"
    content_store.link_view(blob, delivery_view)
    version = _version(
        "version-001",
        {"final_docx": delivery_view.relative_to(tmp_path)},
    )

    published = ReportVersionStore(tmp_path).publish(version)

    version_view = tmp_path / published.artifact_refs["final_docx"]
    assert version_view.suffix == ".docx"
    assert version_view.read_bytes() == b"docx-content"
    assert published.artifact_blob_refs["final_docx"] == blob.relative_path


def test_report_version_reuses_delivery_trusted_handle_without_rehash(
    tmp_path: Path,
) -> None:
    source = tmp_path / "Outputs/report.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"already-verified-delivery")
    delivery_store = ContentAddressedStore(tmp_path)
    blob = delivery_store.ingest_file(source)
    handle = delivery_store.issue_trusted_handle(
        blob, lineage_id="delivery:report:v1:final_docx"
    )
    delivery_view = tmp_path / "Work/runs/run/delivery/report.docx"
    delivery_store.link_trusted_view(handle, delivery_view)
    version_store = ReportVersionStore(tmp_path)

    published = version_store.publish(
        _version(
            "version-trusted",
            {"final_docx": delivery_view.relative_to(tmp_path)},
        ),
        trusted_handle_refs={"final_docx": handle.manifest_ref},
    )

    assert published.artifact_trusted_handle_refs["final_docx"] == handle.manifest_ref
    assert version_store.content_store.metrics_snapshot()["cas_rehash_bytes"] == 0


def test_report_version_loads_legacy_v1_manifest_without_rewriting(
    tmp_path: Path,
) -> None:
    refs = _source_artifacts(tmp_path, "legacy")
    version = _version("version-legacy", refs)
    version_root = tmp_path / "Work/report-versions/version-legacy"
    version_root.mkdir(parents=True)
    payload = version.model_dump(mode="json")
    for key in (
        "storage_version",
        "artifact_blob_refs",
        "session_summary_blob_refs",
        "session_summary_sha256",
    ):
        payload.pop(key)
    manifest = version_root / "version.json"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    loaded = ReportVersionStore(tmp_path).load("version-legacy")

    assert loaded.storage_version == 1
    assert loaded.artifact_refs == refs
    assert loaded.artifact_blob_refs == {}
    assert json.loads(manifest.read_text(encoding="utf-8")) == payload


def test_report_version_rejects_v2_manifest_identity_mismatch(
    tmp_path: Path,
) -> None:
    store = ReportVersionStore(tmp_path)
    store.publish(_version("version-001", _source_artifacts(tmp_path)))
    manifest = tmp_path / "Work/report-versions/version-001/version.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["version_id"] = "version-from-another-directory"
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="identity does not match its directory"):
        store.load("version-001")


@pytest.mark.parametrize("view_kind", ("artifact", "session_summary"))
def test_report_version_rejects_cross_version_v2_views(
    tmp_path: Path,
    view_kind: str,
) -> None:
    summary = tmp_path / "Work/runs/run-version/session-summaries/summary.json"
    summary.parent.mkdir(parents=True)
    summary.write_text('{"context_only": true}', encoding="utf-8")
    refs = _source_artifacts(tmp_path, "shared-cross-version")
    store = ReportVersionStore(tmp_path)
    first = store.publish(
        _version("version-001", refs).model_copy(
            update={"session_summary_refs": [summary.relative_to(tmp_path)]}
        )
    )
    second = store.publish(
        _version("version-002", refs, parent="version-001").model_copy(
            update={"session_summary_refs": [summary.relative_to(tmp_path)]}
        )
    )
    manifest = tmp_path / "Work/report-versions/version-001/version.json"
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    if view_kind == "artifact":
        payload["artifact_refs"]["report_state"] = (
            second.artifact_refs["report_state"].as_posix()
        )
    else:
        payload["session_summary_refs"][0] = (
            second.session_summary_refs[0].as_posix()
        )
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValueError, match="outside its version directory"):
        store.load(first.version_id)


def test_report_version_rejects_uncontrolled_v2_view_symlink(
    tmp_path: Path,
) -> None:
    store = ReportVersionStore(tmp_path)
    published = store.publish(
        _version("version-001", _source_artifacts(tmp_path))
    )
    view = tmp_path / published.artifact_refs["report_state"]
    same_bytes = tmp_path / "Work/same-report-state.json"
    same_bytes.write_bytes(view.read_bytes())
    view.unlink()
    view.symlink_to(same_bytes)

    with pytest.raises(ValueError, match="not a controlled CAS symlink"):
        store.load("version-001")


def test_report_version_rejects_corrupted_v2_blob(tmp_path: Path) -> None:
    store = ReportVersionStore(tmp_path)
    published = store.publish(_version("version-001", _source_artifacts(tmp_path)))
    blob = tmp_path / published.artifact_blob_refs["report_state"]
    blob.chmod(0o644)
    blob.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="failed validation"):
        store.load("version-001")
