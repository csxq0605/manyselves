from __future__ import annotations

from pathlib import Path

import pytest

from manyselves.core.reporting.artifact_ports import (
    CompareAndSwapPointerStore,
    FilesystemObjectStore,
    PortableWorkspaceMaterializer,
)


def test_no_shared_filesystem_worker_exchanges_only_artifact_refs(
    tmp_path: Path,
) -> None:
    objects = FilesystemObjectStore(tmp_path / "object-store")
    source_ref = objects.put(
        "runs/run-1/inputs/source.txt",
        b"portable",
        media_type="text/plain",
        project_lease_epoch=1,
    )
    materializer = PortableWorkspaceMaterializer(
        objects, tmp_path / "worker-temporary"
    )
    sandbox = materializer.materialize([source_ref])
    try:
        source = sandbox / "inputs" / source_ref.ref
        output = sandbox / "outputs/result.txt"
        output.write_text(source.read_text(encoding="utf-8").upper(), encoding="utf-8")
        output_ref = materializer.publish_output(
            sandbox,
            output,
            "runs/run-1/outputs/result.txt",
            media_type="text/plain",
            project_lease_epoch=2,
        )
    finally:
        materializer.cleanup(sandbox)

    assert objects.get(output_ref) == b"PORTABLE"
    assert output_ref.project_lease_epoch == 2
    assert not sandbox.exists()


def test_revision_pointer_rejects_wrong_revision_and_stale_fence(
    tmp_path: Path,
) -> None:
    objects = FilesystemObjectStore(tmp_path / "objects")
    first = objects.put("reports/v1.docx", b"v1", media_type="application/docx")
    second = objects.put("reports/v2.docx", b"v2", media_type="application/docx")
    pointers = CompareAndSwapPointerStore(tmp_path / "pointers")
    pointers.compare_and_swap(
        "latest",
        expected_revision=None,
        new_revision="v1",
        artifact=first,
        fencing_token=4,
    )

    with pytest.raises(RuntimeError, match="conflict"):
        pointers.compare_and_swap(
            "latest",
            expected_revision=None,
            new_revision="v2",
            artifact=second,
            fencing_token=5,
        )
    with pytest.raises(RuntimeError, match="stale"):
        pointers.compare_and_swap(
            "latest",
            expected_revision="v1",
            new_revision="v2",
            artifact=second,
            fencing_token=3,
        )
    updated = pointers.compare_and_swap(
        "latest",
        expected_revision="v1",
        new_revision="v2",
        artifact=second,
        fencing_token=5,
    )
    assert updated.revision == "v2"
