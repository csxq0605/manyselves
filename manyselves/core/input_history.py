"""Project-local history for text submitted through the chat composer."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class InputHistoryStore:
    """Persist recent composer inputs per agent inside the current project."""

    def __init__(self, workspace: Path, max_entries: int = 50):
        self.workspace = Path(workspace).resolve()
        self.path = self.workspace / ".manyselves" / "input_history.json"
        self.max_entries = max(1, max_entries)

    def _read(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "agents": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {"version": 1, "agents": {}}
        if not isinstance(payload, dict) or not isinstance(payload.get("agents"), dict):
            return {"version": 1, "agents": {}}
        return payload

    def entries(self, agent_type: str) -> list[str]:
        raw = self._read()["agents"].get(str(agent_type), [])
        if not isinstance(raw, list):
            return []
        entries = [str(item).strip() for item in raw if str(item).strip()]
        return entries[-self.max_entries :]

    def append(self, agent_type: str, entry: str) -> list[str]:
        """Append one non-empty entry, de-duplicating it like shell history."""

        entry = str(entry).strip()
        if not entry:
            return self.entries(agent_type)

        payload = self._read()
        agents = payload["agents"]
        entries = self.entries(agent_type)
        if entry in entries:
            entries.remove(entry)
        entries.append(entry)
        entries = entries[-self.max_entries :]
        agents[str(agent_type)] = entries

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self.path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(self.path)
        except OSError:
            pass
        return entries
