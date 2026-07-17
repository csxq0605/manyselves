"""Concurrency-safe per-provider-attempt usage ledger."""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any


class UsageLedger:
    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, workspace: Path, run_id: str):
        safe_run = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
        self.path = Path(workspace).resolve() / ".manyselves" / "usage" / f"{safe_run}.jsonl"

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(path, threading.Lock())

    def record_attempt(self, **record: Any) -> dict[str, Any]:
        row = {"timestamp": datetime.now().astimezone().isoformat(), **record}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        with self._lock_for(self.path):
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        with self._lock_for(self.path):
            return [json.loads(line) for line in self.path.read_text(encoding="utf-8").splitlines()]
