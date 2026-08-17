import hashlib
import os
from pathlib import Path

import pytest

from manyselves.core.artifacts.content_store import ContentAddressedStore
from manyselves.core.artifacts.storage_policy import CasPolicy, StorageMode


def test_content_store_reuses_one_blob_and_exposes_suffix_preserving_view(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    first = workspace / "Inputs/first.md"
    second = workspace / "Work/second.txt"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text("same immutable content", encoding="utf-8")
    second.write_text("same immutable content", encoding="utf-8")
    store = ContentAddressedStore(workspace)

    first_blob = store.ingest_file(first)
    second_blob = store.ingest_file(second)
    view = store.link_view(
        first_blob,
        workspace / "Work/views/report.md",
    )

    assert first_blob == second_blob
    assert len([path for path in store.root.rglob("*") if path.is_file()]) == 1
    assert view.path.suffix == ".md"
    assert view.path.read_text(encoding="utf-8") == "same immutable content"
    if view.mode == "symlink":
        assert view.path.resolve() == first_blob.path


def test_content_view_relative_link_survives_atomic_staging_move(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/report.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx-bytes")
    store = ContentAddressedStore(workspace)
    blob = store.ingest_file(source)
    staging = workspace / "Work/report-versions/.stage/version-001"
    destination = workspace / "Work/report-versions/version-001"
    staged_view = staging / "artifacts/report.docx"
    final_view = destination / "artifacts/report.docx"

    view = store.link_view(blob, staged_view, final_path=final_view)
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staging, destination)

    assert final_view.read_bytes() == b"docx-bytes"
    if view.mode == "symlink":
        assert final_view.resolve() == blob.path


def test_content_store_rejects_corrupted_existing_blob(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.txt"
    source.parent.mkdir(parents=True)
    source.write_text("trusted", encoding="utf-8")
    store = ContentAddressedStore(workspace)
    blob = store.ingest_file(source)
    blob.path.chmod(0o644)
    blob.path.write_text("tampered", encoding="utf-8")

    with pytest.raises(ValueError, match="failed validation"):
        store.ingest_file(source)


def test_ingest_hashes_staged_bytes_without_publishing_a_stale_source_digest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"A" * 4096)
    store = ContentAddressedStore(workspace)
    original_digest = store._digest

    def mutate_if_source_is_predigested(path: Path) -> tuple[str, int]:
        result = original_digest(path)
        if Path(path) == source:
            source.write_bytes(b"B" * 4096)
        return result

    monkeypatch.setattr(store, "_digest", mutate_if_source_is_predigested)
    blob = store.ingest_file(source)

    assert source.read_bytes() == b"A" * 4096
    assert blob.sha256 == hashlib.sha256(blob.path.read_bytes()).hexdigest()
    assert blob.path.name == blob.sha256


def test_content_store_rejects_refs_outside_cas(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    outside = workspace / "Work/not-a-blob.txt"
    outside.parent.mkdir(parents=True)
    outside.write_text("not a blob", encoding="utf-8")
    store = ContentAddressedStore(workspace)

    with pytest.raises(FileNotFoundError, match="missing or invalid"):
        store.resolve_blob(outside.relative_to(workspace))
    with pytest.raises(ValueError, match="project-relative"):
        store.resolve_blob(Path("../outside"))


def test_trusted_handle_avoids_rehash_inside_same_ingestion_lineage(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"verified-once" * 1024)
    store = ContentAddressedStore(workspace)
    blob = store.ingest_file(source)
    handle = store.issue_trusted_handle(blob, lineage_id="run-1:delivery")
    rehash_before = store.metrics_snapshot()["cas_rehash_bytes"]

    assert store.resolve_trusted_handle(handle) == blob.path
    view = store.link_trusted_view(
        handle,
        workspace / "Outputs/report.bin",
    )

    assert view.path.read_bytes() == source.read_bytes()
    metrics = store.metrics_snapshot()
    assert metrics["cas_rehash_bytes"] == rehash_before
    assert metrics["cas_trusted_handle_hits"] == 2


def test_trusted_handle_rejects_blob_changed_after_ingestion(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"trusted")
    store = ContentAddressedStore(workspace)
    blob = store.ingest_file(source)
    handle = store.issue_trusted_handle(blob, lineage_id="run-1:delivery")
    blob.path.chmod(0o644)
    blob.path.write_bytes(b"changed")

    with pytest.raises(ValueError, match="changed"):
        store.resolve_trusted_handle(handle)


def test_policy_materialized_copy_is_atomic_hashed_and_does_not_create_blob(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/state.json"
    destination = workspace / "Work/runs/run-1/state.json"
    source.parent.mkdir(parents=True)
    source.write_text('{"status":"ok"}\n', encoding="utf-8")
    store = ContentAddressedStore(workspace)

    result = store.persist_with_policy(
        source,
        destination=destination,
        logical_role="state",
    )

    assert result.storage_mode is StorageMode.MATERIALIZED
    assert result.blob_ref is None
    assert result.opaque_ref is None
    assert result.view_path == destination.resolve()
    assert destination.read_bytes() == source.read_bytes()
    assert result.sha256 == hashlib.sha256(source.read_bytes()).hexdigest()
    assert not store.root.exists()
    assert not list(destination.parent.glob(".*.materialized"))


def test_policy_cas_persists_blob_and_atomic_compatibility_view(tmp_path: Path) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.bin"
    destination = workspace / "Outputs/source.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"snapshot bytes")
    store = ContentAddressedStore(workspace)

    result = store.persist_with_policy(
        source,
        destination=destination,
        logical_role="input_snapshot",
        policy=CasPolicy(policy_version="g1-test"),
    )

    assert result.storage_mode is StorageMode.CAS
    assert result.policy_version == "g1-test"
    assert result.blob_ref is not None
    assert result.opaque_ref is None
    assert result.view_path == destination
    assert destination.read_bytes() == source.read_bytes()
    assert destination.is_symlink()
    assert store.resolve_blob(result.blob_ref, expected_sha256=result.sha256).is_file()


def test_policy_can_replace_an_existing_view_without_touching_canonical_blob(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "project"
    source = workspace / "Inputs/source.docx"
    destination = workspace / "Outputs/final.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx bytes")
    destination.parent.mkdir(parents=True)
    destination.write_bytes(b"old materialized bytes")
    store = ContentAddressedStore(workspace)

    result = store.persist_with_policy(
        source,
        destination=destination,
        logical_role="final_docx",
    )

    assert result.storage_mode is StorageMode.CAS
    assert destination.is_symlink()
    assert destination.read_bytes() == b"docx bytes"
    assert result.blob_ref is not None
    canonical = store.resolve_blob(result.blob_ref, expected_sha256=result.sha256)
    assert canonical.read_bytes() == b"docx bytes"
