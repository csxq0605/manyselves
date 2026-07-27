"""Persistent E/R/W source registry for one reporting run."""

from __future__ import annotations

import hashlib
import json
import threading
from datetime import UTC, datetime
from pathlib import Path

from .agentic_models import SourceKind, SourceRecord

_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[Path, threading.RLock] = {}


def _lock_for(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _PATH_LOCKS.setdefault(path, threading.RLock())


class SourceLedger:
    """Register sources with stable per-kind identifiers and durable metadata."""

    def __init__(self, workspace: Path, run_id: str):
        workspace = Path(workspace).resolve()
        safe_run_id = Path(run_id).name
        if not run_id or safe_run_id != run_id:
            raise ValueError("run_id must be a single safe path component")
        self.workspace = workspace
        self.run_id = safe_run_id
        self.path = workspace / "Work/runs" / safe_run_id / "ledgers/sources.json"
        self.content_root = workspace / "Work/runs" / safe_run_id / "sources"
        self._lock = _lock_for(self.path)

    def _load(self) -> list[SourceRecord]:
        if not self.path.exists():
            return []
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        return [SourceRecord.model_validate(item) for item in raw]

    @property
    def records(self) -> list[SourceRecord]:
        with self._lock:
            return self._load()

    def _persist(self, records: list[SourceRecord]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(
                [record.model_dump(mode="json") for record in records],
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.path)

    def _persist_content(self, source_id: str, content: str) -> Path:
        if Path(source_id).name != source_id or not source_id:
            raise ValueError("source id must be one safe path component")
        path = self.content_root / f"{source_id}.txt"
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".txt.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(path)
        return path.relative_to(self.workspace)

    def content_ref(self, source_id: str) -> Path | None:
        path = self.content_root / f"{Path(source_id).name}.txt"
        return path.relative_to(self.workspace) if path.is_file() else None

    @staticmethod
    def _digest(content: str) -> str:
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _next_id(records: list[SourceRecord], kind: SourceKind) -> str:
        prefix = {SourceKind.LOCAL_REFERENCE: "R-", SourceKind.WEB: "W-"}[kind]
        numbers = [
            int(record.id.removeprefix(prefix))
            for record in records
            if record.kind == kind and record.id.removeprefix(prefix).isdigit()
        ]
        return f"{prefix}{max(numbers, default=0) + 1:03d}"

    def register_local(self, title: str, locator: str, content: str) -> SourceRecord:
        locator_path = locator.split("；", 1)[0].split("#", 1)[0]
        resolved = (self.workspace / locator_path).resolve()
        knowledge_root = (self.workspace / "Knowledge").resolve()
        if not resolved.is_relative_to(knowledge_root) or resolved == knowledge_root:
            raise ValueError("local references must be located beneath project Knowledge")
        digest = self._digest(content)
        with self._lock:
            records = self._load()
            for record in records:
                if (
                    record.kind == SourceKind.LOCAL_REFERENCE
                    and record.locator == locator
                    and record.content_sha256 == digest
                ):
                    self._persist_content(record.id, content)
                    return record
            record = SourceRecord(
                id=self._next_id(records, SourceKind.LOCAL_REFERENCE),
                kind=SourceKind.LOCAL_REFERENCE,
                title=title,
                locator=locator,
                content_sha256=digest,
            )
            records.append(record)
            self._persist(records)
            self._persist_content(record.id, content)
            return record

    def register_project(
        self, evidence_id: str, title: str, locator: str, content: str
    ) -> SourceRecord:
        if not evidence_id.startswith("E-"):
            raise ValueError("project evidence id must start with E-")
        digest = self._digest(content)
        with self._lock:
            records = self._load()
            for record in records:
                if record.id == evidence_id:
                    if record.locator != locator or record.content_sha256 != digest:
                        raise ValueError(f"conflicting project evidence id: {evidence_id}")
                    self._persist_content(record.id, content)
                    return record
            record = SourceRecord(
                id=evidence_id,
                kind=SourceKind.PROJECT_EVIDENCE,
                title=title,
                locator=locator,
                content_sha256=digest,
            )
            records.append(record)
            self._persist(records)
            self._persist_content(record.id, content)
            return record

    def register_web(
        self,
        title: str,
        url: str,
        content: str,
        publisher: str | None = None,
        published_at: str | None = None,
        scope_note: str | None = None,
    ) -> SourceRecord:
        digest = self._digest(content)
        accessed_at = datetime.now(UTC).date().isoformat()
        with self._lock:
            records = self._load()
            for record in records:
                if (
                    record.kind == SourceKind.WEB
                    and record.locator == url
                    and record.content_sha256 == digest
                ):
                    self._persist_content(record.id, content)
                    return record
            record = SourceRecord(
                id=self._next_id(records, SourceKind.WEB),
                kind=SourceKind.WEB,
                title=title,
                locator=url,
                publisher=publisher,
                published_at=published_at,
                accessed_at=accessed_at,
                scope_note=scope_note,
                content_sha256=digest,
            )
            records.append(record)
            self._persist(records)
            self._persist_content(record.id, content)
            return record
