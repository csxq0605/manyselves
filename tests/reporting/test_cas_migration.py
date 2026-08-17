"""Offline compatibility and non-destructive CAS migration checks.

These tests intentionally create the historical manifests in a temporary
workspace.  They exercise the current readers and retention preview without
calling a provider, touching a real run, or deleting content.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.core.reporting.delivery import ProjectDelivery
from manyselves.core.reporting.retention import ReportingRetentionPlanner
from manyselves.core.reporting.versions import ReportVersion, ReportVersionStore

from test_delivery import _package, _publish_legacy_v1, _publish_legacy_v2


@pytest.fixture
def migration_package(tmp_path: Path):
    """Use the same complete five-module package as the delivery contract."""

    return _package(tmp_path)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _cas_snapshot(workspace: Path) -> dict[str, object]:
    """Capture canonical blob count, inode, size, and bytes before a preview."""

    root = workspace / "Work/content/sha256"
    entries: dict[str, tuple[int, int, str]] = {}
    if root.is_dir():
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink() or len(path.name) != 64:
                continue
            relative = path.relative_to(workspace).as_posix()
            stat_result = path.stat()
            entries[relative] = (
                stat_result.st_ino,
                stat_result.st_size,
                _sha256(path),
            )
    return {"count": len(entries), "entries": entries}


def _manifest_snapshot(workspace: Path) -> dict[str, bytes]:
    """Read all lifecycle JSON manifests, including trusted-handle manifests."""

    if not workspace.is_dir():
        return {}
    return {
        path.relative_to(workspace).as_posix(): path.read_bytes()
        for path in sorted(workspace.rglob("*.json"))
        if path.is_file() and not path.is_symlink()
    }


def test_legacy_v1_delivery_load_does_not_migrate_or_rewrite(
    tmp_path: Path,
    migration_package,
) -> None:
    """A v1 materialized package is read as v1 and remains byte-for-byte old."""

    workspace = tmp_path / "project"
    delivery_root = workspace / "Outputs"
    destination = _publish_legacy_v1(delivery_root, migration_package)
    manifest = destination / "delivery-manifest.json"
    manifest_before = manifest.read_bytes()

    receipt = ProjectDelivery(delivery_root).deliver(migration_package)

    assert receipt.success is True
    assert receipt.storage_version == 1
    assert receipt.artifact_refs == {}
    assert all(
        mode == "materialized"
        for key, mode in receipt.artifact_storage.items()
        if key != "manifest"
    )
    assert manifest.read_bytes() == manifest_before
    assert not (workspace / "Work/content/sha256").exists()


def test_legacy_v2_all_cas_delivery_load_and_reuse_are_non_mutating(
    tmp_path: Path,
    migration_package,
) -> None:
    """An all-CAS v2 package loads through the compatibility path and reuses it."""

    workspace = tmp_path / "project"
    delivery_root = workspace / "Outputs"
    destination = _publish_legacy_v2(delivery_root, migration_package)
    manifest = destination / "delivery-manifest.json"
    manifest_before = manifest.read_bytes()
    cas_before = _cas_snapshot(workspace)

    delivery = ProjectDelivery(delivery_root)
    first = delivery.deliver(migration_package)
    resumed = delivery.deliver(migration_package)

    assert first.storage_version == 2
    assert resumed.storage_version == 2
    package_keys = set(first.artifact_sha256) - {"manifest"}
    assert set(first.artifact_refs) == package_keys
    assert set(first.trusted_handle_refs) == package_keys
    assert all(
        mode == "cas"
        for key, mode in first.artifact_storage.items()
        if key != "manifest"
    )
    assert resumed.delivery_dir == first.delivery_dir
    assert resumed.artifact_sha256 == first.artifact_sha256
    assert manifest.read_bytes() == manifest_before
    assert _cas_snapshot(workspace) == cas_before


def test_legacy_v1_and_v2_report_versions_load_without_rewrite(
    tmp_path: Path,
) -> None:
    """Loading old ReportVersion JSON never upgrades or rewrites its manifest."""

    workspace = tmp_path / "project"
    v1_root = workspace / "Work/report-versions/legacy-v1"
    v1_root.mkdir(parents=True)
    legacy_artifact = workspace / "Outputs/legacy-v1.txt"
    legacy_artifact.parent.mkdir(parents=True)
    legacy_artifact.write_text("legacy v1\n", encoding="utf-8")
    v1_payload = {
        "version_id": "legacy-v1",
        "run_id": "run-v1",
        "artifact_refs": {
            "artifact": legacy_artifact.relative_to(workspace).as_posix()
        },
        "artifact_sha256": {"artifact": _sha256(legacy_artifact)},
        "skill_provenance": [],
        "session_summary_refs": [],
    }
    v1_manifest = v1_root / "version.json"
    v1_manifest.write_text(
        json.dumps(v1_payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    v1_before = v1_manifest.read_bytes()

    # ReportVersionStore.publish is the historical all-CAS v2 writer.  Keep
    # that output as a legacy fixture and only use load below for migration
    # compatibility verification.
    v2_source = workspace / "Inputs/legacy-v2.bin"
    v2_source.parent.mkdir(parents=True)
    v2_source.write_bytes(b"legacy v2 CAS payload\n")
    store = ReportVersionStore(workspace)
    v2_published = store.publish(
        ReportVersion(
            version_id="legacy-v2",
            run_id="run-v2",
            artifact_refs={"artifact": v2_source.relative_to(workspace)},
            skill_provenance=[],
            session_summary_refs=[],
        )
    )
    assert v2_published.storage_version == 2
    v2_manifest = workspace / "Work/report-versions/legacy-v2/version.json"
    v2_before = v2_manifest.read_bytes()

    loaded_v1 = store.load("legacy-v1")
    loaded_v2 = store.load("legacy-v2")

    assert loaded_v1.storage_version == 1
    assert loaded_v2.storage_version == 2
    assert v1_manifest.read_bytes() == v1_before
    assert v2_manifest.read_bytes() == v2_before


def test_v3_mixed_delivery_and_version_load_resume_and_small_json_sha(
    tmp_path: Path,
    migration_package,
) -> None:
    """Mixed v3 views round-trip, resume idempotently, and keep small JSON local."""

    workspace = tmp_path / "project"
    delivery = ProjectDelivery(workspace / "Outputs")
    receipt = delivery.deliver(migration_package)
    manifest_before = receipt.manifest_path.read_bytes()
    receipt_ref = workspace / "Work/runs/run-v3/delivery-receipt.json"
    receipt_ref.parent.mkdir(parents=True)
    receipt_ref.write_text(receipt.model_dump_json(indent=2) + "\n", encoding="utf-8")

    small_json = workspace / "Work/runs/run-v3/small.json"
    small_json.write_text(
        '{"migration": "small-json", "keep_materialized": true}\n',
        encoding="utf-8",
    )
    small_digest = _sha256(small_json)

    store = ReportVersionStore(workspace)
    published = store.publish_from_delivery(
        receipt,
        receipt_ref,
        {"small_json": small_json},
        [],
        [],
    )
    version_manifest = workspace / "Work/report-versions/report-001/version.json"
    version_before = version_manifest.read_bytes()

    assert receipt.storage_version == 3
    assert {"cas", "materialized"}.issubset(set(receipt.artifact_storage.values()))
    assert published.storage_version == 3
    assert published.artifact_storage["small_json"] == "materialized"
    assert published.artifact_sha256["small_json"] == small_digest
    assert "small_json" not in published.artifact_blob_refs
    assert "small_json" not in published.artifact_trusted_handle_refs
    assert (workspace / published.artifact_refs["small_json"]).is_file()
    assert not list(
        (workspace / "Work/content/sha256").rglob(small_digest)
    )

    loaded = store.load(published.version_id)
    resumed = store.publish_from_delivery(
        receipt,
        receipt_ref,
        {"small_json": small_json},
        [],
        [],
    )

    assert loaded == published
    assert resumed == published
    assert receipt.manifest_path.read_bytes() == manifest_before
    assert version_manifest.read_bytes() == version_before


def test_old_cas_blob_is_referenced_and_retention_preview_is_read_only(
    tmp_path: Path,
    migration_package,
) -> None:
    """Retention sees v2 CAS refs and leaves every blob and manifest untouched."""

    workspace = tmp_path / "project"
    destination = _publish_legacy_v2(workspace / "Outputs", migration_package)
    cas = ContentAddressedStore(workspace)
    old_blob = cas.ingest_file(migration_package.final_docx)

    orphan_source = workspace / "Inputs/old-orphan.bin"
    orphan_source.parent.mkdir(parents=True, exist_ok=True)
    orphan_source.write_bytes(b"orphan payload that is not referenced\n")
    orphan_blob = cas.ingest_file(orphan_source)
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    os.utime(orphan_blob.path, (old_timestamp, old_timestamp))

    blobs_before = _cas_snapshot(workspace)
    manifests_before = _manifest_snapshot(workspace)

    result = ReportingRetentionPlanner(workspace).preview(
        grace_days=7,
        now=datetime.now(timezone.utc),
    )
    usage = result["usage"]
    candidate_refs = {item["blob_ref"] for item in result["plan"]["candidates"]}

    assert old_blob.relative_path.as_posix() not in candidate_refs
    assert orphan_blob.relative_path.as_posix() in candidate_refs
    assert usage["referenced_blob_count"] >= 1
    assert usage["blob_count"] == blobs_before["count"]
    assert destination.joinpath("delivery-manifest.json").read_bytes() == manifests_before[
        destination.joinpath("delivery-manifest.json")
        .relative_to(workspace)
        .as_posix()
    ]

    # ``preview`` is the dry-run API: no CAS inode, count, digest, or known
    # manifest may change while it reports the old v2 reference.
    assert _cas_snapshot(workspace) == blobs_before
    assert _manifest_snapshot(workspace) == manifests_before
