"""Registry for create/resolve semantics without business key interpretation."""

from .models import ConversationKey, ConversationMode, ConversationRecord


class ConversationRegistry:
    """Resolve run and persistent identities; always create ephemeral records."""

    def __init__(self) -> None:
        self._records: dict[str, ConversationRecord] = {}
        self._ephemeral_sequence = 0

    def create(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord:
        storage_key = self._storage_key(key, run_id)
        if key.mode is not ConversationMode.EPHEMERAL:
            existing = self._records.get(storage_key)
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
        return record

    def resolve(
        self,
        key: ConversationKey,
        *,
        run_id: str,
    ) -> ConversationRecord | None:
        if key.mode is ConversationMode.EPHEMERAL:
            return None
        return self._records.get(self._storage_key(key, run_id))

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
        run_id = record.run_id or "persistent"
        self._records[self._storage_key(record.key, run_id)] = record

    @staticmethod
    def _storage_key(key: ConversationKey, run_id: str) -> str:
        scope = "persistent" if key.mode is ConversationMode.PERSISTENT else run_id
        return f"{key.mode}:{scope}:{key.agent_id}:{key.value}"
