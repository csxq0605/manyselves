"""Run-persistent, module-scoped memory for facts returned by research tools."""

from __future__ import annotations

import json
import threading
from pathlib import Path

from ..models import EvidenceItem
from ..store import ReportingStore


_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(path, threading.RLock())


class EvidenceResearchMemory:
    """Deduplicated facts shared by specialist, auditor, and resumed runs."""

    def __init__(self, workspace: Path, run_id: str, module_id: str):
        self.workspace = Path(workspace).resolve()
        self.run_id = Path(run_id).name
        self.module_id = module_id
        self.relative_path = Path(
            f"Work/runs/{self.run_id}/context/module-{module_id}-evidence-memory.json"
        )
        self.path = self.workspace / self.relative_path
        self._lock = _lock_for(self.path)

    def ensure(self) -> Path:
        with self._lock:
            if not self.path.is_file():
                self._save(
                    {
                        "module_id": self.module_id,
                        "queries": [],
                        "evidence": {},
                        "metrics": {
                            "query_calls": 0,
                            "cache_hits": 0,
                            "index_searches": 0,
                            "source_opens": 0,
                            "source_cache_hits": 0,
                        },
                    }
                )
        return self.relative_path

    def _load(self) -> dict:
        self.ensure()
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, payload: dict) -> None:
        ReportingStore(self.workspace).write_json(self.relative_path.as_posix(), payload)

    def remember(
        self,
        items: list[EvidenceItem],
        *,
        query: str | None = None,
        applied_limit: int | None = None,
    ) -> None:
        with self._lock:
            payload = self._load()
            evidence = payload.setdefault("evidence", {})
            for item in items:
                evidence[item.id] = item.model_dump(mode="json")
            if query:
                record = {
                    "query": query,
                    "applied_limit": applied_limit or len(items),
                    "evidence_ids": [item.id for item in items],
                }
                queries = payload.setdefault("queries", [])
                queries[:] = [item for item in queries if item.get("query") != query]
                queries.append(record)
            self._save(payload)

    def recall_query(self, query: str, applied_limit: int) -> list[EvidenceItem] | None:
        with self._lock:
            payload = self._load()
            metrics = payload.setdefault("metrics", {})
            metrics["query_calls"] = int(metrics.get("query_calls", 0)) + 1
            for record in payload.get("queries", []):
                if record.get("query") != query:
                    continue
                recorded_limit = int(record.get("applied_limit", 0) or 0)
                evidence_ids = list(record.get("evidence_ids", []))
                if recorded_limit < applied_limit and len(evidence_ids) >= recorded_limit:
                    continue
                evidence = payload.get("evidence", {})
                if any(source_id not in evidence for source_id in evidence_ids):
                    continue
                metrics["cache_hits"] = int(metrics.get("cache_hits", 0)) + 1
                self._save(payload)
                return [EvidenceItem.model_validate(evidence[source_id]) for source_id in evidence_ids]
            metrics["index_searches"] = int(metrics.get("index_searches", 0)) + 1
            self._save(payload)
            return None

    def recall_source(self, source_id: str) -> EvidenceItem | None:
        with self._lock:
            payload = self._load()
            metrics = payload.setdefault("metrics", {})
            metrics["source_opens"] = int(metrics.get("source_opens", 0)) + 1
            raw = payload.get("evidence", {}).get(source_id)
            if raw is None:
                self._save(payload)
                return None
            metrics["source_cache_hits"] = int(metrics.get("source_cache_hits", 0)) + 1
            self._save(payload)
            return EvidenceItem.model_validate(raw)

    @property
    def evidence_ids(self) -> list[str]:
        with self._lock:
            return sorted(self._load().get("evidence", {}))
