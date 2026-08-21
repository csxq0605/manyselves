"""Registry for create/resolve semantics without business key interpretation."""

from typing import Protocol

from .models import ConversationKey, ConversationMode, ConversationRecord


class ConversationStore(Protocol):
    """Persistence port used only by cross-Run conversation identities."""

    def load(self, storage_key: str) -> ConversationRecord | None: ...

    def save(self, storage_key: str, record: ConversationRecord) -> None: ...

    def delete(self, storage_key: str) -> None: ...


class ConversationRegistry:
    """Resolve run and persistent identities; always create ephemeral records."""

    def __init__(self, store: ConversationStore | None = None) -> None:
        self._records: dict[str, ConversationRecord] = {}
        self._store = store
        self._ephemeral_sequence = 0
        self._reset_sequence = 0

    def create(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord:
        storage_key = self._storage_key(key, run_id)
        if key.mode is not ConversationMode.EPHEMERAL:
            existing = self._records.get(storage_key)
            if (
                existing is None
                and key.mode is ConversationMode.PERSISTENT
                and self._store is not None
            ):
                existing = self._store.load(storage_key)
                if existing is not None:
                    self._records[storage_key] = existing
            if existing is not None:
                return existing
            conversation_id = storage_key
        else:
            self._ephemeral_sequence += 1
            conversation_id = f"{storage_key}:{self._ephemeral_sequence}"
        record = ConversationRecord(
            conversation_id=conversation_id,
            key=key,
            run_id=None if key.mode is ConversationMode.PERSISTENT else run_id,
        )
        if key.mode is not ConversationMode.EPHEMERAL:
            self._records[storage_key] = record
            if key.mode is ConversationMode.PERSISTENT and self._store is not None:
                self._store.save(storage_key, record)
        return record

    def resolve(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord | None:
        if key.mode is ConversationMode.EPHEMERAL:
            return None
        storage_key = self._storage_key(key, run_id)
        record = self._records.get(storage_key)
        if (
            record is None
            and key.mode is ConversationMode.PERSISTENT
            and self._store is not None
        ):
            record = self._store.load(storage_key)
            if record is not None:
                self._records[storage_key] = record
        return record

    def create_or_resolve(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord:
        return self.resolve(key, run_id=run_id) or self.create(key, run_id=run_id)

    def remember(self, record: ConversationRecord) -> None:
        """Restore one non-ephemeral record from authoritative workflow state."""

        if record.key.mode is ConversationMode.EPHEMERAL:
            return
        marker = ":reset:"
        if marker in record.conversation_id:
            suffix = record.conversation_id.rsplit(marker, maxsplit=1)[-1]
            if suffix.isdigit():
                self._reset_sequence = max(self._reset_sequence, int(suffix))
        run_id = record.run_id or "persistent"
        storage_key = self._storage_key(record.key, run_id)
        self._records[storage_key] = record
        if record.key.mode is ConversationMode.PERSISTENT and self._store is not None:
            self._store.save(storage_key, record)

    def reset(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord:
        """Replace one conversation identity while retaining its declared binding."""

        storage_key = self._storage_key(key, run_id)
        self._records.pop(storage_key, None)
        if key.mode is ConversationMode.PERSISTENT and self._store is not None:
            self._store.delete(storage_key)
        self._reset_sequence += 1
        record = ConversationRecord(
            conversation_id=f"{storage_key}:reset:{self._reset_sequence}",
            key=key,
            run_id=None if key.mode is ConversationMode.PERSISTENT else run_id,
        )
        if key.mode is not ConversationMode.EPHEMERAL:
            self._records[storage_key] = record
            if key.mode is ConversationMode.PERSISTENT and self._store is not None:
                self._store.save(storage_key, record)
        return record

    @staticmethod
    def _storage_key(key: ConversationKey, run_id: str) -> str:
        scope = "persistent" if key.mode is ConversationMode.PERSISTENT else run_id
        return f"{key.mode}:{scope}:{key.agent_id}:{key.value}"
