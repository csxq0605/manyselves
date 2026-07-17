import json
import time
from pathlib import Path

import pytest

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
    started = time.time_ns()
    output = tmp_path / "Outputs/report.txt"
    output.parent.mkdir(parents=True)
    output.write_text("report", encoding="utf-8")
    manifest = tmp_path / "Outputs/manifest.json"
    manifest.write_text(json.dumps({"version": "other"}), encoding="utf-8")
    receipt = tmp_path / "Work/runs/run/delivery-receipt.json"
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"success": True, "manifest_path": str(manifest), "final_docx": str(output)}), encoding="utf-8")
    with pytest.raises(OutputVerificationError, match="current run"):
        verify_current_run_outputs(tmp_path, "run", [output], started)
