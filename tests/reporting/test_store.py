from autoreport.core.reporting.agentic_models import SourceKind, SourceRecord
from autoreport.core.reporting.store import ReportingStore


def test_write_run_model_stays_beneath_run_directory(tmp_path):
    store = ReportingStore(tmp_path)
    source = SourceRecord(
        id="R-001",
        kind=SourceKind.LOCAL_REFERENCE,
        title="参考资料",
        locator="Knowledge/01_页面导入知识库/a.md",
    )
    path = store.write_run_model("run-1", "ledgers/sources/R-001.json", source)
    assert path == tmp_path / "Work/runs/run-1/ledgers/sources/R-001.json"
    assert '"id": "R-001"' in path.read_text(encoding="utf-8")
