import json
import hashlib
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.core.reporting.retention import ReportingRetentionPlanner


def test_retention_planner_reports_reuse_and_never_deletes(tmp_path) -> None:
    first_source = tmp_path / "Inputs/first.bin"
    second_source = tmp_path / "Inputs/second.bin"
    third_source = tmp_path / "Inputs/third.bin"
    first_source.parent.mkdir(parents=True)
    first_source.write_bytes(b"shared-content" * 100)
    second_source.write_bytes(b"orphan-content" * 50)
    third_source.write_bytes(b"manifest-only-content" * 25)
    content = ContentAddressedStore(tmp_path)
    referenced = content.ingest_file(first_source)
    unreferenced = content.ingest_file(second_source)
    manifest_only = content.ingest_file(third_source)
    view = tmp_path / "Work/runs/run-a/assets/photo.bin"
    content.link_view(referenced, view)
    manifest = tmp_path / "Work/runs/run-a/agent-conversations/agent.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(
        json.dumps({"transcript_ref": referenced.relative_path.as_posix()}),
        encoding="utf-8",
    )
    version_manifest = tmp_path / "Work/report-versions/version-a/version.json"
    version_manifest.parent.mkdir(parents=True)
    version_manifest.write_text(
        json.dumps(
            {
                "artifact_blob_refs": {
                    "final_docx": manifest_only.relative_path.as_posix()
                }
            }
        ),
        encoding="utf-8",
    )

    result = ReportingRetentionPlanner(tmp_path).generate(grace_days=0)

    assert result["plan"]["mode"] == "dry_run"
    assert result["plan"]["automatic_deletion"] is False
    assert result["usage"]["blob_count"] == 3
    assert result["usage"]["referenced_blob_count"] == 2
    assert result["usage"]["unreferenced_blob_count"] == 1
    assert result["usage"]["compatibility_view_count"] == 1
    assert result["usage"]["reused_bytes"] == len(first_source.read_bytes())
    assert result["plan"]["candidate_count"] == 1
    assert result["plan"]["candidates"][0]["blob_ref"] == (
        unreferenced.relative_path.as_posix()
    )
    assert referenced.path.is_file()
    assert unreferenced.path.is_file()
    assert (tmp_path / "Work/storage-usage.json").is_file()
    assert (tmp_path / "Work/retention-plan.json").is_file()


def test_read_only_preview_covers_mixed_manifests_and_protection_markers(
    tmp_path: Path,
) -> None:
    """Preview reports storage without touching a CAS inode or old manifest."""

    source = tmp_path / "Inputs/source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"cas-view" * 20)
    materialized = tmp_path / "Outputs/Reports/final.docx"
    materialized.parent.mkdir(parents=True)
    materialized.write_bytes(b"materialized-delivery" * 7)
    orphan_source = tmp_path / "Inputs/orphan.bin"
    orphan_source.write_bytes(b"old-orphan" * 4)
    protected_source = tmp_path / "Inputs/protected.bin"
    protected_source.write_bytes(b"old-protected" * 5)

    content = ContentAddressedStore(tmp_path)
    cas_view_blob = content.ingest_file(source)
    orphan_blob = content.ingest_file(orphan_source)
    protected_blob = content.ingest_file(protected_source)
    content.link_view(cas_view_blob, tmp_path / "Work/Outputs/final.docx")
    old_timestamp = (datetime.now(timezone.utc) - timedelta(days=30)).timestamp()
    for blob in (orphan_blob, protected_blob):
        os.utime(blob.path, (old_timestamp, old_timestamp))

    version_v1 = tmp_path / "Work/report-versions/v1/version.json"
    version_v1.parent.mkdir(parents=True)
    version_v1.write_text(
        json.dumps(
            {
                "storage_version": 1,
                "artifact_refs": {"final_docx": "Outputs/Reports/final.docx"},
            }
        ),
        encoding="utf-8",
    )
    version_v2 = tmp_path / "Work/report-versions/v2/version.json"
    version_v2.parent.mkdir(parents=True)
    version_v2.write_text(
        json.dumps(
            {
                "storage_version": 2,
                "artifact_blob_refs": {
                    "final_docx": cas_view_blob.relative_path.as_posix()
                },
            }
        ),
        encoding="utf-8",
    )
    version_v3 = tmp_path / "Work/report-versions/v3/version.json"
    version_v3.parent.mkdir(parents=True)
    version_v3.write_text(
        json.dumps(
            {
                "storage_version": 3,
                "artifact_storage": {"final_docx": "cas"},
                "artifact_refs": {
                    "final_docx": cas_view_blob.relative_path.as_posix()
                },
                "session_summary_blob_refs": [
                    cas_view_blob.relative_path.as_posix()
                ],
            }
        ),
        encoding="utf-8",
    )
    delivery_manifest = tmp_path / "Work/runs/run-1/delivery/delivery-manifest.json"
    delivery_manifest.parent.mkdir(parents=True)
    delivery_manifest.write_text(
        json.dumps(
            {
                "manifest_version": 3,
                "artifact_storage": {"final_docx": "materialized"},
                "artifact_refs": {"final_docx": "Outputs/Reports/final.docx"},
            }
        ),
        encoding="utf-8",
    )
    receipt = tmp_path / "Work/runs/run-1/delivery-receipt.json"
    receipt.write_text(
        json.dumps(
            {
                "storage_version": 3,
                "manifest_path": delivery_manifest.relative_to(tmp_path).as_posix(),
                "final_docx": "Outputs/Reports/final.docx",
                "artifact_refs": {"final_docx": "Outputs/Reports/final.docx"},
            }
        ),
        encoding="utf-8",
    )
    conversation = tmp_path / "Work/runs/run-1/agent-conversations/session.json"
    conversation.parent.mkdir(parents=True, exist_ok=True)
    conversation.write_text(
        json.dumps({"context_blob_ref": cas_view_blob.relative_path.as_posix()}),
        encoding="utf-8",
    )
    pinned = tmp_path / "Work/runs/run-2/.pinned"
    pinned.parent.mkdir(parents=True, exist_ok=True)
    pinned.write_text(
        json.dumps({"blob_ref": protected_blob.relative_path.as_posix()}),
        encoding="utf-8",
    )

    canonical_before = {
        path.relative_to(tmp_path).as_posix(): (
            path.stat().st_ino,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in (cas_view_blob.path, orphan_blob.path, protected_blob.path)
    }
    manifests_before = {
        path: path.read_bytes()
        for path in (version_v1, version_v2, version_v3, delivery_manifest, receipt)
    }

    result = ReportingRetentionPlanner(tmp_path).preview(
        grace_days=7,
        now=datetime.now(timezone.utc),
    )
    usage = result["usage"]
    candidates = {item["blob_ref"]: item for item in result["plan"]["candidates"]}

    assert usage["canonical_logical_bytes"] == sum(
        path.stat().st_size for path in (cas_view_blob.path, orphan_blob.path, protected_blob.path)
    )
    assert usage["compatibility_view_count"] == 1
    assert usage["view_logical_bytes"] == cas_view_blob.path.stat().st_size
    assert usage["reused_bytes"] == usage["view_logical_bytes"]
    assert usage["materialized_logical_bytes"] == materialized.stat().st_size
    assert usage["referenced_blob_bytes"] == cas_view_blob.path.stat().st_size
    assert usage["unreferenced_blob_bytes"] == (
        orphan_blob.path.stat().st_size + protected_blob.path.stat().st_size
    )
    assert usage["potential_reclaim_bytes"] == orphan_blob.path.stat().st_size
    assert candidates[orphan_blob.relative_path.as_posix()]["eligible_after_grace"] is True
    protected_candidate = candidates[protected_blob.relative_path.as_posix()]
    assert protected_candidate["active_or_pinned"] is True
    assert protected_candidate["eligible_after_grace"] is False
    assert protected_candidate["action"] == "preview_only"
    assert pinned.relative_to(tmp_path).as_posix() in protected_candidate[
        "reference_sources"
    ]

    canonical_after = {
        path.relative_to(tmp_path).as_posix(): (
            path.stat().st_ino,
            hashlib.sha256(path.read_bytes()).hexdigest(),
            path.stat().st_size,
        )
        for path in (cas_view_blob.path, orphan_blob.path, protected_blob.path)
    }
    assert canonical_after == canonical_before
    assert {path: path.read_bytes() for path in manifests_before} == manifests_before
    assert not (tmp_path / "Work/storage-usage.json").exists()
    assert not (tmp_path / "Work/retention-plan.json").exists()
