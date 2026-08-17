import json

from manyselves.core.reporting.research.project_evidence import ProjectEvidenceIndex


def test_evidence_snapshot_indexes_exact_complete_jsonl(tmp_path) -> None:
    run_id = "run-index"
    source = tmp_path / f"Work/runs/{run_id}/preparation/evidence.jsonl"
    source.parent.mkdir(parents=True)
    rows = [
        {
            "id": "E-001",
            "subject": "连接点",
            "fact": "完整温升记录",
            "source": {"file_id": "F-1", "path": "Inputs/a.xlsx"},
            "value": 86,
            "unit": "℃",
        },
        {
            "id": "E-002",
            "subject": "保护装置",
            "fact": "完整定值记录",
            "source": {"file_id": "F-2", "path": "Inputs/b.xlsx"},
        },
    ]
    source.write_text(
        "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows),
        encoding="utf-8",
    )
    index = ProjectEvidenceIndex(tmp_path, run_id)

    assert index.get("E-001").fact == "完整温升记录"
    assert [item.id for item in index.search("完整", limit=10)] == ["E-001", "E-002"]
    manifest_ref = index.snapshot_manifest_ref()
    manifest = json.loads((tmp_path / manifest_ref).read_text(encoding="utf-8"))
    assert manifest["item_count"] == 2
    assert manifest["source_bytes"] == len(source.read_bytes())
    assert [item["id"] for item in manifest["items"]] == ["E-001", "E-002"]
