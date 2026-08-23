"""Focused characterization for the Capability preparation snapshot boundary."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from manyselves.capabilities.distribution_reporting.runtime.models.preparation import (
    ManifestFile,
    PreparationContext,
    ProjectManifest,
)
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    CoverageMatrix,
    EvidenceItem,
    PhotoAsset,
    ReportRequest,
    SourceLocation,
)
from manyselves.capabilities.distribution_reporting.runtime.preparation_snapshot import (
    persist_preparation_snapshot,
    restore_preparation_snapshot,
)
from manyselves.capabilities.distribution_reporting.runtime.storage import ReportingStore


def _context(run_id: str = "snapshot-run") -> PreparationContext:
    manifest = ProjectManifest(
        files=[
            ManifestFile(
                id="file-input",
                path=Path("Inputs/input.txt"),
                sha256="input-sha",
                media_type="text/plain",
                purpose="notes",
            )
        ]
    )
    evidence = EvidenceItem(
        id="E-001",
        subject="输入事实",
        fact="测试快照可恢复",
        source=SourceLocation(file_id="file-input", path=Path("Inputs/input.txt")),
        module_id="2.1",
        submodule_id="2.1.1",
    )
    photo = PhotoAsset(
        id="P-001",
        path=Path("Work/runs/snapshot-run/assets/P-001.png"),
        sha256="photo-sha",
        media_type="image/png",
        source_member="xl/media/image1.png",
        primary_evidence_id="E-001",
    )
    return PreparationContext(
        run_id=run_id,
        request=ReportRequest(instruction="snapshot characterization"),
        input_snapshot_ref=f"Work/runs/{run_id}/input-snapshot.json",
        input_snapshot_digest="input-digest",
        project_manifest=manifest,
        evidence_items=[evidence],
        photo_assets=[photo],
        photo_evidence_adjacency={"photo_to_evidence": {"P-001": ["E-001"]}},
        mapping_gaps=[{"code": "none", "message": ""}],
        coverage_matrix=CoverageMatrix(entries={}),
        report_taxonomy={"schema_version": 1, "modules": []},
        preparation_parallelism={"reducer_order": ["file-input"]},
    )


def test_preparation_snapshot_round_trip_uses_typed_files_and_completion_refs(
    tmp_path: Path,
) -> None:
    store = ReportingStore(tmp_path)
    original = _context()

    published = persist_preparation_snapshot(original, store)
    restored = restore_preparation_snapshot(
        PreparationContext(
            run_id=original.run_id,
            request=original.request,
            resume=True,
        ),
        store,
    )

    assert published.preparation_completion_ref == (
        "Work/runs/snapshot-run/preparation/completion.json"
    )
    assert restored.project_manifest == original.project_manifest
    assert restored.evidence_items == original.evidence_items
    assert restored.photo_assets == original.photo_assets
    assert restored.photo_evidence_adjacency == original.photo_evidence_adjacency
    assert restored.mapping_gaps == original.mapping_gaps
    assert restored.coverage_matrix == original.coverage_matrix
    assert restored.report_taxonomy == original.report_taxonomy
    assert restored.input_snapshot_ref == original.input_snapshot_ref
    assert restored.input_snapshot_digest == original.input_snapshot_digest
    assert restored.preparation_refs == published.preparation_refs

    completion = json.loads(
        (
            tmp_path
            / "Work/runs/snapshot-run/preparation/completion.json"
        ).read_text(encoding="utf-8")
    )
    assert set(completion) == {
        "schema_version",
        "run_id",
        "status",
        "input_snapshot_ref",
        "input_snapshot_digest",
        "preparation_refs",
        "manifest_order",
        "reducer_order",
        "evidence_count",
        "photo_count",
    }
    assert "preparation_sha256" not in completion


def test_staging_write_failure_does_not_publish_final_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ReportingStore(tmp_path)
    original_write_json = store.write_json

    def fail_photo_manifest(relative: str, value: object) -> Path:
        if str(relative).endswith("photo-manifest.json"):
            raise RuntimeError("injected staging write failure")
        return original_write_json(relative, value)

    monkeypatch.setattr(store, "write_json", fail_photo_manifest)

    with pytest.raises(RuntimeError, match="injected staging write failure"):
        persist_preparation_snapshot(_context("staging-failure"), store)

    run_root = tmp_path / "Work/runs/staging-failure"
    assert not (run_root / "preparation").exists()
    assert not list(run_root.glob(".preparation-*"))
