"""Contract tests for the serialized application runtime facade."""

import asyncio
import inspect
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from manyselves.application.control import ControlLeaseRequired, ControlLeaseService
from manyselves.application.errors import CommandIdConflictError, RuntimeNotReadyError
from manyselves.application.legacy_runtime_adapter import LegacyRuntimeAdapter
from manyselves.application.models import (
    AcceptedCommand,
    InterruptCommand,
    RollbackCommand,
    RollbackResult,
    RuntimeSnapshot,
    SendFileContextCommand,
    SendMessageCommand,
)
from manyselves.application.runtime_facade import RuntimeFacade


class MutableClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 7, 31, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        return self.now

    def advance(self, *, seconds: int) -> None:
        self.now += timedelta(seconds=seconds)


class RecordingBackend:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail_message_once = False

    async def send_user_message(
        self,
        content: str,
        agent_type: str,
        message_id: str | None = None,
        source: str = "user",
    ) -> None:
        self.calls.append(("message", content, agent_type, message_id, source))
        if self.fail_message_once:
            self.fail_message_once = False
            raise RuntimeError("transient failure")

    async def send_file_context(self, file_context: dict[str, Any], agent_type: str) -> None:
        self.calls.append(("file", file_context, agent_type))

    async def interrupt_current_message(self, agent_type: str) -> None:
        self.calls.append(("interrupt", agent_type))

    async def rollback_to_checkpoint(
        self, agent_type: str, checkpoint_id: str
    ) -> dict[str, Any]:
        self.calls.append(("rollback", agent_type, checkpoint_id))
        return {
            "restored_files": 2,
            "conversation_history": [{"role": "user", "content": "before"}],
        }


class SnapshotLoopManager:
    def get_all_agent_statuses(self) -> dict[str, str]:
        return {"main": "thinking", "researcher": "idle"}

    def get_agent_session_id(self, agent_id: str) -> str | None:
        assert agent_id == "main"
        return "session-1"


class StableString:
    """Non-JSON value whose backend-visible string representation is stable."""

    def __init__(self, value: str) -> None:
        self.value = value

    def __str__(self) -> str:
        return self.value


def make_host(*, ready: bool = True, workspace: Path | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        backend=RecordingBackend(),
        is_ready=ready,
        workspace=workspace,
        loop_manager=SnapshotLoopManager() if ready else None,
    )


def lease_for(
    leases: ControlLeaseService, *, client_id: str = "browser-1"
) -> str:
    return leases.acquire(client_id=client_id, actor_id="alice").token


def message_command(token: str, *, command_id: UUID | None = None) -> SendMessageCommand:
    return SendMessageCommand(
        command_id=command_id or uuid4(),
        lease_token=token,
        content="hello",
        agent_id="researcher",
        message_id="message-1",
        source="main_agent",
    )


def test_command_models_validate_message_content_and_source() -> None:
    common = {
        "command_id": uuid4(),
        "lease_token": "token",
        "agent_id": "main",
    }
    with pytest.raises(ValidationError):
        SendMessageCommand(**common, content="")
    with pytest.raises(ValidationError):
        SendMessageCommand(**common, content="hello", source="system")


def test_runtime_snapshot_has_exact_locked_fields() -> None:
    assert set(RuntimeSnapshot.model_fields) == {
        "ready",
        "workspace",
        "agent_statuses",
        "active_session_id",
        "controller_client_id",
    }


@pytest.mark.asyncio
async def test_facade_maps_all_commands_to_existing_backend_methods(tmp_path: Path) -> None:
    host = make_host(workspace=tmp_path)
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)

    send_command = message_command(token)
    sent = await facade.send_user_message(send_command)
    file_command = SendFileContextCommand(
        command_id=uuid4(),
        lease_token=token,
        file_context={"type": "file", "file": "Inputs/source.txt"},
        agent_id="main",
    )
    file_sent = await facade.send_file_context(file_command)
    interrupted = await facade.interrupt(
        InterruptCommand(command_id=uuid4(), lease_token=token, agent_id="main")
    )
    rollback = await facade.rollback(
        RollbackCommand(
            command_id=uuid4(),
            lease_token=token,
            agent_id="main",
            checkpoint_id="checkpoint-1",
        )
    )

    assert sent == AcceptedCommand(command_id=send_command.command_id)
    assert file_sent.status == "accepted"
    assert interrupted.status == "accepted"
    assert rollback == RollbackResult(
        restored_files=2,
        conversation_history=[{"role": "user", "content": "before"}],
    )
    assert host.backend.calls == [
        ("message", "hello", "researcher", "message-1", "main_agent"),
        ("file", {"type": "file", "file": "Inputs/source.txt"}, "main"),
        ("interrupt", "main"),
        ("rollback", "main", "checkpoint-1"),
    ]


@pytest.mark.asyncio
async def test_all_mutation_types_share_one_serialization_lock() -> None:
    events: list[str] = []
    first_entered = asyncio.Event()
    allow_first = asyncio.Event()

    class BlockingBackend(RecordingBackend):
        async def send_user_message(self, *args: Any, **kwargs: Any) -> None:
            events.append("message-start")
            first_entered.set()
            await allow_first.wait()
            events.append("message-end")

        async def interrupt_current_message(self, agent_type: str) -> None:
            events.append("interrupt")

    host = make_host()
    host.backend = BlockingBackend()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)

    first = asyncio.create_task(facade.send_user_message(message_command(token)))
    await first_entered.wait()
    second = asyncio.create_task(
        facade.interrupt(
            InterruptCommand(command_id=uuid4(), lease_token=token, agent_id="main")
        )
    )
    await asyncio.sleep(0)
    assert events == ["message-start"]
    allow_first.set()
    await asyncio.gather(first, second)
    assert events == ["message-start", "message-end", "interrupt"]


@pytest.mark.asyncio
async def test_lease_is_revalidated_after_waiting_for_mutation_lock() -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    token = lease_for(leases)
    first_entered = asyncio.Event()
    allow_first = asyncio.Event()

    class BlockingBackend(RecordingBackend):
        async def send_user_message(self, *args: Any, **kwargs: Any) -> None:
            first_entered.set()
            await allow_first.wait()

    host = make_host()
    host.backend = BlockingBackend()
    facade = RuntimeFacade(host, leases=leases)

    in_progress = asyncio.create_task(facade.send_user_message(message_command(token)))
    await first_entered.wait()
    waiting = asyncio.create_task(
        facade.interrupt(
            InterruptCommand(command_id=uuid4(), lease_token=token, agent_id="main")
        )
    )
    await asyncio.sleep(0)
    clock.advance(seconds=31)
    allow_first.set()

    with pytest.raises(ControlLeaseRequired):
        await waiting
    await in_progress
    assert host.backend.calls == []


@pytest.mark.asyncio
async def test_not_ready_runtime_rejects_mutations_with_stable_error() -> None:
    host = make_host(ready=False)
    leases = ControlLeaseService()
    token = lease_for(leases)

    with pytest.raises(RuntimeNotReadyError) as raised:
        await RuntimeFacade(host, leases=leases).send_user_message(message_command(token))

    assert raised.value.code == "RUNTIME_NOT_READY"
    assert host.backend.calls == []


@pytest.mark.asyncio
async def test_duplicate_successful_command_returns_original_without_second_call() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command = message_command(token)

    first = await facade.send_user_message(command)
    second = await facade.send_user_message(command)

    assert second is first
    assert len(host.backend.calls) == 1


@pytest.mark.asyncio
async def test_duplicate_rollback_returns_original_typed_result() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command = RollbackCommand(
        command_id=uuid4(),
        lease_token=token,
        agent_id="main",
        checkpoint_id="checkpoint-1",
    )

    first = await facade.rollback(command)
    second = await facade.rollback(command)

    assert isinstance(first, RollbackResult)
    assert second is first
    assert host.backend.calls == [("rollback", "main", "checkpoint-1")]


@pytest.mark.asyncio
async def test_command_id_reuse_across_accepted_mutations_is_a_conflict() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()

    first = await facade.send_user_message(message_command(token, command_id=command_id))
    with pytest.raises(CommandIdConflictError) as raised:
        await facade.interrupt(
            InterruptCommand(command_id=command_id, lease_token=token, agent_id="main")
        )

    assert first.status == "accepted"
    assert raised.value.code == "COMMAND_ID_CONFLICT"
    assert host.backend.calls == [
        ("message", "hello", "researcher", "message-1", "main_agent")
    ]


@pytest.mark.asyncio
async def test_accepted_command_id_cannot_be_reused_for_rollback() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()

    await facade.send_user_message(message_command(token, command_id=command_id))
    with pytest.raises(CommandIdConflictError):
        await facade.rollback(
            RollbackCommand(
                command_id=command_id,
                lease_token=token,
                agent_id="main",
                checkpoint_id="checkpoint-1",
            )
        )

    assert host.backend.calls == [
        ("message", "hello", "researcher", "message-1", "main_agent")
    ]


@pytest.mark.asyncio
async def test_rollback_command_id_cannot_be_reused_for_accepted_command() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()

    rollback = await facade.rollback(
        RollbackCommand(
            command_id=command_id,
            lease_token=token,
            agent_id="main",
            checkpoint_id="checkpoint-1",
        )
    )
    with pytest.raises(CommandIdConflictError):
        await facade.interrupt(
            InterruptCommand(command_id=command_id, lease_token=token, agent_id="main")
        )

    assert isinstance(rollback, RollbackResult)
    assert host.backend.calls == [("rollback", "main", "checkpoint-1")]


@pytest.mark.asyncio
async def test_same_operation_command_id_with_changed_payload_is_a_conflict() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()

    await facade.send_user_message(message_command(token, command_id=command_id))
    changed = message_command(token, command_id=command_id).model_copy(
        update={"content": "different"}
    )
    with pytest.raises(CommandIdConflictError):
        await facade.send_user_message(changed)

    assert host.backend.calls == [
        ("message", "hello", "researcher", "message-1", "main_agent")
    ]


@pytest.mark.asyncio
async def test_file_context_non_json_values_replay_by_consumed_semantics() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()
    command = SendFileContextCommand(
        command_id=command_id,
        lease_token=token,
        file_context={
            "type": "file",
            "file": StableString("  Inputs/source.txt  "),
            "irrelevant": object(),
        },
        agent_id="main",
    )

    first = await facade.send_file_context(command)
    command.file_context["file"] = "Inputs/mutated.txt"
    command.file_context["irrelevant"] = object()
    replay = SendFileContextCommand(
        command_id=command_id,
        lease_token=token,
        file_context={
            "type": "file",
            "file": StableString("Inputs/source.txt"),
            "different_irrelevant_key": object(),
        },
        agent_id="main",
    )
    second = await facade.send_file_context(replay)

    assert second is first
    assert len(host.backend.calls) == 1


@pytest.mark.asyncio
async def test_file_context_changed_consumed_value_is_a_conflict() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()
    original = SendFileContextCommand(
        command_id=command_id,
        lease_token=token,
        file_context={
            "type": "selection",
            "file": StableString("Inputs/source.txt"),
            "start_line": 2,
            "end_line": 4,
        },
        agent_id="main",
    )
    changed = original.model_copy(
        update={
            "file_context": {
                "type": "selection",
                "file": StableString("Inputs/source.txt"),
                "start_line": 2,
                "end_line": 5,
            }
        }
    )

    await facade.send_file_context(original)
    with pytest.raises(CommandIdConflictError):
        await facade.send_file_context(changed)

    assert len(host.backend.calls) == 1


@pytest.mark.asyncio
async def test_file_context_fingerprint_preserves_explicit_none_string_semantics() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command_id = uuid4()
    explicit_none = SendFileContextCommand(
        command_id=command_id,
        lease_token=token,
        file_context={"type": "file", "file": None},
        agent_id="main",
    )
    missing_file = SendFileContextCommand(
        command_id=command_id,
        lease_token=token,
        file_context={"type": "file"},
        agent_id="main",
    )

    await facade.send_file_context(explicit_none)
    with pytest.raises(CommandIdConflictError):
        await facade.send_file_context(missing_file)

    assert len(host.backend.calls) == 1


@pytest.mark.asyncio
async def test_command_cache_is_bounded_with_deterministic_fifo_eviction() -> None:
    host = make_host()
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases, command_cache_size=2)
    oldest = message_command(token)

    await facade.send_user_message(oldest)
    await facade.interrupt(
        InterruptCommand(command_id=uuid4(), lease_token=token, agent_id="main")
    )
    await facade.send_file_context(
        SendFileContextCommand(
            command_id=uuid4(),
            lease_token=token,
            file_context={"type": "file", "file": "a.txt"},
            agent_id="main",
        )
    )
    await facade.send_user_message(oldest)

    assert [call[0] for call in host.backend.calls] == [
        "message",
        "interrupt",
        "file",
        "message",
    ]


@pytest.mark.asyncio
async def test_failed_command_is_not_cached_and_can_be_retried() -> None:
    host = make_host()
    host.backend.fail_message_once = True
    leases = ControlLeaseService()
    token = lease_for(leases)
    facade = RuntimeFacade(host, leases=leases)
    command = message_command(token)

    with pytest.raises(RuntimeError, match="transient"):
        await facade.send_user_message(command)
    accepted = await facade.send_user_message(command)

    assert accepted.status == "accepted"
    assert len(host.backend.calls) == 2


def test_snapshot_before_start_is_empty_and_does_not_create_persistence() -> None:
    host = make_host(ready=False)
    leases = ControlLeaseService()
    lease_for(leases)

    snapshot = RuntimeFacade(host, leases=leases).snapshot()

    assert snapshot == RuntimeSnapshot(
        ready=False,
        workspace=None,
        agent_statuses={},
        active_session_id=None,
        controller_client_id="browser-1",
    )


def test_snapshot_when_ready_uses_adapter_and_hides_expired_controller(tmp_path: Path) -> None:
    clock = MutableClock()
    leases = ControlLeaseService(clock=clock, ttl=timedelta(seconds=30))
    lease_for(leases)
    host = make_host(workspace=tmp_path)
    facade = RuntimeFacade(host, leases=leases)

    assert facade.snapshot() == RuntimeSnapshot(
        ready=True,
        workspace=str(tmp_path),
        agent_statuses={"main": "thinking", "researcher": "idle"},
        active_session_id="session-1",
        controller_client_id="browser-1",
    )
    clock.advance(seconds=31)
    assert facade.snapshot().controller_client_id is None


def test_legacy_adapter_uses_public_loop_manager_queries_only(tmp_path: Path) -> None:
    host = make_host(workspace=tmp_path)
    adapter = LegacyRuntimeAdapter(host)

    snapshot = adapter.snapshot(controller_client_id="browser-1")

    assert snapshot.active_session_id == "session-1"
    source = inspect.getsource(LegacyRuntimeAdapter)
    assert "_task_board" not in source
    assert "_conversation_history" not in source
    assert "_current_session_id" not in source
