# Event Payload Sanitizer Redesign

Date: 2026-08-02

## Purpose

Close the two load-bearing Task 5 security gaps without changing the existing runtime, message models, SSE envelope, replay model, or React plans:

1. `authentication*` fields can bypass fail-closed redaction.
2. `token_usage` containers can expose opaque leaves or invoke untrusted conversion hooks.

After this design is implemented and independently approved, Plan 02 Task 6 exports the locked OpenAPI contract and work proceeds directly to the React workspace.

## Constraints

- Do not modify `manyselves/core/**` or `manyselves/templates/**`.
- Keep `EventEnvelope` schema version 1 and every existing event type unchanged.
- Preserve JSON object/list structure where it is safe to do so.
- Replace an unprovably safe sensitive value with `[REDACTED]`; do not reject or drop the event.
- Never call an unknown object's `__str__`, `__repr__`, or `__iter__` while sanitizing.
- `ConfigChange.old_value` and `new_value` use the same field policy independently.
- Normal prose outside a credential field, including “Bearer authentication”, remains readable.

## Approaches Considered

### 1. Continue patching key-name rules

This is the smallest change, but five review rounds demonstrated that each new exception creates another boundary. It is rejected because the behavior remains distributed across `_sensitive_key`, `_json_safe`, text regexes, and `ConfigChange` overrides.

### 2. Context-driven sanitizer with explicit policies — selected

Extract one web API component that classifies a field once and then traverses its value under a named context: generic, authentication, or token usage. Sensitive contexts use allowlists; unknown values fail closed. This is narrowly scoped, testable independently, and keeps the current public event contract.

### 3. Per-message public payload DTOs

Defining a dedicated public DTO for every concrete message is the strongest long-term contract, but it is substantially larger than the two blockers and would delay React. It remains a possible Phase 2 hardening step, not part of this recovery.

## Architecture

Create `manyselves/webapi/events/sanitizer.py` with one public entry point:

```python
class EventPayloadSanitizer:
    def sanitize_mapping(self, value: dict[object, object]) -> dict[str, Any]: ...
    def sanitize_field(self, name: object, value: object) -> Any: ...
```

`EventMapper` owns one sanitizer instance and uses it for the message dump. `ConfigChange` calls `sanitize_field(config_type, old_value)` and `sanitize_field(config_type, new_value)` independently. The mapper no longer contains its own key-classification or recursive-redaction implementation.

Internally the sanitizer has three contexts:

- `GENERIC`: preserve supported JSON-safe values and scrub recognizable credential text.
- `AUTHENTICATION`: preserve only explicit non-secret metadata; redact every unknown scalar leaf.
- `TOKEN_USAGE`: preserve only exact token metric fields with numeric/`None` values; redact every unknown leaf.

Classification happens before traversal. A field is never first treated as generic and later guessed to be sensitive from its rendered value.

## Field Classification

Keys are classified only when their exact type is `str`. Non-string mapping keys receive deterministic, collision-free redacted placeholder names without invoking conversion hooks.

Classification precedence is fixed: explicit secret meaning first, then a registered context root, then an exact telemetry field, then generic handling. An auth/authentication boundary field containing `header`, `jwt`, `token`, `secret`, `password`, or `credential` is therefore wholly redacted rather than traversed as metadata.

### Always-secret fields

Normalized keys containing established credential meanings—token credentials, authorization/header, JWT, password, secret, credential, cookies, API/private/access/client keys—redact the complete value unless the key is an explicitly registered context root below.

### Authentication context roots

Recognize exact `auth` and `authentication`, plus delimiter- or camel-case-boundary forms such as:

- `auth_config`, `authConfig`, `auth-payload`
- `authentication_config`, `authenticationConfig`, `authentication-header`

Do not match ordinary fields such as `author`, `authority`, `authorized`, or `authorization_status` as context roots. `authorization` remains an always-secret credential field.

The last example is recognized as authentication-related and then wholly redacted because `header` has explicit secret meaning. If a non-secret authentication root contains a supported built-in container, traverse it in `AUTHENTICATION` context. A scalar, custom object, or unsupported container at the root is `[REDACTED]`.

### Token usage context root

Only normalized `tokenusage` is a token-usage context root. `None` is preserved. A built-in `dict`, `list`, or `tuple` is traversed in `TOKEN_USAGE` context. Scalars, sets, frozensets, custom mappings, and custom iterables are `[REDACTED]`.

### Exact token metric fields

The scalar allowlist is derived from actual project data:

- `input_tokens`, `output_tokens`
- `prompt_tokens`, `completion_tokens`
- `total_tokens`, `cached_tokens`, `reasoning_tokens`
- `max_tokens`, `max_total_tokens`, `working_memory_tokens`
- `tokens_in`, `tokens_out`, `token_count`
- `agent_cumulative_input_tokens`, `agent_cumulative_output_tokens`

After normalization, only these exact names accept an exact built-in `int` or `float` other than `bool`, or `None`. Unknown names such as `api_tokens`, `csrf_tokens`, `oauth_tokens`, `opaque_tokens`, `mystery_tokens`, and `mystery_token_count` are sensitive regardless of value type.

## Traversal Rules

### Generic context

- Supported generic values are `None`, exact built-in `str`/`bool`/`int`/`float`/`bytes`, `datetime`, `Enum`, `Path`, Pydantic secret values, Pydantic models, and the exact built-in containers named below. Other values are unsupported.
- Exact built-in `dict`, `list`, `tuple`, `set`, and `frozenset` are traversed without invoking user conversion hooks; sets become deterministic JSON lists after their sanitized values are produced.
- Pydantic secret types are always `[REDACTED]`.
- Unknown/custom objects are `[REDACTED]`; they are never stringified.
- Recognizable credential text in an ordinary string is scrubbed, while ordinary explanatory prose remains readable.

### Authentication context

Mappings preserve keys and container shape. Only these normalized scalar metadata fields may retain supported scalar values:

- `method`, `scheme`, `type`, `provider`, `description`, `configured`, `enabled`

All other scalar leaves—including `value`, endpoint/header/JWT/token/secret/credential values and unlabeled list entries—become `[REDACTED]`. Nested exact built-in containers recurse under the same authentication context. Unknown/custom objects are redacted without hook invocation.

### Token usage context

Mappings preserve shape. Exact metric keys retain only numeric/`None` values. Nested `token_usage` built-in containers recurse under the same context. Unknown keys, strings, booleans, custom objects, sets, and frozensets are redacted. Lists and tuples may preserve a list of nested built-in mappings/lists/tuples, but unlabeled scalar entries are redacted.

### Safety guards

- Track active container identities; a cycle becomes `[REDACTED]` instead of recursing forever.
- Enforce a maximum traversal depth of 32; values beyond the bound become `[REDACTED]`.
- Sanitization of an individual unsupported value must not raise and must not stop broker publication.

## Data Flow

```text
internal Message
    -> model_dump
    -> EventPayloadSanitizer.sanitize_mapping
       -> classify field
       -> choose GENERIC / AUTHENTICATION / TOKEN_USAGE
       -> bounded fail-closed traversal
    -> EventEnvelope v1
    -> replay buffer
    -> SSE clients
```

No unsanitized payload is inserted into replay or passed to the broker.

## Error Handling

- Unsupported message classes continue to raise `UnsupportedMessageTypeError` before publication.
- Unsupported payload values do not raise; they become `[REDACTED]`.
- Programmer errors in the sanitizer are not silently swallowed by the broker. Tests must prove all documented input categories have deterministic results.
- The sanitizer does not log original secret values.

## Testing

TDD begins with the two independent-review reproducers and expands only to their policy boundaries.

1. Authentication root matrix: exact, delimiter, camel case, scalar, container, `authentication*`, and non-matches `author/authority/authorized`.
2. Authentication recursion: metadata retained; unknown scalar leaves and nested secrets redacted; list/tuple/set/frozenset/custom objects cannot leak or execute hooks.
3. Token usage recursion: exact metrics preserve numeric/`None`; unknown keys and opaque list entries redact; custom objects and unsupported containers execute no hooks.
4. `ConfigChange`: old/new independently apply the same rules.
5. Generic regression: 23 message mappings, UTC normalization, ordinary prose, recognized credential text, and current payload types.
6. End-to-end SSE/replay assertion: secret sent through a message is absent from both serialized SSE data and replayed events.
7. Existing Task 5 mapper/SSE tests, complete web API suite, combined application/web API suite, Ruff, core freeze, protected-path diff, and `git diff --check` remain required.

## Completion Gate

The redesign is complete only when:

- both P1 reproducers fail on the pre-redesign commit and pass on the implementation;
- the implementation report contains fresh post-change commands and outputs;
- an independent reviewer approves the scoped redesign diff with no open Critical or Important finding;
- Task 5 is then marked complete before Task 6 begins.
