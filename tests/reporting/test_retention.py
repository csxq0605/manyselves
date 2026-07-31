import json

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
