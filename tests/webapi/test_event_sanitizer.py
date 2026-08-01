"""Direct policy tests for public event payload sanitization."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from pydantic import BaseModel

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


@pytest.mark.parametrize(
    "name",
    [
        "authApiKey",
        "authAuthorization",
        "authCookie",
        "authPrivateKey",
        "authAccessKey",
        "authClientKey",
        "authBearer",
    ],
)
def test_explicit_secret_meaning_wins_before_authentication_context(name: str) -> None:
    value = {"method": "oauth2", "provider": "example", "opaque": "secret"}

    assert EventPayloadSanitizer().sanitize_field(name, value) == "[REDACTED]"


@pytest.mark.parametrize(
    "name",
    [
        "author",
        "Author",
        "AUTHOR",
        "AuThOr",
        "authority",
        "Authority",
        "AUTHORITY",
        "AuThOrItY",
        "authorized",
        "Authorized",
        "AUTHORIZED",
        "AuThOrIzEd",
        "AUTHORName",
        "AUTHORITYConfig",
        "AUTHORIZEDState",
    ],
)
def test_authentication_boundary_nonmatches_are_case_independent(name: str) -> None:
    value = {"safe": "value"}

    assert EventPayloadSanitizer().sanitize_field(name, value) == value


@pytest.mark.parametrize(
    "name",
    [
        "auth",
        "AUTH",
        "authentication",
        "AUTHENTICATION",
        "auth_config",
        "AUTH_CONFIG",
        "authentication-payload",
        "Authentication-Payload",
        "authConfig",
        "AuthConfig",
        "authenticationConfig",
        "AuthenticationConfig",
    ],
)
def test_authentication_roots_preserve_exact_delimiter_and_camel_forms(name: str) -> None:
    value = {"method": "oauth2", "opaque": "secret"}

    assert EventPayloadSanitizer().sanitize_field(name, value) == {
        "method": "oauth2",
        "opaque": "[REDACTED]",
    }


@pytest.mark.parametrize("name", ["AUTHConfig", "AUTHENTICATIONConfig"])
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (
            {"method": "oauth2", "opaque": "secret"},
            {"method": "oauth2", "opaque": "[REDACTED]"},
        ),
        (["opaque-list-value"], ["[REDACTED]"]),
        (("opaque-tuple-value",), ["[REDACTED]"]),
        ({"opaque-set-value"}, ["[REDACTED]"]),
        (frozenset({"opaque-frozen-value"}), ["[REDACTED]"]),
    ],
    ids=["dict", "list", "tuple", "set", "frozenset"],
)
def test_acronym_camel_authentication_roots_use_authentication_context(
    name: str,
    value: object,
    expected: object,
) -> None:
    assert EventPayloadSanitizer().sanitize_field(name, value) == expected


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
    "value",
    [
        {"input_tokens": 7},
        [7],
        (7,),
        True,
        "7",
    ],
    ids=["dict", "list", "tuple", "bool", "string"],
)
def test_token_usage_metric_keys_reject_every_non_numeric_value(value: object) -> None:
    assert EventPayloadSanitizer().sanitize_field(
        "token_usage", {"input_tokens": value}
    ) == {"input_tokens": "[REDACTED]"}


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


def test_only_native_path_types_are_serialized() -> None:
    calls = {"str": 0}
    native = Path("artifact.txt")

    class HostilePath(type(native)):
        def __str__(self) -> str:
            calls["str"] += 1
            return "hostile-path-secret"

    sanitizer = EventPayloadSanitizer()

    assert sanitizer.sanitize_field("path", HostilePath("opaque")) == "[REDACTED]"
    assert calls == {"str": 0}
    assert sanitizer.sanitize_field("path", native) == "artifact.txt"


def test_base_model_identity_is_tracked_before_dump_recursion() -> None:
    calls = {"dump": 0}

    class SelfReturningModel(BaseModel):
        def model_dump(self, *args: object, **kwargs: object) -> object:
            calls["dump"] += 1
            if calls["dump"] > 1:
                raise AssertionError("model cycle must be detected before a second dump")
            return self

    assert (
        EventPayloadSanitizer().sanitize_field("details", SelfReturningModel())
        == "[REDACTED]"
    )
    assert calls == {"dump": 1}


def test_normal_base_model_is_preserved_as_a_sanitized_mapping() -> None:
    class NormalModel(BaseModel):
        label: str
        count: int

    assert EventPayloadSanitizer().sanitize_field(
        "details", NormalModel(label="ready", count=2)
    ) == {"label": "ready", "count": 2}


def test_base_model_consumes_depth_before_dumped_mapping_recursion() -> None:
    class NormalModel(BaseModel):
        label: str

    deep: object = NormalModel(label="too-deep")
    for _ in range(31):
        deep = [deep]

    sanitized = EventPayloadSanitizer().sanitize_field("details", deep)
    cursor = sanitized
    for _ in range(31):
        assert type(cursor) is list
        cursor = cursor[0]
    assert cursor == "[REDACTED]"
