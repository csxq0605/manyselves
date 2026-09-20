from manyselves.kernel.conversations import (
    ConversationKey,
    ConversationMode,
    ConversationRegistry,
)
from manyselves.runtime.conversation_store import FileConversationStore


def test_run_conversation_reuses_same_agent_and_key() -> None:
    registry = ConversationRegistry()
    key = ConversationKey(
        agent_id="agent-a",
        value="topic-1",
        mode=ConversationMode.RUN,
    )

    first = registry.create_or_resolve(key, run_id="run-1")
    second = registry.create_or_resolve(key, run_id="run-1")

    assert second is first
    assert second.conversation_id == first.conversation_id


def test_conversations_are_independent_by_agent_key_and_run() -> None:
    registry = ConversationRegistry()

    agent_a = registry.create_or_resolve(
        ConversationKey(agent_id="agent-a", value="topic", mode="run"),
        run_id="run-1",
    )
    agent_b = registry.create_or_resolve(
        ConversationKey(agent_id="agent-b", value="topic", mode="run"),
        run_id="run-1",
    )
    other_key = registry.create_or_resolve(
        ConversationKey(agent_id="agent-a", value="other", mode="run"),
        run_id="run-1",
    )
    other_run = registry.create_or_resolve(
        ConversationKey(agent_id="agent-a", value="topic", mode="run"),
        run_id="run-2",
    )

    assert (
        len(
            {
                agent_a.conversation_id,
                agent_b.conversation_id,
                other_key.conversation_id,
                other_run.conversation_id,
            }
        )
        == 4
    )


def test_ephemeral_conversation_is_new_for_each_creation() -> None:
    registry = ConversationRegistry()
    key = ConversationKey(agent_id="agent-a", value="topic", mode="ephemeral")

    first = registry.create(key, run_id="run-1")
    second = registry.create(key, run_id="run-1")

    assert first.conversation_id != second.conversation_id


def test_persistent_conversation_is_resolved_across_runs() -> None:
    registry = ConversationRegistry()
    key = ConversationKey(agent_id="agent-a", value="topic", mode="persistent")

    first = registry.create_or_resolve(key, run_id="run-1")
    second = registry.create_or_resolve(key, run_id="run-2")

    assert second is first


def test_serialized_run_record_can_seed_a_fresh_registry() -> None:
    first_registry = ConversationRegistry()
    key = ConversationKey(agent_id="agent-a", value="topic", mode="run")
    first = first_registry.create_or_resolve(key, run_id="run-1")
    second_registry = ConversationRegistry()
    second_registry.remember(type(first).model_validate(first.model_dump(mode="json")))

    restored = second_registry.resolve(key, run_id="run-1")

    assert restored is not None
    assert restored.conversation_id == first.conversation_id


def test_persistent_conversation_survives_a_fresh_registry(tmp_path) -> None:
    key = ConversationKey(agent_id="agent-a", value="topic", mode="persistent")
    first_registry = ConversationRegistry(FileConversationStore(tmp_path))
    first = first_registry.create_or_resolve(key, run_id="run-1")

    second_registry = ConversationRegistry(FileConversationStore(tmp_path))
    restored = second_registry.resolve(key, run_id="run-2")

    assert restored is not None
    assert restored.conversation_id == first.conversation_id
    assert restored.run_id is None
