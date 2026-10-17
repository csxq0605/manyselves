"""Offline production-tool paging replay; never invokes a Provider."""
import asyncio
import json
import sys
from pathlib import Path
from uuid import uuid4

root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(root / "manyselves"))
from manyselves.capabilities.distribution_reporting.runtime.research.evidence_memory import EvidenceResearchMemory
from manyselves.capabilities.distribution_reporting.runtime.research_tools import SearchProjectEvidenceTool
from manyselves.capabilities.distribution_reporting.runtime.source_ledger import SourceLedger


async def main():
    source = root / "test/Work/runs/full-report-8c70828443b9499d80f8a6bda41c670c/preparation/evidence.jsonl"
    workspace = root / "audit_manyselves/pagination_replay"
    run_id = "offline-" + uuid4().hex[:12]
    snapshot = workspace / f"Work/runs/{run_id}/preparation/evidence.jsonl"
    snapshot.parent.mkdir(parents=True, exist_ok=True)
    snapshot.write_bytes(source.read_bytes())
    ledger = SourceLedger(workspace, run_id)
    metrics = {"provider_calls": 0, "source_run": source.parent.parent.name,
               "replay_run": run_id, "modules": {}, "checks": {}}
    all_hits = []
    for module in ("2.1", "2.2", "2.3", "2.4", "2.5"):
        memory = EvidenceResearchMemory(workspace, run_id, module)
        tool = SearchProjectEvidenceTool(workspace, run_id=run_id, evidence_memory=memory, ledger=ledger)
        pages, hits, offset = [], [], 0
        while True:
            page = await tool(query="", module_id=module, limit=12, offset=offset)
            assert not page["cache_hit"]
            hits.extend(page["hits"])
            pages.append(page)
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        ids = [hit["id"] for hit in hits]
        assert len(ids) == len(set(ids)) == pages[0]["total_matches"]
        resumed = SearchProjectEvidenceTool(
            workspace, run_id=run_id, evidence_memory=EvidenceResearchMemory(workspace, run_id, module),
        )
        for page in pages:
            cached = await resumed(**page["query_context"])
            assert cached["cache_hit"] and cached["hits"] == page["hits"]
            assert cached["next_offset"] == page["next_offset"]
        metrics["modules"][module] = {"records": len(ids), "pages": len(pages),
                                     "restart_cached_pages": len(pages)}
        all_hits.extend(hits)
    for label, module, prefix, sheet, expected in (
        ("residual_current", "2.4", "实测剩余电流=", None, 14),
        ("thermal", "2.2", "测点温度=", "红外热成像检测记录表", 13),
        ("spd", "2.3", "电涌保护装置=", "总配评估详情表", 9),
    ):
        hits = [hit for hit in all_hits if hit["module_id"] == module
                and prefix in hit["fact"] and (sheet is None or hit["source"]["sheet"] == sheet)]
        assert len(hits) == expected
        metrics["checks"][label] = {"recalled": len(hits), "expected": expected}
    assert len(all_hits) == 316
    output = root / "audit_manyselves/paginated_evidence_replay_metrics.json"
    output.write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


asyncio.run(main())
