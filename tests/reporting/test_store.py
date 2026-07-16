import pytest

from manyselves.core.reporting.agentic_models import SourceKind, SourceRecord
from manyselves.core.reporting.store import ReportingStore


def _source() -> SourceRecord:
    return SourceRecord(
        id="R-001",
        kind=SourceKind.LOCAL_REFERENCE,
        title="参考资料",
        locator="Knowledge/参考/a.md",
    )


def test_write_run_model_stays_beneath_run_directory(tmp_path):
    store = ReportingStore(tmp_path)
    path = store.write_run_model("run-1", "ledgers/sources/R-001.json", _source())
    assert path == tmp_path / "Work/runs/run-1/ledgers/sources/R-001.json"
    assert '"id": "R-001"' in path.read_text(encoding="utf-8")


def test_write_run_model_rejects_empty_run_id(tmp_path):
    store = ReportingStore(tmp_path)

    with pytest.raises(ValueError, match="run path"):
        store.write_run_model("", "source.json", _source())

    assert not (tmp_path / "Work/runs/source.json").exists()


@pytest.mark.parametrize("relative", ["../escape.json", "/tmp/escape.json"])
def test_write_run_model_rejects_parent_traversal_and_absolute_paths(tmp_path, relative):
    store = ReportingStore(tmp_path)

    with pytest.raises(ValueError, match="run path"):
        store.write_run_model("run-1", relative, _source())


def test_write_run_model_rejects_symlink_escape(tmp_path):
    workspace = tmp_path / "workspace"
    outside = tmp_path / "outside"
    run_directory = workspace / "Work/runs/run-1"
    run_directory.mkdir(parents=True)
    outside.mkdir()
    (run_directory / "ledgers").symlink_to(outside, target_is_directory=True)

    store = ReportingStore(workspace)
    with pytest.raises(ValueError, match="run path"):
        store.write_run_model("run-1", "ledgers/R-001.json", _source())

    assert not (outside / "R-001.json").exists()
