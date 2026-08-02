"""Stable public event mapping and process-local replay contracts."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr

from manyselves.interfaces import types as message_types
from manyselves.interfaces.types import (
    AgentResponse,
    AgentResultMessage,
    ApiDebugMessage,
    BlockedNoticeMessage,
    Checkpoint,
    ConfigChange,
    Error,
    FileRollbackRequest,
    Message,
    MessageType,
    PeerQueryMessage,
    PeerReplyMessage,
    ProgressNoteMessage,
    QueueUpdateMessage,
    ReportMessage,
    ResearchNotePublishedMessage,
    RestartRequest,
    RollbackStatus,
    StatusChange,
    SystemNotice,
    TaskUpdateMessage,
    ToolCallMessage,
    ToolResult,
    UserMessage,
    WorkflowMessage,
)
from manyselves.webapi.events.mapper import (
    EventContext,
    EventMapper,
    UnsupportedMessageTypeError,
)
from manyselves.webapi.events.models import EventEnvelope
from manyselves.webapi.events.replay import ReplayBuffer

NOW = datetime(2026, 7, 31, 10, 30, tzinfo=UTC)


def context(sequence: int = 1) -> EventContext:
    return EventContext(
        event_id=f"test-stream:evt-{sequence}",
        stream_id="test-stream",
        sequence=sequence,
        project_id="project-1",
        session_id="session-1",
        agent_id="main",
        run_id="run-1",
        message_id="message-1",
    )


def event(sequence: int, *, event_type: str = "system.notice") -> EventEnvelope:
    return EventEnvelope(
        streamId="test-stream",
        eventId=f"test-stream:evt-{sequence}",
        sequence=sequence,
        type=event_type,
        timestamp=NOW,
        projectId="project-1",
        sessionId="session-1",
        agentId="main",
        runId=None,
        messageId=None,
        payload={"sequence": sequence},
    )


def concrete_message_types() -> set[type[Message]]:
    """Return every constructible Message class declared by the interface module."""
    return {
        value
        for value in vars(message_types).values()
        if isinstance(value, type) and issubclass(value, Message) and value is not Message
    }


MAPPER_CASES: dict[type[Message], tuple[Message, str]] = {
    UserMessage: (UserMessage(content="hello", timestamp=NOW), "user.message.created"),
    AgentResponse: (
        AgentResponse(agent_type="main", content="done", timestamp=NOW),
        "agent.message.completed",
    ),
    ToolCallMessage: (
        ToolCallMessage(agent_type="main", tool_name="read_file", arguments={}, timestamp=NOW),
        "tool.started",
    ),
    ToolResult: (
        ToolResult(agent_type="main", tool_name="read_file", result="ok", timestamp=NOW),
        "tool.completed",
    ),
    StatusChange: (
        StatusChange(agent_type="main", status="thinking", timestamp=NOW),
        "agent.status.changed",
    ),
    ConfigChange: (
        ConfigChange(config_type="model", old_value="old", new_value="new", timestamp=NOW),
        "configuration.changed",
    ),
    RestartRequest: (
        RestartRequest(reason="config_change", timestamp=NOW),
        "runtime.restart.requested",
    ),
    Checkpoint: (
        Checkpoint(agent_type="main", checkpoint_id="cp-1", description="saved", timestamp=NOW),
        "checkpoint.created",
    ),
    FileRollbackRequest: (
        FileRollbackRequest(agent_type="main", checkpoint_id="cp-1", timestamp=NOW),
        "rollback.requested",
    ),
    RollbackStatus: (
        RollbackStatus(agent_type="main", checkpoint_id="cp-1", success=True, timestamp=NOW),
        "rollback.completed",
    ),
    Error: (Error(source="system", message="failed", timestamp=NOW), "system.error"),
    TaskUpdateMessage: (
        TaskUpdateMessage(
            task_id="task-1",
            action="created",
            source_agent="main",
            target_agent="analyst",
            timestamp=NOW,
        ),
        "task.status.changed",
    ),
    QueueUpdateMessage: (
        QueueUpdateMessage(agent_type="main", queued_messages=["next"], timestamp=NOW),
        "queue.changed",
    ),
    ApiDebugMessage: (
        ApiDebugMessage(
            model="model",
            tokens_in=1,
            tokens_out=2,
            duration_ms=3,
            status="success",
            timestamp=NOW,
        ),
        "debug.api.completed",
    ),
    ReportMessage: (
        ReportMessage(agent_type="report", task_id="task-1", report_type="progress", timestamp=NOW),
        "report.status.changed",
    ),
    SystemNotice: (
        SystemNotice(agent_type="main", content="waiting", timestamp=NOW),
        "system.notice",
    ),
    WorkflowMessage: (
        WorkflowMessage(type=MessageType.REPORT, task_id="task-1", sender="report", timestamp=NOW),
        "reporting.workflow.message",
    ),
    PeerQueryMessage: (
        PeerQueryMessage(
            task_id="task-1",
            sender="report",
            recipient="analyst",
            query_id="query-1",
            source_session_id="session-1",
            question="why?",
            timestamp=NOW,
        ),
        "reporting.peer.query",
    ),
    PeerReplyMessage: (
        PeerReplyMessage(
            task_id="task-1",
            sender="analyst",
            recipient="report",
            query_id="query-1",
            target_session_id="session-1",
            answer="because",
            timestamp=NOW,
        ),
        "reporting.peer.reply",
    ),
    ProgressNoteMessage: (
        ProgressNoteMessage(task_id="task-1", sender="analyst", timestamp=NOW),
        "reporting.progress.changed",
    ),
    ResearchNotePublishedMessage: (
        ResearchNotePublishedMessage(
            task_id="task-1", sender="analyst", note_id="note-1", timestamp=NOW
        ),
        "reporting.research_note.published",
    ),
    BlockedNoticeMessage: (
        BlockedNoticeMessage(task_id="task-1", sender="analyst", reason="missing", timestamp=NOW),
        "reporting.blocked",
    ),
    AgentResultMessage: (
        AgentResultMessage(
            task_id="task-1",
            sender="analyst",
            run_id="run-1",
            result_path="Work/runs/run-1/result.md",
            timestamp=NOW,
        ),
        "reporting.agent_result.changed",
    ),
}


def test_mapper_cases_cover_every_concrete_interface_message() -> None:
    """Adding a new concrete bus message without a public mapping would hide it from clients."""
    assert set(MAPPER_CASES) == concrete_message_types()


@pytest.mark.parametrize(
    ("message", "expected_type"),
    MAPPER_CASES.values(),
    ids=[message_type.__name__ for message_type in MAPPER_CASES],
)
def test_internal_message_maps_to_stable_public_event(message: Message, expected_type: str) -> None:
    """Renaming or silently ignoring a mapped event would break React reconnect behavior."""
    mapped = EventMapper().map(message, context=context())

    assert mapped.type == expected_type
    assert mapped.schema_version == 1
    assert mapped.timestamp == NOW


@pytest.mark.parametrize(
    ("message", "expected_type"),
    [
        (AgentResponse(agent_type="main", content="part", streaming=True), "agent.message.delta"),
        (ToolResult(agent_type="main", tool_name="run", result=None, error="boom"), "tool.failed"),
        (RollbackStatus(agent_type="main", checkpoint_id="cp-1", success=False), "rollback.failed"),
        (
            ApiDebugMessage(model="m", tokens_in=0, tokens_out=0, duration_ms=1, status="error"),
            "debug.api.failed",
        ),
        (
            SystemNotice(agent_type="main", content="Interrupted", kind="interrupt"),
            "system.interrupted",
        ),
    ],
)
def test_mapper_preserves_branch_specific_terminal_semantics(
    message: Message, expected_type: str
) -> None:
    """Collapsing deltas, failures, and interrupts into generic events would confuse UI state."""
    assert EventMapper().map(message, context=context()).type == expected_type


class ExampleEnum(str, Enum):
    VALUE = "enum-value"


class ExampleModel(BaseModel):
    path: Path


def test_event_payload_is_json_safe_deterministic_and_secret_redacted(tmp_path: Path) -> None:
    """Non-native values or provider credentials must not break or leak through SSE JSON."""
    raw_secret = "provider-secret-value"
    message = ConfigChange(
        config_type="provider",
        old_value={"api_key": raw_secret, "when": NOW, "kind": ExampleEnum.VALUE},
        new_value={
            "credential": SecretStr(raw_secret),
            "auth": {
                "providerToken": raw_secret,
                "nested": {
                    "bearerToken": raw_secret,
                    "privateKey": raw_secret,
                },
            },
            "model": ExampleModel(path=tmp_path / "artifact.txt"),
        },
        timestamp=NOW,
    )

    mapped = EventMapper().map(message, context=context())
    first = mapped.to_json()
    second = mapped.to_json()
    decoded = json.loads(first)

    assert first == second
    assert "\n" not in first
    assert raw_secret not in first
    assert decoded["payload"]["old_value"]["api_key"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["credential"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["providerToken"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["nested"]["bearerToken"] == "[REDACTED]"
    assert decoded["payload"]["new_value"]["auth"]["nested"]["privateKey"] == "[REDACTED]"
    assert decoded["payload"]["old_value"]["when"] == "2026-07-31T10:30:00Z"
    assert decoded["payload"]["old_value"]["kind"] == "enum-value"
    assert decoded["payload"]["new_value"]["model"]["path"] == str(tmp_path / "artifact.txt")


def test_sensitive_config_change_redacts_scalar_old_and_new_values() -> None:
    """Credential rotations often carry scalar secrets rather than keyed nested mappings."""
    mapped = EventMapper().map(
        ConfigChange(
            config_type="providerToken",
            old_value="old-provider-secret",
            new_value="new-provider-secret",
            timestamp=NOW,
        ),
        context=context(),
    )

    assert mapped.payload["old_value"] == "[REDACTED]"
    assert mapped.payload["new_value"] == "[REDACTED]"
    assert "provider-secret" not in mapped.to_json()


def test_error_payload_redacts_key_families_and_embedded_credentials() -> None:
    """Unexpected provider errors must not carry credentials into the SSE wire payload."""
    secrets = {
        "secretAccessKey": "aws-secret-access-value",
        "authorizationHeader": "Bearer header-secret-value",
        "clientKey": "client-key-secret-value",
        "accessKeyId": "AKIAIOSFODNN7EXAMPLE",
        "apiKeyValue": "sk-api-key-secret-value",
        "privateKey": "private-key-secret-value",
    }
    message = Error(
        source="provider",
        message=(
            "request failed with Bearer loose-bearer-secret and "
            "Basic dXNlcjpwYXNzd29yZA==; key sk-looseSecret1234; "
            "account AKIA1234567890ABCDEF"
        ),
        details={
            "provider": secrets,
            "diagnostic": "Authorization: Bearer nested-bearer-secret",
            "tokens_in": 123,
            "tokens_out": 456,
        },
        timestamp=NOW,
    )

    mapped = EventMapper().map(message, context=context())
    serialized = mapped.to_json()

    for raw in [*secrets.values(), "loose-bearer-secret", "dXNlcjpwYXNzd29yZA==", "sk-looseSecret1234", "AKIA1234567890ABCDEF", "nested-bearer-secret"]:
        assert raw not in serialized
    assert mapped.payload["details"]["tokens_in"] == 123
    assert mapped.payload["details"]["tokens_out"] == 456


def test_secret_stems_and_decoded_bytes_are_redacted_without_hiding_token_counts() -> None:
    message = Error(
        source="provider",
        message="failed",
        details={
            "clientSecretValue": "client-secret-value",
            "authorizationMetadata": "metadata-secret-value",
            "accessKeyValue": "AKIA1111111111111111",
            "providerTokenValue": "provider-token-secret-value",
            "accessTokenValue": "access-token-secret-value",
            "wire": b"Bearer byte-secret-value",
            "tokens_in": 11,
            "tokens_out": 12,
            "max_tokens": 13,
        },
        timestamp=NOW,
    )

    mapped = EventMapper().map(message, context=context())
    serialized = mapped.to_json()

    for secret in (
        "client-secret-value",
        "metadata-secret-value",
        "AKIA1111111111111111",
        "provider-token-secret-value",
        "access-token-secret-value",
        "byte-secret-value",
    ):
        assert secret not in serialized
    assert mapped.payload["details"]["tokens_in"] == 11
    assert mapped.payload["details"]["tokens_out"] == 12
    assert mapped.payload["details"]["max_tokens"] == 13


def test_fallback_objects_and_value_aware_token_fields_are_safe() -> None:
    class CredentialObject:
        def __str__(self) -> str:
            return "Bearer custom-object-secret"

    message = Error(
        source="provider",
        message="Bearer authentication and Basic authentication are supported",
        details={
            "fallback": CredentialObject(),
            "working_memory_tokens": 2048,
            "max_total_tokens": 4096.0,
            "token_usage": {
                "prompt_tokens": 12,
                "completion_tokens": 34,
            },
            "provider_token_value": "opaque-provider-value",
        },
        timestamp=NOW,
    )

    mapped = EventMapper().map(message, context=context())
    details = mapped.payload["details"]

    assert "custom-object-secret" not in mapped.to_json()
    assert mapped.payload["message"] == (
        "Bearer authentication and Basic authentication are supported"
    )
    assert details["working_memory_tokens"] == 2048
    assert details["max_total_tokens"] == 4096.0
    assert details["token_usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 34,
    }
    assert details["provider_token_value"] == "[REDACTED]"


def test_numeric_credential_tokens_redact_but_metric_shaped_tokens_remain_numeric() -> None:
    message = Error(
        source="provider",
        message="failed",
        details={
            "accessToken": 101,
            "providerToken": 202.0,
            "control_token": 303,
            "refresh_token": 404,
            "id_token": 505,
            "working_memory_tokens": 1024,
            "max_total_tokens": 2048.0,
            "prompt_tokens": 12,
            "completion_tokens": 34,
            "token_count": 46,
            "token_usage": {"input_tokens": 7, "output_tokens": 8},
        },
        timestamp=NOW,
    )

    details = EventMapper().map(message, context=context()).payload["details"]

    for key in ("accessToken", "providerToken", "control_token", "refresh_token", "id_token"):
        assert details[key] == "[REDACTED]"
    assert details["working_memory_tokens"] == 1024
    assert details["max_total_tokens"] == 2048.0
    assert details["prompt_tokens"] == 12
    assert details["completion_tokens"] == 34
    assert details["token_count"] == 46
    assert details["token_usage"] == {"input_tokens": 7, "output_tokens": 8}


@pytest.mark.parametrize(
    ("config_type", "old_value", "new_value", "expected_old", "expected_new"),
    [
        ("max_tokens", 1024, 2048, 1024, 2048),
        ("working_memory_tokens", 512, 768.0, 512, 768.0),
        ("max_total_tokens", 4096.0, 8192, 4096.0, 8192),
        ("accessToken", 123, 456, "[REDACTED]", "[REDACTED]"),
        ("max_tokens", 1024, "opaque-token-value", 1024, "[REDACTED]"),
    ],
)
def test_config_change_redacts_old_and_new_independently_with_value_aware_rules(
    config_type: str,
    old_value: object,
    new_value: object,
    expected_old: object,
    expected_new: object,
) -> None:
    mapped = EventMapper().map(
        ConfigChange(
            config_type=config_type,
            old_value=old_value,
            new_value=new_value,
            timestamp=NOW,
        ),
        context=context(),
    )

    assert mapped.payload["old_value"] == expected_old
    assert mapped.payload["new_value"] == expected_new


def test_auth_headers_jwt_and_scalar_auth_redact_while_auth_containers_recurse() -> None:
    error = Error(
        source="provider",
        message="Bearer authentication is supported",
        details={
            "authHeader": "opaque-header-value",
            "jwt": "opaque-jwt-value",
            "jwtToken": 123456,
            "auth": {
                "method": "oauth2",
                "accessToken": "nested-access-value",
                "description": "Basic authentication is supported",
            },
        },
        timestamp=NOW,
    )
    tool = ToolResult(
        agent_type="main",
        tool_name="provider_probe",
        result={
            "auth": b"opaque-auth-bytes",
            "authHeader": {"value": "nested-header-value"},
            "jwt": ["jwt-part-one", "jwt-part-two"],
            "authentication": "Bearer authentication remains documented",
        },
        timestamp=NOW,
    )

    error_payload = EventMapper().map(error, context=context()).payload
    tool_payload = EventMapper().map(tool, context=context()).payload

    assert error_payload["message"] == "Bearer authentication is supported"
    assert error_payload["details"]["authHeader"] == "[REDACTED]"
    assert error_payload["details"]["jwt"] == "[REDACTED]"
    assert error_payload["details"]["jwtToken"] == "[REDACTED]"
    assert error_payload["details"]["auth"] == {
        "method": "oauth2",
        "accessToken": "[REDACTED]",
        "description": "Basic authentication is supported",
    }
    assert tool_payload["result"]["auth"] == "[REDACTED]"
    assert tool_payload["result"]["authHeader"] == "[REDACTED]"
    assert tool_payload["result"]["jwt"] == "[REDACTED]"
    assert tool_payload["result"]["authentication"] == "[REDACTED]"


def test_optional_token_metrics_preserve_none_but_unknown_and_credential_tokens_redact() -> None:
    message = Error(
        source="provider",
        message="failed",
        details={
            "max_tokens": None,
            "working_memory_tokens": None,
            "max_total_tokens": None,
            "prompt_tokens": None,
            "token_count": None,
            "token_usage": None,
            "mystery_token": None,
            "accessToken": None,
            "auth": None,
        },
        timestamp=NOW,
    )

    details = EventMapper().map(message, context=context()).payload["details"]

    for key in (
        "max_tokens",
        "working_memory_tokens",
        "max_total_tokens",
        "prompt_tokens",
        "token_count",
        "token_usage",
    ):
        assert details[key] is None
    for key in ("mystery_token", "accessToken", "auth"):
        assert details[key] == "[REDACTED]"


def test_config_change_optional_metric_old_and_new_values_remain_independent() -> None:
    metric = EventMapper().map(
        ConfigChange(
            config_type="max_tokens",
            old_value=None,
            new_value=4096,
            timestamp=NOW,
        ),
        context=context(),
    )
    usage = EventMapper().map(
        ConfigChange(
            config_type="token_usage",
            old_value=None,
            new_value={"input_tokens": 1, "output_tokens": 2},
            timestamp=NOW,
        ),
        context=context(),
    )
    credential = EventMapper().map(
        ConfigChange(
            config_type="jwtToken",
            old_value=None,
            new_value=123,
            timestamp=NOW,
        ),
        context=context(),
    )

    assert metric.payload["old_value"] is None
    assert metric.payload["new_value"] == 4096
    assert usage.payload["old_value"] is None
    assert usage.payload["new_value"] == {"input_tokens": 1, "output_tokens": 2}
    assert credential.payload["old_value"] == "[REDACTED]"
    assert credential.payload["new_value"] == "[REDACTED]"


def test_auth_context_preserves_explicit_metadata_and_redacts_every_opaque_leaf() -> None:
    message = Error(
        source="provider",
        message="Bearer authentication uses the configured provider",
        details={
            "auth": {
                "method": "oauth2",
                "scheme": "Bearer",
                "type": "service",
                "provider": "example-provider",
                "description": "Bearer authentication is supported",
                "configured": True,
                "enabled": False,
                "value": "opaque-value",
                "endpoint": "https://identity.example/token",
                "header": "opaque-header",
                "jwt": "opaque-jwt",
                "token": 123,
                "secret": None,
                "credential": b"opaque-credential",
                "nested": {
                    "provider": "nested-provider",
                    "opaque": "nested-opaque-value",
                },
            }
        },
        timestamp=NOW,
    )

    payload = EventMapper().map(message, context=context()).payload

    assert payload["message"] == "Bearer authentication uses the configured provider"
    assert payload["details"]["auth"] == {
        "method": "oauth2",
        "scheme": "Bearer",
        "type": "service",
        "provider": "example-provider",
        "description": "Bearer authentication is supported",
        "configured": True,
        "enabled": False,
        "value": "[REDACTED]",
        "endpoint": "[REDACTED]",
        "header": "[REDACTED]",
        "jwt": "[REDACTED]",
        "token": "[REDACTED]",
        "secret": "[REDACTED]",
        "credential": "[REDACTED]",
        "nested": {
            "provider": "nested-provider",
            "opaque": "[REDACTED]",
        },
    }


def test_prefixed_auth_containers_redact_unlabelled_scalars_and_never_execute_custom_values() -> None:
    calls = {"str": 0, "iter": 0}

    class OpaqueObject:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("auth sanitization must not stringify opaque objects")

    class OpaqueIterable:
        def __iter__(self):
            calls["iter"] += 1
            raise AssertionError("auth sanitization must not iterate custom objects")

        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("auth sanitization must not stringify custom iterables")

    message = ToolResult(
        agent_type="main",
        tool_name="provider_probe",
        result={
            "auth_tuple": ("tuple-secret", {"method": "basic", "value": "hidden"}),
            "authSet": {"set-secret-one", "set-secret-two"},
            "authFrozen": frozenset({"frozen-secret"}),
            "auth_payload": {
                "custom_object": OpaqueObject(),
                "custom_iterable": OpaqueIterable(),
                "parts": [
                    "list-secret",
                    {"scheme": "Bearer", "unknown": "hidden"},
                ],
            },
            "authHeader": {"value": "whole-header-is-secret"},
            "jwt": ["whole-jwt-is-secret"],
        },
        timestamp=NOW,
    )

    result = EventMapper().map(message, context=context()).payload["result"]

    assert result["auth_tuple"] == [
        "[REDACTED]",
        {"method": "basic", "value": "[REDACTED]"},
    ]
    assert result["authSet"] == ["[REDACTED]", "[REDACTED]"]
    assert result["authFrozen"] == ["[REDACTED]"]
    assert result["auth_payload"] == {
        "custom_object": "[REDACTED]",
        "custom_iterable": "[REDACTED]",
        "parts": [
            "[REDACTED]",
            {"scheme": "Bearer", "unknown": "[REDACTED]"},
        ],
    }
    assert result["authHeader"] == "[REDACTED]"
    assert result["jwt"] == "[REDACTED]"
    assert calls == {"str": 0, "iter": 0}


def test_auth_prefix_matching_does_not_capture_author_or_authority_fields() -> None:
    message = Error(
        source="provider",
        message="failed",
        details={
            "author": "Ada",
            "authority": "standards-board",
            "authorized": True,
            "authentication": "Bearer authentication is supported",
        },
        timestamp=NOW,
    )

    assert EventMapper().map(message, context=context()).payload["details"] == {
        "author": "Ada",
        "authority": "standards-board",
        "authorized": True,
        "authentication": "[REDACTED]",
    }


def test_mapper_closes_authentication_and_token_usage_review_reproducers() -> None:
    calls = {"str": 0, "repr": 0, "iter": 0}

    class HostileWithoutSafeHooks:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("must not repr")

        def __iter__(self):
            calls["iter"] += 1
            raise AssertionError("must not iterate")

    hostile = HostileWithoutSafeHooks()
    message = Error(
        source="provider",
        message="failed",
        details={
            "authentication": "opaque-auth-value-123",
            "authentication_header": "opaque-header-value-123",
            "authenticationConfig": {"opaque": "value-123", "custom": hostile},
            "token_usage": {
                "input_tokens": 3,
                "mystery": "opaque-token-value-123",
                "custom": hostile,
            },
        },
        timestamp=NOW,
    )

    details = EventMapper().map(message, context=context()).payload["details"]

    assert details == {
        "authentication": "[REDACTED]",
        "authentication_header": "[REDACTED]",
        "authenticationConfig": {
            "opaque": "[REDACTED]",
            "custom": "[REDACTED]",
        },
        "token_usage": {
            "input_tokens": 3,
            "mystery": "[REDACTED]",
            "custom": "[REDACTED]",
        },
    }
    assert calls == {"str": 0, "repr": 0, "iter": 0}


@pytest.mark.parametrize(
    ("config_type", "old_value", "new_value", "expected_old", "expected_new"),
    [
        (
            "authentication",
            "old-auth-secret-123",
            {"method": "oauth2", "opaque": "new-auth-secret-123"},
            "[REDACTED]",
            {"method": "oauth2", "opaque": "[REDACTED]"},
        ),
        (
            "authenticationConfig",
            {"provider": "example", "opaque": "old-config-secret-123"},
            {"configured": True, "value": "new-config-secret-123"},
            {"provider": "example", "opaque": "[REDACTED]"},
            {"configured": True, "value": "[REDACTED]"},
        ),
        (
            "token_usage",
            {"input_tokens": 2, "opaque": "old-usage-secret-123"},
            {"output_tokens": 3, "mystery": "new-usage-secret-123"},
            {"input_tokens": 2, "opaque": "[REDACTED]"},
            {"output_tokens": 3, "mystery": "[REDACTED]"},
        ),
    ],
)
def test_config_change_applies_context_policy_independently(
    config_type: str,
    old_value: object,
    new_value: object,
    expected_old: object,
    expected_new: object,
) -> None:
    mapped = EventMapper().map(
        ConfigChange(
            config_type=config_type,
            old_value=old_value,
            new_value=new_value,
            timestamp=NOW,
        ),
        context=context(),
    )

    assert mapped.payload["old_value"] == expected_old
    assert mapped.payload["new_value"] == expected_new
    wire = mapped.to_json()
    for marker in ("auth-secret-123", "config-secret-123", "usage-secret-123"):
        assert marker not in wire


def test_only_explicit_token_metrics_preserve_numeric_or_none_values() -> None:
    known_metrics = {
        "max_tokens": None,
        "working_memory_tokens": 1,
        "input_tokens": None,
        "output_tokens": 2,
        "prompt_tokens": None,
        "completion_tokens": 3,
        "total_tokens": None,
        "cached_tokens": 4,
        "reasoning_tokens": None,
        "max_total_tokens": 5,
        "agent_cumulative_input_tokens": None,
        "agent_cumulative_output_tokens": 6,
        "tokens_in": None,
        "tokens_out": 7,
        "token_count": None,
        "token_usage": {"input_tokens": 8, "output_tokens": None},
    }
    unknown_tokens = {
        "mystery_tokens": None,
        "opaque_tokens": 9,
        "api_tokens": 10.0,
        "csrf_tokens": None,
        "oauth_tokens": 11,
        "mystery_token_count": 12,
        "opaque_token": "opaque",
    }
    message = Error(
        source="provider",
        message="failed",
        details={**known_metrics, **unknown_tokens},
        timestamp=NOW,
    )

    details = EventMapper().map(message, context=context()).payload["details"]

    assert {key: details[key] for key in known_metrics} == known_metrics
    assert {key: details[key] for key in unknown_tokens} == {
        key: "[REDACTED]" for key in unknown_tokens
    }


@pytest.mark.parametrize("value", [None, 13, 13.5, "opaque"])
@pytest.mark.parametrize(
    "config_type",
    ["mystery_tokens", "opaque_tokens", "api_tokens", "csrf_tokens", "oauth_tokens"],
)
def test_config_change_rejects_unknown_token_suffixes_for_every_value_type(
    config_type: str,
    value: object,
) -> None:
    mapped = EventMapper().map(
        ConfigChange(
            config_type=config_type,
            old_value=value,
            new_value=value,
            timestamp=NOW,
        ),
        context=context(),
    )

    assert mapped.payload["old_value"] == "[REDACTED]"
    assert mapped.payload["new_value"] == "[REDACTED]"


def test_mapper_normalizes_top_level_and_payload_datetimes_to_utc() -> None:
    """Mixed local/offset timestamps must not make ordering ambiguous to remote clients."""
    offset_time = datetime(2026, 7, 31, 12, 30, tzinfo=timezone(timedelta(hours=8)))
    naive_payload = datetime(2026, 7, 31, 9, 15)
    expected_naive = naive_payload.astimezone(UTC).isoformat().replace("+00:00", "Z")
    message = Error(
        source="system",
        message="failed",
        details={"observed_at": naive_payload, "offset_at": offset_time},
        timestamp=offset_time,
    )

    mapped = EventMapper().map(message, context=context())
    wire = json.loads(mapped.to_json())

    assert mapped.timestamp == datetime(2026, 7, 31, 4, 30, tzinfo=UTC)
    assert wire["timestamp"] == "2026-07-31T04:30:00Z"
    assert wire["payload"]["details"]["observed_at"] == expected_naive
    assert wire["payload"]["details"]["offset_at"] == "2026-07-31T04:30:00Z"


def test_event_envelope_uses_locked_aliases_and_explicit_context() -> None:
    """Snake-case wire fields or stale inferred identifiers would violate the frontend contract."""
    mapped = EventMapper().map(
        AgentResponse(agent_type="other", content="done", message_id="internal-id", timestamp=NOW),
        context=context(sequence=7),
    )
    body = json.loads(mapped.to_json())

    assert list(body) == [
        "schemaVersion",
        "streamId",
        "eventId",
        "sequence",
        "type",
        "timestamp",
        "projectId",
        "sessionId",
        "agentId",
        "runId",
        "messageId",
        "payload",
    ]
    assert body | {"payload": None} == {
        "schemaVersion": 1,
        "streamId": "test-stream",
        "eventId": "test-stream:evt-7",
        "sequence": 7,
        "type": "agent.message.completed",
        "timestamp": "2026-07-31T10:30:00Z",
        "projectId": "project-1",
        "sessionId": "session-1",
        "agentId": "main",
        "runId": "run-1",
        "messageId": "message-1",
        "payload": None,
    }


def test_unknown_future_message_subtype_fails_visibly() -> None:
    """A future internal message must fail mapper coverage instead of disappearing silently."""

    class FutureMessage(Message):
        type: MessageType = MessageType.SYSTEM_NOTICE

    with pytest.raises(UnsupportedMessageTypeError, match="FutureMessage"):
        EventMapper().map(FutureMessage(), context=context())


def test_replay_returns_only_events_strictly_after_a_present_cursor() -> None:
    replay = ReplayBuffer(capacity=4)
    for sequence in range(1, 4):
        replay.append(event(sequence))

    assert [item.sequence for item in replay.after("test-stream:evt-1").events] == [2, 3]
    assert replay.after("test-stream:evt-3").events == ()
    assert replay.after("test-stream:evt-0").events == tuple(
        event(item) for item in range(1, 4)
    )


def test_evt_zero_requires_resync_when_replay_no_longer_starts_at_one() -> None:
    """Treating evt-0 as retained after eviction would silently return a truncated history."""
    replay = ReplayBuffer(capacity=2)
    replay.append(event(1))
    replay.append(event(2))
    replay.append(event(3))

    outcome = replay.after("test-stream:evt-0")

    assert outcome.events == ()
    assert outcome.requires_resync is True
    assert outcome.reason == "evicted"


def test_evt_zero_is_valid_for_empty_or_complete_from_one_replay() -> None:
    empty = ReplayBuffer(capacity=2)
    complete = ReplayBuffer(capacity=2)
    complete.append(event(1))

    assert empty.after("test-stream:evt-0").events == ()
    assert complete.after("test-stream:evt-0").events == (event(1),)


@pytest.mark.parametrize(
    ("cursor", "reason"),
    [
        ("test-stream:evt-1", "evicted"),
        ("test-stream:evt-3", "unknown"),
        ("test-stream:evt-99", "future"),
        ("not-an-event", "malformed"),
    ],
)
def test_replay_requests_resync_for_unrecoverable_cursor(cursor: str, reason: str) -> None:
    replay = ReplayBuffer(capacity=2)
    replay.append(event(2))
    replay.append(event(4))

    outcome = replay.after(cursor)

    assert outcome.events == ()
    assert outcome.requires_resync is True
    assert outcome.reason == reason


@pytest.mark.parametrize("capacity", [0, -1, True])
def test_replay_rejects_invalid_capacity(capacity: int) -> None:
    with pytest.raises(ValueError, match="capacity"):
        ReplayBuffer(capacity=capacity)


def test_replay_is_bounded_and_requires_strict_monotonic_sequence() -> None:
    replay = ReplayBuffer(capacity=2)
    replay.append(event(1))
    replay.append(event(2))
    replay.append(event(3))

    assert replay.snapshot() == (event(2), event(3))
    with pytest.raises(ValueError, match="monotonic"):
        replay.append(event(3))


def test_replay_rejects_event_id_that_disagrees_with_sequence() -> None:
    """A mismatched cursor ID would make a retained event impossible to recover exactly."""
    replay = ReplayBuffer(capacity=2)
    mismatched = event(1).model_copy(update={"event_id": "test-stream:evt-99"})

    with pytest.raises(ValueError, match="event ID"):
        replay.append(mismatched)
