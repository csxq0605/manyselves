"""Typed current-output ownership contract tests."""

import errno
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from docx import Document

from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.core.reporting.delivery import DeliveryPackage, ProjectDelivery
from manyselves.core.reporting.output_ownership import (
    CURRENT_OUTPUT_SET_REF,
    FINAL_REPORT_DOCX_REF,
    OUTPUT_ARTIFACT_REFS,
    OUTPUT_OWNER_REF,
    OutputOwner,
    OutputOwnerError,
    OutputOwnerStore,
    build_output_owner,
)
from manyselves.core.reporting.store import ReportingStore
from manyselves.core.reporting.versions import ReportVersionStore


def _seed_completed_publication(
    workspace: Path,
    *,
    run_id: str = "report-owner",
) -> tuple[OutputOwnerStore, OutputOwner]:
    output_root = workspace / "Outputs/Reports"
    module_root = workspace / "Outputs/Modules"
    output_root.mkdir(parents=True, exist_ok=True)
    module_root.mkdir(parents=True, exist_ok=True)
    final_markdown = workspace / f"Work/runs/{run_id}/report/配电安全专家咨询报告.md"
    final_markdown.parent.mkdir(parents=True, exist_ok=True)
    final_markdown.write_text("# report\n", encoding="utf-8")
    final_docx = workspace / FINAL_REPORT_DOCX_REF
    source_index_docx = output_root / "证据与来源索引.docx"
    Document().save(final_docx)
    Document().save(source_index_docx)
    report_state = workspace / "Work/report-state.json"
    report_state.parent.mkdir(parents=True, exist_ok=True)
    report_state.write_text("{}\n", encoding="utf-8")
    source_index = output_root / "证据与来源索引.md"
    source_index.write_text("# index\n", encoding="utf-8")
    module_files = {}
    for module_id in REPORT_MODULE_IDS:
        path = module_root / f"{module_id}.md"
        path.write_text(f"# {module_id}\n", encoding="utf-8")
        module_files[module_id] = path

    receipt = ProjectDelivery(
        workspace / "Work" / "runs" / run_id / "delivery"
    ).deliver(
        DeliveryPackage(
            report_id=run_id,
            version=run_id,
            module_files=module_files,
            final_docx=final_docx,
            report_state=report_state,
            source_index=source_index,
            source_index_docx=source_index_docx,
        )
    )
    receipt_ref = Path(f"Work/runs/{run_id}/delivery-receipt.json")
    receipt_path = ReportingStore(workspace).write_json(
        receipt_ref.as_posix(), receipt.model_dump(mode="json")
    )
    version = ReportVersionStore(workspace).publish_from_delivery(
        receipt,
        receipt_path,
        {"canonical_markdown": final_markdown},
        [],
        [],
    )
    owner = build_output_owner(
        run_id=run_id,
        report_version_id=version.version_id,
        final_docx_sha256=receipt.artifact_sha256["final_docx"],
        delivery_receipt_ref=receipt_ref,
        published_at=datetime(2026, 8, 9, tzinfo=timezone.utc),
    )
    return OutputOwnerStore(workspace), owner


def test_four_output_set_publishes_one_atomic_current_view(tmp_path: Path) -> None:
    store, legacy_owner = _seed_completed_publication(tmp_path)
    version = ReportVersionStore(tmp_path).load(legacy_owner.run_id)
    artifact_sha256 = {
        output_key: version.artifact_sha256[version_key]
        for output_key, version_key in {
            "final_markdown": "canonical_markdown",
            "final_docx": "final_docx",
            "source_index": "source_index",
            "source_index_docx": "source_index_docx",
        }.items()
    }
    owner = build_output_owner(
        run_id=legacy_owner.run_id,
        report_version_id=legacy_owner.report_version_id,
        final_docx_sha256=artifact_sha256["final_docx"],
        delivery_receipt_ref=legacy_owner.delivery_receipt_ref,
        artifact_sha256=artifact_sha256,
    )
    sources = {
        output_key: version.artifact_refs[version_key]
        for output_key, version_key in {
            "final_markdown": "canonical_markdown",
            "final_docx": "final_docx",
            "source_index": "source_index",
            "source_index_docx": "source_index_docx",
        }.items()
    }

    pointer = store.publish_output_set(owner, sources=sources)

    current = tmp_path / CURRENT_OUTPUT_SET_REF
    assert current.is_symlink()
    assert current.resolve() == (tmp_path / owner.output_set_ref).resolve()
    assert pointer == tmp_path / OUTPUT_OWNER_REF
    for key, public_ref in OUTPUT_ARTIFACT_REFS.items():
        visible = tmp_path / public_ref
        immutable = tmp_path / owner.output_set_ref / public_ref.name
        assert visible.is_symlink()
        assert immutable.is_symlink()
        assert visible.read_bytes() == immutable.read_bytes()
        assert store._sha256(visible) == artifact_sha256[key]
    assert store.require(expected_run_id=owner.run_id) == owner


def test_owner_publish_is_atomic_and_require_validates_all_bindings(
    tmp_path: Path,
) -> None:
    store, owner = _seed_completed_publication(tmp_path)

    pointer = store.publish(owner)
    replacement = owner.model_copy(
        update={"published_at": owner.published_at + timedelta(seconds=1)}
    )
    store.publish(replacement)

    assert pointer == tmp_path / OUTPUT_OWNER_REF
    assert store.load() == replacement
    assert store.require(
        expected_run_id=owner.run_id,
        expected_report_version_id=owner.report_version_id,
        expected_final_docx_sha256=owner.final_docx_sha256,
        expected_delivery_receipt_ref=owner.delivery_receipt_ref,
    ) == replacement
    published_at = json.loads(pointer.read_text(encoding="utf-8"))["published_at"]
    assert published_at.endswith(("Z", "+00:00"))
    assert list(pointer.parent.glob(f".{pointer.name}.*.tmp")) == []


def test_failed_owner_validation_does_not_replace_current_pointer(
    tmp_path: Path,
) -> None:
    store, owner = _seed_completed_publication(tmp_path)
    pointer = store.publish(owner)
    original = pointer.read_bytes()
    invalid = owner.model_copy(update={"final_docx_sha256": "0" * 64})

    with pytest.raises(OutputOwnerError, match="visible final DOCX"):
        store.publish(invalid)

    assert pointer.read_bytes() == original
    assert store.require(expected_run_id=owner.run_id) == owner


def test_owner_publish_propagates_directory_fsync_eio(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store, owner = _seed_completed_publication(tmp_path)

    def fail_directory_fsync(_path: Path) -> None:
        raise OSError(errno.EIO, "injected owner directory fsync failure")

    monkeypatch.setattr(store.store, "fsync_directory", fail_directory_fsync)

    with pytest.raises(OSError) as captured:
        store.publish(owner)

    assert captured.value.errno == errno.EIO


def test_owner_require_rejects_stale_visible_output_and_wrong_run(
    tmp_path: Path,
) -> None:
    store, owner = _seed_completed_publication(tmp_path)
    store.publish(owner)

    with pytest.raises(OutputOwnerError, match="run_id does not match"):
        store.require(expected_run_id="report-other")

    (tmp_path / FINAL_REPORT_DOCX_REF).write_bytes(b"stale output")
    with pytest.raises(OutputOwnerError, match="visible final DOCX"):
        store.require(expected_run_id=owner.run_id)


def test_owner_load_rejects_a_broken_symlink_pointer(tmp_path: Path) -> None:
    pointer = tmp_path / OUTPUT_OWNER_REF
    pointer.parent.mkdir(parents=True)
    pointer.symlink_to(tmp_path / "missing-owner.json")

    with pytest.raises(OutputOwnerError, match="not a regular file"):
        OutputOwnerStore(tmp_path).load()


def test_owner_rejects_internal_symlink_delivery_ancestor(tmp_path: Path) -> None:
    store, owner = _seed_completed_publication(tmp_path)
    store.publish(owner)
    delivery_root = tmp_path / f"Work/runs/{owner.run_id}/delivery"
    relocated = tmp_path / "Work/relocated-owner-delivery"
    delivery_root.rename(relocated)
    delivery_root.symlink_to(relocated, target_is_directory=True)

    with pytest.raises(OutputOwnerError, match="symbolic-link ancestor"):
        store.require(expected_run_id=owner.run_id)


@pytest.mark.parametrize("unsafe_run_id", [".", ".."])
def test_owner_rejects_dot_path_identities(unsafe_run_id: str) -> None:
    with pytest.raises(ValueError, match="safe path segment"):
        build_output_owner(
            run_id=unsafe_run_id,
            report_version_id=unsafe_run_id,
            final_docx_sha256="0" * 64,
            delivery_receipt_ref=Path(
                f"Work/runs/{unsafe_run_id}/delivery-receipt.json"
            ),
        )
