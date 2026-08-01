"""Direct policy tests for public event payload sanitization."""

from __future__ import annotations

from collections.abc import Iterator, Mapping

import pytest

from manyselves.webapi.events.sanitizer import EventPayloadSanitizer


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("authentication", "opaque-auth-value", "[REDACTED]"),
        ("authenticationHeader", "opaque-header", "[REDACTED]"),
        ("authentication_header", {"value": "opaque"}, "[REDACTED]"),
        (
            "authenticationConfig",
            {"method": "oauth2", "provider": "example", "opaque": "secret"},
            {"method": "oauth2", "provider": "example", "opaque": "[REDACTED]"},
        ),
        ("author", "Ada", "Ada"),
        ("authority", "standards-board", "standards-board"),
        ("authorized", True, True),
    ],
)
def test_authentication_boundaries_fail_closed_without_harming_author_fields(
    name: str,
    value: object,
    expected: object,
) -> None:
    assert EventPayloadSanitizer().sanitize_field(name, value) == expected


def test_authentication_context_keeps_only_metadata_scalars() -> None:
    value = {
        "description": "Bearer authentication is supported",
        "configured": True,
        "value": "opaque-value",
        "nested": {"scheme": "Bearer", "unknown": "opaque-nested"},
        "parts": [
            "opaque-list-value",
            {"provider": "example", "jwt": "opaque-jwt"},
        ],
    }

    assert EventPayloadSanitizer().sanitize_field("authenticationConfig", value) == {
        "description": "Bearer authentication is supported",
        "configured": True,
        "value": "[REDACTED]",
        "nested": {"scheme": "Bearer", "unknown": "[REDACTED]"},
        "parts": [
            "[REDACTED]",
            {"provider": "example", "jwt": "[REDACTED]"},
        ],
    }


def test_sensitive_contexts_never_execute_unknown_object_hooks() -> None:
    calls = {"str": 0, "repr": 0, "iter": 0}

    class Hostile:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("must not repr")

        def __iter__(self) -> Iterator[object]:
            calls["iter"] += 1
            raise AssertionError("must not iterate")

    sanitizer = EventPayloadSanitizer()
    assert sanitizer.sanitize_field("authenticationConfig", Hostile()) == "[REDACTED]"
    assert sanitizer.sanitize_field("token_usage", Hostile()) == "[REDACTED]"
    assert calls == {"str": 0, "repr": 0, "iter": 0}


def test_cycles_and_depth_limit_fail_closed() -> None:
    cyclic: dict[str, object] = {}
    cyclic["self"] = cyclic
    deep: object = "too-deep"
    for _ in range(34):
        deep = [deep]

    sanitizer = EventPayloadSanitizer()
    assert sanitizer.sanitize_field("details", cyclic) == {"self": "[REDACTED]"}

    sanitized = sanitizer.sanitize_field("details", deep)
    cursor = sanitized
    for _ in range(32):
        assert type(cursor) is list
        cursor = cursor[0]
    assert cursor == "[REDACTED]"


def test_token_usage_keeps_only_exact_numeric_metrics() -> None:
    sanitizer = EventPayloadSanitizer()
    value = {
        "input_tokens": 10,
        "output_tokens": None,
        "agent_cumulative_input_tokens": 20,
        "mystery": "opaque",
        "api_tokens": 30,
        "enabled": True,
        "nested": {"total_tokens": 40, "opaque": "secret"},
        "entries": [{"prompt_tokens": 5, "value": "secret"}, "opaque-list-leaf"],
    }

    assert sanitizer.sanitize_field("token_usage", value) == {
        "input_tokens": 10,
        "output_tokens": None,
        "agent_cumulative_input_tokens": 20,
        "mystery": "[REDACTED]",
        "api_tokens": "[REDACTED]",
        "enabled": "[REDACTED]",
        "nested": {"total_tokens": 40, "opaque": "[REDACTED]"},
        "entries": [
            {"prompt_tokens": 5, "value": "[REDACTED]"},
            "[REDACTED]",
        ],
    }


@pytest.mark.parametrize(
    "name",
    [
        "api_tokens",
        "csrf_tokens",
        "oauth_tokens",
        "opaque_tokens",
        "mystery_tokens",
        "mystery_token_count",
    ],
)
@pytest.mark.parametrize("value", [None, 1, 1.5, "opaque", [1], {"value": 1}])
def test_unknown_token_fields_fail_closed(name: str, value: object) -> None:
    assert EventPayloadSanitizer().sanitize_field(name, value) == "[REDACTED]"


def test_token_usage_rejects_unsupported_containers_without_hooks() -> None:
    calls = {"iter": 0, "items": 0, "repr": 0, "str": 0}

    class HostileMapping(Mapping[object, object]):
        def __getitem__(self, key: object) -> object:
            raise AssertionError("must not index")

        def __iter__(self) -> Iterator[object]:
            calls["iter"] += 1
            raise AssertionError("must not iterate")

        def __len__(self) -> int:
            raise AssertionError("must not inspect length")

        def items(self):
            calls["items"] += 1
            raise AssertionError("must not read items")

    class HostileIterable:
        def __iter__(self) -> Iterator[object]:
            calls["iter"] += 1
            raise AssertionError("must not iterate")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("must not repr")

        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("must not stringify")

    sanitizer = EventPayloadSanitizer()
    for value in ({1}, frozenset({1}), HostileMapping(), HostileIterable()):
        assert sanitizer.sanitize_field("token_usage", value) == "[REDACTED]"
    assert calls == {"iter": 0, "items": 0, "repr": 0, "str": 0}


def test_non_string_keys_are_redacted_without_collision_or_hooks() -> None:
    calls = {"str": 0, "repr": 0}

    class HostileKey:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("must not repr")

    sanitized = EventPayloadSanitizer().sanitize_mapping(
        {HostileKey(): "first", object(): "second"}
    )

    assert sanitized == {
        "[REDACTED]": "[REDACTED]",
        "[REDACTED]#2": "[REDACTED]",
    }
    assert calls == {"str": 0, "repr": 0}
