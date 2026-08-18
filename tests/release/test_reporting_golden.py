import hashlib
import json
import shutil
from pathlib import Path

from manyselves.application.reporting_facade import ReportingFacade


def _seed(workspace: Path) -> None:
    root = workspace / "Work" / "runs" / "run-v1"
    root.mkdir(parents=True)
    (root / "request.json").write_text('{"requirement":"legacy"}\n', encoding="utf-8")
    (root / "workflow-state.json").write_text('{"activity":"delivery","status":"completed"}\n', encoding="utf-8")
    output = workspace / "Outputs" / "Reports" / "report.md"
    output.parent.mkdir(parents=True)
    output.write_text("# Verified report\n", encoding="utf-8")
    (workspace / "Work" / "runs" / "run-v1.json").write_text(json.dumps({
        "output_paths": ["Outputs/Reports/report.md"], "status": "completed",
    }) + "\n", encoding="utf-8")


def _semantic(workspace: Path) -> dict:
    snapshot = ReportingFacade(workspace, None).snapshot("run-v1")
    return {
        "run": snapshot["run"],
        "state": snapshot["state"],
        "outputs": snapshot["outputs"],
        "artifact": hashlib.sha256((workspace / "Outputs/Reports/report.md").read_bytes()).hexdigest(),
    }


def test_old_workspace_reporting_state_and_verified_outputs_are_service_compatible(tmp_path: Path) -> None:
    fixture = tmp_path / "workspace-v1"
    _seed(fixture)
    legacy = tmp_path / "legacy"
    service = tmp_path / "service"
    shutil.copytree(fixture, legacy)
    shutil.copytree(fixture, service)
    assert _semantic(service) == _semantic(legacy)
