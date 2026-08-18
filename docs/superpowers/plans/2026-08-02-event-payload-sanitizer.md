# Event Payload Sanitizer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace Task 5's fail-open event redaction helpers with one context-driven, fail-closed sanitizer so the two P1 credential leaks are closed before OpenAPI and React work begins.

**Architecture:** Add `EventPayloadSanitizer` as the only recursive payload-cleaning component. It classifies each field before traversal and applies generic, authentication, or token-usage policy with exact built-in type checks, bounded recursion, and no untrusted conversion hooks. `EventMapper` retains message-to-event translation but delegates all payload and `ConfigChange` value cleaning to the sanitizer.

**Tech Stack:** Python 3.12, Pydantic 2, pytest, pytest-asyncio, Ruff, existing FastAPI/SSE event broker.

## Global Constraints

- Do not modify `manyselves/core/**` or `manyselves/templates/**`.
- Keep `EventEnvelope` schema version 1 and every current event type unchanged.
- Preserve safe JSON structure; replace unprovably safe sensitive values with exact string `[REDACTED]` without dropping or rejecting the event.
- Never call an unknown object's `__str__`, `__repr__`, or `__iter__` while sanitizing.
- `ConfigChange.old_value` and `new_value` must call the same field-policy entry point independently.
- Preserve ordinary prose outside credential fields, including `Bearer authentication` and `Basic authentication`.
- Use a clean Linux/WSL Python 3.12 environment outside the worktree; never let Windows `uv` mutate the worktree `.venv`.
- The only controller-owned untracked file, `docs/phase1/implementation-status.md`, must never be staged by the implementer.
- The locked design is `docs/superpowers/specs/2026-08-02-event-payload-sanitizer-design.md`.

---

### Task 1: Replace Event Redaction with a Context-Driven Sanitizer

**Files:**
- Create: `manyselves/webapi/events/sanitizer.py`
- Modify: `manyselves/webapi/events/mapper.py`
- Modify: `manyselves/webapi/events/__init__.py`
- Create: `tests/webapi/test_event_sanitizer.py`
- Modify: `tests/webapi/test_event_mapper.py`
- Modify: `tests/webapi/test_sse.py`

**Interfaces:**
- Consumes: Pydantic `BaseModel`, `SecretStr`, and `SecretBytes`; the current `Message.model_dump()` result; `ConfigChange.config_type`, `old_value`, and `new_value`.
- Produces: `EventPayloadSanitizer.sanitize_mapping(value: dict[object, object]) -> dict[str, Any]` and `EventPayloadSanitizer.sanitize_field(name: object, value: object) -> Any`.
- Preserves: `EventMapper.map(message, *, context) -> EventEnvelope`, all 23 current concrete message mappings, `EventEnvelope` v1, broker replay, and SSE framing.

- [ ] **Step 1: Create the isolated Linux test environment**

Run from WSL:

```bash
cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion
uv venv --python 3.12 /tmp/manyselves-sanitizer-py312
uv pip install --python /tmp/manyselves-sanitizer-py312/bin/python -e '.[dev]'
/tmp/manyselves-sanitizer-py312/bin/python --version
```

Expected: the final command reports Python 3.12.x and no environment files are created inside the worktree.

- [ ] **Step 2: Write failing authentication-boundary unit tests**

Create `tests/webapi/test_event_sanitizer.py` with direct public-interface tests. The minimum boundary matrix is:

```python
from __future__ import annotations

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
```

Add an authentication-container test whose input contains:

```python
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
```

- [ ] **Step 3: Write failing no-hook and recursion-guard tests**

Add hostile objects whose hooks raise if called:

```python
def test_sensitive_contexts_never_execute_unknown_object_hooks() -> None:
    calls = {"str": 0, "repr": 0, "iter": 0}

    class Hostile:
        def __str__(self) -> str:
            calls["str"] += 1
            raise AssertionError("must not stringify")

        def __repr__(self) -> str:
            calls["repr"] += 1
            raise AssertionError("must not repr")

        def __iter__(self):
            calls["iter"] += 1
            raise AssertionError("must not iterate")

    sanitizer = EventPayloadSanitizer()
    assert sanitizer.sanitize_field("authenticationConfig", Hostile()) == "[REDACTED]"
    assert sanitizer.sanitize_field("token_usage", Hostile()) == "[REDACTED]"
    assert calls == {"str": 0, "repr": 0, "iter": 0}
```

Add a self-referential exact built-in dictionary and a 34-level nested list. Assert that the cycle and every value beyond depth 32 become `[REDACTED]` without raising `RecursionError`.

```python
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
```

- [ ] **Step 4: Write failing token-usage policy tests**

Use the exact project metric allowlist and explicit unsafe counterexamples:

```python
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
        "entries": [{"prompt_tokens": 5, "value": "[REDACTED]"}, "[REDACTED]"],
    }
```

Parametrize `api_tokens`, `csrf_tokens`, `oauth_tokens`, `opaque_tokens`, `mystery_tokens`, and `mystery_token_count` with `None`, integer, float, string, list, and dictionary values; every result must be `[REDACTED]`. Assert that a `token_usage` set, frozenset, custom mapping, or custom iterable is wholly `[REDACTED]` without executing hooks.

```python
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
```

- [ ] **Step 5: Write failing mapper and ConfigChange integration tests**

Extend `tests/webapi/test_event_mapper.py` with the independent-review reproducers:

```python
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
```

Add parametrized `ConfigChange` cases for `authentication`, `authenticationConfig`, and `token_usage`. Assert that old/new values are sanitized independently by the same policy and that opaque values never appear in `EventEnvelope.to_json()`.

- [ ] **Step 6: Write a failing SSE/replay boundary test**

Extend `tests/webapi/test_sse.py` using the existing `EventBroker`, `TrackingBus`, and `resolve_context` fixtures:

```python
@pytest.mark.asyncio
async def test_sensitive_payload_is_absent_from_live_and_replayed_events() -> None:
    broker = EventBroker(
        bus=TrackingBus(),
        context_resolver=resolve_context,
        replay_capacity=4,
        client_capacity=4,
    )
    live = await broker.register(None)
    await broker.publish_internal(
        Error(
            source="provider",
            message="failed",
            details={
                "authenticationConfig": {"opaque": "auth-secret-123"},
                "token_usage": {"mystery": "usage-secret-123", "input_tokens": 1},
            },
        )
    )
    live_event = await live.get()
    replayed = await broker.register("evt-0")
    replay_event = await replayed.get()

    for event in (live_event, replay_event):
        wire = event.to_json()
        assert "auth-secret-123" not in wire
        assert "usage-secret-123" not in wire
        assert '"input_tokens":1' in wire

    await broker.close()
```

Use the current `Error` constructor fields exactly as defined in `manyselves/interfaces/types.py`; do not add a new public event shape.

- [ ] **Step 7: Run the new tests against `7656262` and verify RED**

Run from WSL:

```bash
cd /mnt/d/yuanxi-algo/manyselves/.worktrees/phase1-completion
/tmp/manyselves-sanitizer-py312/bin/python -m pytest \
  tests/webapi/test_event_sanitizer.py \
  tests/webapi/test_event_mapper.py \
  tests/webapi/test_sse.py \
  -q
```

Expected: the new authentication and token-usage cases fail because `EventPayloadSanitizer` does not exist and the two P1 reproducers remain observable. Existing tests may pass; import errors must be resolved into behavior failures before production code is written.

- [ ] **Step 8: Implement the minimal sanitizer**

Create `manyselves/webapi/events/sanitizer.py` with these stable definitions:

```python
from __future__ import annotations

from enum import Enum, auto
from typing import Any


REDACTED = "[REDACTED]"
MAX_SANITIZE_DEPTH = 32

TOKEN_METRIC_KEYS = frozenset(
    {
        "inputtokens",
        "outputtokens",
        "prompttokens",
        "completiontokens",
        "totaltokens",
        "cachedtokens",
        "reasoningtokens",
        "maxtokens",
        "maxtotaltokens",
        "workingmemorytokens",
        "tokensin",
        "tokensout",
        "tokencount",
        "agentcumulativeinputtokens",
        "agentcumulativeoutputtokens",
    }
)
AUTH_METADATA_KEYS = frozenset(
    {"method", "scheme", "type", "provider", "description", "configured", "enabled"}
)


class _Context(Enum):
    GENERIC = auto()
    AUTHENTICATION = auto()
    TOKEN_USAGE = auto()


class EventPayloadSanitizer:
    def sanitize_mapping(self, value: dict[object, object]) -> dict[str, Any]:
        result = self._sanitize(value, context=_Context.GENERIC, depth=0, active=set())
        if type(result) is not dict:
            raise TypeError("Sanitized event payload must be an object")
        return result

    def sanitize_field(self, name: object, value: object) -> Any:
        return self._sanitize_field(name, value, depth=0, active=set())

    def _sanitize_field(
        self,
        name: object,
        value: object,
        *,
        depth: int,
        active: set[int],
    ) -> Any:
        key = _normalized_key(name)
        if key is None:
            return REDACTED
        if _is_auth_boundary(name):
            if _contains_auth_secret_marker(key):
                return REDACTED
            if type(value) in AUTH_CONTAINER_TYPES:
                return self._sanitize(
                    value,
                    context=_Context.AUTHENTICATION,
                    depth=depth,
                    active=active,
                )
            return REDACTED
        if key == "tokenusage":
            if value is None:
                return None
            if type(value) in (dict, list, tuple):
                return self._sanitize(
                    value,
                    context=_Context.TOKEN_USAGE,
                    depth=depth,
                    active=active,
                )
            return REDACTED
        if "token" in key:
            if key in TOKEN_METRIC_KEYS and _is_metric_value(value):
                return value
            return REDACTED
        if _is_secret_key(key):
            return REDACTED
        return self._sanitize(
            value,
            context=_Context.GENERIC,
            depth=depth,
            active=active,
        )

    def _sanitize(
        self,
        value: object,
        *,
        context: _Context,
        depth: int,
        active: set[int],
    ) -> Any:
        if depth > MAX_SANITIZE_DEPTH:
            return REDACTED
        if type(value) in AUTH_CONTAINER_TYPES and id(value) in active:
            return REDACTED
        if context is _Context.AUTHENTICATION:
            return self._sanitize_auth(value, depth=depth, active=active)
        if context is _Context.TOKEN_USAGE:
            return self._sanitize_token_usage(value, depth=depth, active=active)
        return self._sanitize_generic(value, depth=depth, active=active)
```

Implement the referenced private helpers for exact string-key normalization, collision-free redacted keys, auth-boundary recognition, secret markers, metric values, generic traversal, authentication traversal, token-usage traversal, active-container cycle detection, depth 32, and credential-text scrubbing. Use exact built-in type membership where the design forbids custom hooks. Do not import the mapper from this module.

- [ ] **Step 9: Integrate the sanitizer into `EventMapper`**

Modify `manyselves/webapi/events/mapper.py` so its constructor supports dependency injection while preserving `EventMapper()`:

```python
class EventMapper:
    def __init__(self, sanitizer: EventPayloadSanitizer | None = None) -> None:
        self._sanitizer = sanitizer or EventPayloadSanitizer()

    def map(self, message: Message, *, context: EventContext) -> EventEnvelope:
        event_type = self._event_type(message)
        dumped = message.model_dump(exclude={"type", "timestamp"})
        payload = self._sanitizer.sanitize_mapping(dumped)
        if type(message) is ConfigChange:
            payload["old_value"] = self._sanitizer.sanitize_field(
                message.config_type, message.old_value
            )
            payload["new_value"] = self._sanitizer.sanitize_field(
                message.config_type, message.new_value
            )
        return EventEnvelope(
            eventId=context.event_id,
            sequence=context.sequence,
            type=event_type,
            timestamp=message.timestamp,
            projectId=context.project_id,
            sessionId=context.session_id,
            agentId=context.agent_id,
            runId=context.run_id,
            messageId=context.message_id,
            payload=payload,
        )
```

Remove `_sensitive_key`, `_json_safe`, `_auth_json_safe`, and their constants/imports from the mapper. Re-export `EventPayloadSanitizer` and `REDACTED` from `manyselves/webapi/events/__init__.py` only if existing package conventions re-export public event components.

- [ ] **Step 10: Run the focused tests and verify GREEN**

Run:

```bash
/tmp/manyselves-sanitizer-py312/bin/python -m pytest \
  tests/webapi/test_event_sanitizer.py \
  tests/webapi/test_event_mapper.py \
  tests/webapi/test_sse.py \
  -q
```

Expected: all selected tests pass, no opaque repro string appears in assertion output, and hostile hook counters remain zero.

- [ ] **Step 11: Refactor only after GREEN and re-run the focused tests**

Consolidate duplicated exact-type/context traversal without changing the public methods or policy constants. Keep `sanitizer.py` independent of broker, replay, routes, and internal core modules.

Run the Step 10 command again. Expected: identical passing count and no warnings.

- [ ] **Step 12: Run the complete fresh verification gate**

Run from WSL after the final production change:

```bash
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/webapi -q
/tmp/manyselves-sanitizer-py312/bin/python -m pytest tests/application tests/webapi -q
/tmp/manyselves-sanitizer-py312/bin/python -m ruff check \
  manyselves/application manyselves/webapi tests/application tests/webapi
/tmp/manyselves-sanitizer-py312/bin/python scripts/check_phase1_core_freeze.py \
  --base-file docs/phase1/core-freeze-base.txt
```

Run from PowerShell:

```powershell
git diff --name-only 9972cd8..HEAD -- manyselves/core manyselves/templates
git diff --check
git status --short
```

Expected: both pytest commands pass with zero failures; Ruff reports `All checks passed!`; the freeze script exits 0; protected-path diff is empty; `git diff --check` is empty; status contains only the intended sanitizer/mapper/tests plus controller-owned `?? docs/phase1/implementation-status.md`.

- [ ] **Step 13: Write the implementation report and commit the redesign**

Append exact RED/GREEN commands, outputs, changed files, and any concern to:

```text
.superpowers/sdd/2026-07-31-manyselves-phase1-02-fastapi-sse/task-5-sanitizer-redesign-report.md
```

The report is ignored and must not be staged. Then run:

```powershell
git add -- `
  manyselves/webapi/events/sanitizer.py `
  manyselves/webapi/events/mapper.py `
  manyselves/webapi/events/__init__.py `
  tests/webapi/test_event_sanitizer.py `
  tests/webapi/test_event_mapper.py `
  tests/webapi/test_sse.py
git diff --cached --check
git commit -m "fix(webapi): replace event redaction with fail closed sanitizer"
```

Expected: one implementation commit; `docs/phase1/implementation-status.md` and the ignored report are absent from the commit.

- [ ] **Step 14: Run independent scoped review before Task 6**

Review range begins at `7656262` and ends at the redesign implementation commit. The reviewer must receive the design spec, this plan, the implementation report, and the full scoped diff. Approval requires:

- both original P1 reproducers are closed;
- no unknown-object hooks execute;
- generic prose and known metrics retain their specified values;
- all current event, replay, SSE cancellation, and startup-lifecycle contracts remain intact;
- no Critical or Important finding remains.

If approved, update the ledger and `docs/phase1/implementation-status.md` to mark Task 5 complete, then start Plan 02 Task 6. If not approved, keep Task 5 incomplete and adjudicate the finding before any OpenAPI or React implementation.
