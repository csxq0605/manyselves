"""File persistence for cross-Run conversation identities."""

import json
from pathlib import Path

from manyselves.kernel.conversations import ConversationRecord


class FileConversationStore:
    """Persist the small registry projection beside other Runtime state."""

    def __init__(self, workspace: Path) -> None:
        self._path = Path(workspace) / "Work" / "conversations.json"

    def load(self, storage_key: str) -> ConversationRecord | None:
        payload = self._read()
        value = payload.get(storage_key)
        return None if value is None else ConversationRecord.model_validate(value)

    def save(self, storage_key: str, record: ConversationRecord) -> None:
        payload = self._read()
        payload[storage_key] = record.model_dump(mode="json")
        self._write(payload)

    def delete(self, storage_key: str) -> None:
        payload = self._read()
        if storage_key not in payload:
            return
        del payload[storage_key]
        self._write(payload)

    def _read(self) -> dict[str, object]:
        if not self._path.is_file():
            return {}
        value = json.loads(self._path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}

    def _write(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
