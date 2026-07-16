from pathlib import Path

import pytest

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
    assert second.parent_version_id == first.version_id
    assert store.latest().version_id == "version-002"
    assert [item.version_id for item in store.list_versions()] == [
        "version-001",
        "version-002",
    ]
    assert not list((tmp_path / "Work/report-versions").glob(".latest-*.tmp"))


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
