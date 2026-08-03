import json
import hashlib
import time
from pathlib import Path

import pytest
from docx import Document

from manyselves.core.reporting.output_verifier import (
    OutputVerificationError,
    verify_current_run_outputs,
)


def test_verifier_rejects_missing_and_outside_outputs(tmp_path: Path) -> None:
    started = time.time_ns()
    with pytest.raises(OutputVerificationError):
        verify_current_run_outputs(tmp_path, "run", [], started)
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x", encoding="utf-8")
    with pytest.raises(OutputVerificationError):
        verify_current_run_outputs(tmp_path, "run", [outside], started)


def test_verifier_rejects_wrong_run_receipt(tmp_path: Path) -> None:
    output = tmp_path / "Outputs/report.txt"
    output.parent.mkdir(parents=True)
    output.write_text("report", encoding="utf-8")
    # Use the filesystem's own clock for this receipt-specific assertion. WSL
    # can report a freshly written file a few milliseconds behind time.time_ns,
    # which would exercise the independent stale-output guard first.
    started = output.stat().st_mtime_ns
    manifest = tmp_path / "Outputs/manifest.json"
    manifest.write_text(json.dumps({"version": "other"}), encoding="utf-8")
    receipt = tmp_path / "Work/runs/run/delivery-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"success": True, "manifest_path": str(manifest), "final_docx": str(output)}), encoding="utf-8")
    with pytest.raises(OutputVerificationError, match="current run"):
        verify_current_run_outputs(tmp_path, "run", [output], started)


def test_resume_verifier_accepts_inherited_artifact_inside_same_run(
    tmp_path: Path,
) -> None:
    review = tmp_path / "Work/runs/run/reviews/final-completion.json"
    review.parent.mkdir(parents=True)
    review.write_text('{"status":"completed"}', encoding="utf-8")
    started = time.time_ns()

    paths = verify_current_run_outputs(
        tmp_path,
        "run",
        [review],
        started,
        allow_existing_run_artifacts=True,
    )

    assert paths == [review]


def test_resume_verifier_still_rejects_stale_shared_output(tmp_path: Path) -> None:
    output = tmp_path / "Outputs/report.txt"
    output.parent.mkdir(parents=True)
    output.write_text("old report", encoding="utf-8")
    started = time.time_ns()

    with pytest.raises(OutputVerificationError, match="stale"):
        verify_current_run_outputs(
            tmp_path,
            "run",
            [output],
            started,
            allow_existing_run_artifacts=True,
        )


def test_verifier_accepts_hash_validated_restored_delivery_output(
    tmp_path: Path,
) -> None:
    output = tmp_path / "Outputs/report.txt"
    output.parent.mkdir(parents=True)
    output.write_text("restored report", encoding="utf-8")
    started = time.time_ns()

    paths = verify_current_run_outputs(
        tmp_path,
        "run",
        [output],
        started,
        allow_existing_artifacts=True,
    )

    assert paths == [output]


def test_verifier_checks_source_index_markdown_and_docx_delivery_hashes(
    tmp_path: Path,
) -> None:
    delivery = tmp_path / "Outputs/Reports/run"
    delivery.mkdir(parents=True)
    final_docx = delivery / "配电安全专家咨询报告.docx"
    source_index = delivery / "证据与来源索引.md"
    source_index_docx = delivery / "证据与来源索引.docx"
    Document().save(final_docx)
    source_index.write_text("## 证据与来源索引\n", encoding="utf-8")
    Document().save(source_index_docx)
    artifacts = {
        "final_docx": hashlib.sha256(final_docx.read_bytes()).hexdigest(),
        "source_index": hashlib.sha256(source_index.read_bytes()).hexdigest(),
        "source_index_docx": hashlib.sha256(source_index_docx.read_bytes()).hexdigest(),
    }
    manifest = delivery / "delivery-manifest.json"
    manifest.write_text(
        json.dumps({"version": "run", "artifacts": artifacts}),
        encoding="utf-8",
    )
    receipt = tmp_path / "Work/runs/run/delivery-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(
        json.dumps(
            {
                "success": True,
                "manifest_path": str(manifest),
                "final_docx": str(final_docx),
                "source_index": str(source_index),
                "source_index_docx": str(source_index_docx),
            }
        ),
        encoding="utf-8",
    )
    started = time.time_ns()

    paths = verify_current_run_outputs(
        tmp_path,
        "run",
        [final_docx, source_index, source_index_docx],
        started,
        allow_existing_artifacts=True,
    )
    assert paths == [final_docx, source_index, source_index_docx]

    source_index.write_text("tampered", encoding="utf-8")
    with pytest.raises(OutputVerificationError, match="manifest hash"):
        verify_current_run_outputs(
            tmp_path,
            "run",
            [final_docx, source_index, source_index_docx],
            started,
            allow_existing_artifacts=True,
        )
