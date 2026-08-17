"""Read-only index over normalized project evidence artifacts."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from ..models import EvidenceItem


def project_evidence_locator(item: EvidenceItem) -> str:
    """Return the one canonical, backward-compatible locator for an E-* item."""

    source = item.source
    parts = [source.path.as_posix()]
    if source.sheet:
        parts.append(f"工作表={source.sheet}")
    if source.cell:
        parts.append(f"单元格={source.cell}")
    if source.page:
        parts.append(f"页码={source.page}")
    return "；".join(parts)


class ProjectEvidenceIndex:
    """Search one run's immutable normalized customer-evidence snapshot."""

    def __init__(self, workspace: Path, run_id: str | None = None):
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.path = (
            self.workspace / f"Work/runs/{run_id}/preparation/evidence.jsonl"
            if run_id
            else self.workspace / "Work/evidence.jsonl"
        )
        self._items_cache: tuple[EvidenceItem, ...] | None = None
        self._by_id: dict[str, EvidenceItem] = {}
        self._snapshot_ref: Path | None = None

    def _load_snapshot(self) -> tuple[EvidenceItem, ...]:
        if self._items_cache is not None:
            return self._items_cache
        if not self.path.exists():
            self._items_cache = ()
            self._by_id = {}
            return self._items_cache
        source_bytes = self.path.read_bytes()
        source_sha256 = hashlib.sha256(source_bytes).hexdigest()
        relative_root = (
            Path(f"Work/runs/{self.run_id}/indexes/evidence")
            if self.run_id
            else Path("Work/indexes/evidence")
        )
        snapshot_ref = relative_root / f"{source_sha256}.json"
        snapshot_path = self.workspace / snapshot_ref
        lines = [line for line in source_bytes.decode("utf-8").splitlines() if line.strip()]
        items = tuple(EvidenceItem.model_validate_json(line) for line in lines)
        if len({item.id for item in items}) != len(items):
            raise ValueError("evidence snapshot contains duplicate ids")
        manifest = {
            "snapshot_version": 1,
            "source_ref": self.path.relative_to(self.workspace).as_posix(),
            "source_sha256": source_sha256,
            "source_bytes": len(source_bytes),
            "item_count": len(items),
            "items": [
                {
                    "id": item.id,
                    "line": index,
                    "content_sha256": hashlib.sha256(
                        lines[index - 1].encode("utf-8")
                    ).hexdigest(),
                }
                for index, item in enumerate(items, start=1)
            ],
        }
        serialized = (
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2)
            + "\n"
        )
        snapshot_path.parent.mkdir(parents=True, exist_ok=True)
        if snapshot_path.is_file():
            if snapshot_path.read_text(encoding="utf-8") != serialized:
                raise ValueError("evidence snapshot manifest changed for one source digest")
        else:
            temporary = snapshot_path.with_suffix(".tmp")
            temporary.write_text(serialized, encoding="utf-8")
            os.replace(temporary, snapshot_path)
        self._items_cache = items
        self._by_id = {item.id: item for item in items}
        self._snapshot_ref = snapshot_ref
        return items

    def items(self) -> list[EvidenceItem]:
        return list(self._load_snapshot())

    def snapshot_manifest_ref(self) -> Path | None:
        self._load_snapshot()
        return self._snapshot_ref

    def get(self, evidence_id: str) -> EvidenceItem:
        if not evidence_id.startswith("E-"):
            raise ValueError("project source ids must start with E-")
        self._load_snapshot()
        if evidence_id in self._by_id:
            return self._by_id[evidence_id]
        raise ValueError(f"unknown project evidence id: {evidence_id}")

    def search(self, query: str, limit: int = 10) -> list[EvidenceItem]:
        terms = [term.casefold() for term in query.split() if term.strip()]
        if not terms or limit <= 0:
            return []
        ranked: list[tuple[int, EvidenceItem]] = []
        for item in self._load_snapshot():
            text = " ".join(
                str(value)
                for value in (
                    item.id,
                    item.subject,
                    item.fact,
                    item.value or "",
                    item.unit or "",
                    item.observed_at or "",
                    item.source.path,
                    item.source.sheet or "",
                    item.source.cell or "",
                )
            ).casefold()
            score = sum(text.count(term) for term in terms)
            if score:
                ranked.append((score, item))
        ranked.sort(key=lambda pair: (-pair[0], pair[1].id))
        return [item for _, item in ranked[:limit]]
