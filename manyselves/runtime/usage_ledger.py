"""Concurrency-safe per-provider-attempt usage ledger.

The ledger is deliberately a telemetry boundary rather than a pricing engine.
It records enough information to explain why a logical round happened and how
much context was sent, while leaving monetary conversion to a separately
versioned price table.  The JSONL format is append-only and remains readable
for ledgers written before the round/attempt fields were introduced.
"""

from __future__ import annotations

import fcntl
import json
import threading
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any


class RoundReason(StrEnum):
    """Why a provider round was dispatched.

    Lower-case member names intentionally mirror the persisted JSON values and
    the names used by runtime summaries.  A ``provider_retry`` is valid only
    for a physical attempt numbered two or greater; the ledger never derives
    that reason from a status or disposition field.
    """

    direct_submit = "direct_submit"
    evidence_lookup = "evidence_lookup"
    long_output_continuation = "long_output_continuation"
    semantic_correction = "semantic_correction"
    provider_retry = "provider_retry"
    redundant_followup = "redundant_followup"
    tool_contract_error = "tool_contract_error"


_ROUND_REASON_VALUES = frozenset(reason.value for reason in RoundReason)
_ATTEMPT_KINDS = frozenset(
    {"provider_request", "pre_send_rebuild", "pre_send_block"}
)
_PRE_SEND_KINDS = frozenset({"pre_send_rebuild", "pre_send_block"})


def _integer(row: dict[str, Any], key: str) -> int:
    try:
        return int(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _nonnegative_integer(row: dict[str, Any], key: str) -> int:
    return max(0, _integer(row, key))


def _as_bool(value: Any) -> bool:
    """Parse JSONL booleans without treating ``"false"`` as true."""

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().casefold()
        if normalized in {"", "0", "false", "no", "off", "none", "null"}:
            return False
        if normalized in {"1", "true", "yes", "on"}:
            return True
    return bool(value)


def _value_string(value: Any) -> str:
    """Return an enum-compatible string while preserving ordinary values."""

    value = getattr(value, "value", value)
    return str(value)


def _uncached_input_tokens(row: dict[str, Any]) -> int:
    """Read the explicit field or derive it for pre-field ledger rows."""

    if "uncached_input_tokens" in row:
        return max(0, _integer(row, "uncached_input_tokens"))
    return max(
        0,
        _integer(row, "input_tokens")
        - _integer(row, "cached_input_tokens")
        - _integer(row, "cache_write_input_tokens"),
    )


_TOKEN_FIELDS = frozenset(
    {
        "input_tokens",
        "cached_input_tokens",
        "cache_write_input_tokens",
        "uncached_input_tokens",
        "output_tokens",
        "total_tokens",
    }
)

_CHAR_FIELDS = (
    "new_chars",
    "repeated_chars",
    "repeated_stable_chars",
    "repeated_dynamic_chars",
    "duplicate_tool_result_chars",
    "duplicate_completed_result_chars",
    "duplicate_evidence_chars",
)

# A few callers used a more descriptive spelling while the schema was being
# developed.  Reading these aliases costs nothing and keeps JSONL summaries
# forward/backward compatible without making them part of the canonical row.
_CHAR_ALIASES: dict[str, tuple[str, ...]] = {
    "new_chars": ("new_context_chars",),
    "repeated_chars": ("repeated_context_chars",),
    "repeated_stable_chars": ("stable_chars", "stable_context_chars"),
    "repeated_dynamic_chars": ("dynamic_chars", "dynamic_context_chars"),
    "duplicate_tool_result_chars": (
        "duplicate_tool_chars",
        "duplicate_tool_call_chars",
    ),
    "duplicate_completed_result_chars": ("duplicate_completed_chars",),
    "duplicate_evidence_chars": ("duplicate_evidence_context_chars",),
}

_NEW_SCHEMA_MARKERS = frozenset(
    {
        "round_reason",
        "reason_source",
        "logical_round_id",
        "parent_provider_call_id",
        "attempt_kind",
        "provider_request_sent",
        "context_manifest_version",
        "new_chars",
        "repeated_chars",
        "repeated_stable_chars",
        "repeated_dynamic_chars",
        "duplicate_tool_result_chars",
        "duplicate_completed_result_chars",
        "duplicate_evidence_chars",
        "pre_send_guard_status",
        "rebuild_count",
        # Transitional spellings are markers too, so reading one does not
        # mislabel an otherwise new row as a legacy record.
        "stable_chars",
        "dynamic_chars",
        "duplicate_tool_chars",
        "duplicate_tool_call_chars",
        "duplicate_completed_chars",
    }
)


def _is_legacy_row(row: dict[str, Any]) -> bool:
    return not any(key in row for key in _NEW_SCHEMA_MARKERS)


def _provider_request_sent(row: dict[str, Any]) -> bool:
    """Read the physical-send bit, treating pre-schema rows as sent.

    Older rows represented only provider attempts and therefore have no
    ``provider_request_sent`` key.  Treating those rows as sent preserves the
    historical counters; newly written rows always carry an explicit bool.
    """

    if "provider_request_sent" not in row:
        return True
    return _as_bool(row.get("provider_request_sent"))


def _field_value(row: dict[str, Any], field: str) -> int:
    if field == "uncached_input_tokens":
        return _uncached_input_tokens(row)
    if field in row:
        return _nonnegative_integer(row, field)
    for alias in _CHAR_ALIASES.get(field, ()):
        if alias in row:
            return _nonnegative_integer(row, alias)
    return 0


class UsageLedger:
    _locks: dict[Path, threading.Lock] = {}
    _locks_guard = threading.Lock()

    def __init__(self, workspace: Path, run_id: str):
        safe_run = "".join(c if c.isalnum() or c in "-_." else "_" for c in run_id)
        self.path = Path(workspace).resolve() / ".manyselves" / "usage" / f"{safe_run}.jsonl"

    @classmethod
    def _lock_for(cls, path: Path) -> threading.Lock:
        with cls._locks_guard:
            return cls._locks.setdefault(path, threading.Lock())

    @staticmethod
    def _normalize_round_reason(value: Any) -> str | None:
        if value is None:
            return None
        normalized = _value_string(value).strip()
        if not normalized:
            return None
        if normalized not in _ROUND_REASON_VALUES:
            raise ValueError(
                "round_reason must be one of "
                + ", ".join(sorted(_ROUND_REASON_VALUES))
            )
        return normalized

    @staticmethod
    def _normalize_attempt_kind(value: Any) -> str:
        normalized = _value_string(value).strip()
        if normalized not in _ATTEMPT_KINDS:
            raise ValueError(
                "attempt_kind must be one of " + ", ".join(sorted(_ATTEMPT_KINDS))
            )
        return normalized

    def record_attempt(self, **record: Any) -> dict[str, Any]:
        """Append one usage/guard record and return its normalized row.

        ``record_attempt`` historically meant a real provider attempt.  Calls
        that omit the new send bit therefore default to ``provider_request_sent``
        true, while explicit pre-send guard records can set it false.  No retry
        reason is inferred from ``retry``, status, or accepted/unknown
        disposition: callers must provide ``round_reason`` explicitly.
        """

        row = {"timestamp": datetime.now().astimezone().isoformat(), **record}

        # ``reason`` was used by an early draft of the schema; accept it as an
        # input alias but persist only the canonical ``round_reason`` key.
        if "reason" in row:
            alias_reason = row.pop("reason")
            if "round_reason" in row and row.get("round_reason") not in {
                None,
                alias_reason,
            }:
                raise ValueError("reason and round_reason disagree")
            row.setdefault("round_reason", alias_reason)

        raw_kind = row.get("attempt_kind")
        attempt_kind = (
            self._normalize_attempt_kind(raw_kind) if raw_kind is not None else None
        )

        if "provider_request_sent" in row:
            provider_request_sent = _as_bool(row.get("provider_request_sent"))
        else:
            # ``not_sent`` is the one disposition that proves no provider
            # request left this process.  A pre-send kind carries the same
            # evidence.  Every other legacy call remains a sent attempt.
            disposition = _value_string(row.get("attempt_disposition", "")).strip().casefold()
            provider_request_sent = not (
                disposition == "not_sent" or attempt_kind in _PRE_SEND_KINDS
            )
        row["provider_request_sent"] = provider_request_sent

        if attempt_kind is None:
            attempt_kind = (
                "provider_request"
                if provider_request_sent
                else (
                    "pre_send_rebuild"
                    if _nonnegative_integer(row, "rebuild_count") > 0
                    else "pre_send_block"
                )
            )
        row["attempt_kind"] = attempt_kind

        round_reason = self._normalize_round_reason(row.get("round_reason"))
        row["round_reason"] = round_reason

        if round_reason == RoundReason.provider_retry.value and _integer(row, "attempt") < 2:
            raise ValueError("round_reason=provider_retry requires attempt >= 2")

        if row.get("reason_source") is None:
            row["reason_source"] = "explicit" if round_reason is not None else "unclassified"
        else:
            row["reason_source"] = _value_string(row["reason_source"])

        # Context and guard fields are intentionally explicit in new rows.  A
        # missing reason remains unclassified rather than being guessed from a
        # status or turn kind.
        row.setdefault("logical_round_id", None)
        row.setdefault("parent_provider_call_id", None)
        row.setdefault("context_manifest_version", None)
        for field in _CHAR_FIELDS:
            if field not in row:
                aliases = _CHAR_ALIASES.get(field, ())
                row[field] = next(
                    (_nonnegative_integer(row, alias) for alias in aliases if alias in row),
                    0,
                )
            else:
                row[field] = _nonnegative_integer(row, field)
        row.setdefault("pre_send_guard_status", None)
        row["rebuild_count"] = _nonnegative_integer(row, "rebuild_count")

        row.setdefault("uncached_input_tokens", _uncached_input_tokens(row))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(row, ensure_ascii=False, default=str) + "\n"
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._lock_for(self.path):
            with lock_path.open("a+", encoding="utf-8") as process_lock:
                fcntl.flock(process_lock.fileno(), fcntl.LOCK_EX)
                try:
                    with self.path.open("a", encoding="utf-8") as handle:
                        handle.write(line)
                        handle.flush()
                finally:
                    fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)
        return row

    def rows(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with self._lock_for(self.path):
            with lock_path.open("a+", encoding="utf-8") as process_lock:
                fcntl.flock(process_lock.fileno(), fcntl.LOCK_SH)
                try:
                    rows: list[dict[str, Any]] = []
                    for line in self.path.read_text(encoding="utf-8").splitlines():
                        if not line.strip():
                            continue
                        row = json.loads(line)
                        if not isinstance(row, dict):
                            raise ValueError("usage ledger rows must be JSON objects")
                        # Do not rewrite legacy JSONL or add synthetic round
                        # reasons.  The marker is in-memory and the summary
                        # treats it as an explicit compatibility bucket.
                        if _is_legacy_row(row):
                            row = {"reason_source": "legacy_unclassified", **row}
                        rows.append(row)
                    return rows
                finally:
                    fcntl.flock(process_lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _group_name(row: dict[str, Any], group_by: str) -> str:
        value = row.get(group_by)
        if group_by == "round_reason":
            if value is not None and str(value).strip():
                return _value_string(value)
            if row.get("reason_source") == "legacy_unclassified":
                return "legacy_unclassified"
            return "unclassified"
        if group_by == "attempt_kind" and (value is None or not str(value).strip()):
            if row.get("reason_source") == "legacy_unclassified":
                return "legacy_unclassified"
            return "unknown"
        return str(value or "unknown")

    def summarize(self, *, group_by: str = "stage") -> dict[str, Any]:
        """Aggregate provider cost signals without inventing provider prices.

        ``provider_attempts`` and all token counters are based exclusively on
        rows whose ``provider_request_sent`` is true.  Character/latency fields
        are retained for pre-send rebuild/block telemetry because those rows
        still explain local context work even though they have no provider
        charge.
        """

        allowed_groupings = {
            "stage",
            "task_id",
            "agent_id",
            "model",
            "phase",
            "round_reason",
            "attempt_kind",
        }
        if group_by not in allowed_groupings:
            raise ValueError(
                "group_by must be one of stage, task_id, agent_id, model, phase, "
                "round_reason, or attempt_kind"
            )
        rows = self.rows()
        fields = (
            "input_tokens",
            "cached_input_tokens",
            "cache_write_input_tokens",
            "uncached_input_tokens",
            "output_tokens",
            "total_tokens",
            "request_chars",
            "message_chars",
            "tool_schema_chars",
            "duration_ms",
            "queue_wait_ms",
            "context_build_ms",
            "serialization_ms",
            "ttft_ms",
            "provider_active_ms",
            "tool_time_ms",
            "response_tool_call_count",
            *_CHAR_FIELDS,
        )

        def empty_bucket() -> dict[str, Any]:
            return {
                "provider_attempts": 0,
                "successful_attempts": 0,
                "failed_attempts": 0,
                "duplicate_request_attempts": 0,
                "repeated_request_chars": 0,
                "repeated_message_chars": 0,
                "repeated_tool_schema_chars": 0,
                **{field: 0 for field in fields},
            }

        totals = empty_bucket()
        groups: dict[str, dict[str, Any]] = {}
        request_counts: dict[str, int] = {}
        message_counts: dict[str, int] = {}
        tool_schema_counts: dict[str, int] = {}
        repeated_request_chars = 0
        repeated_message_chars = 0
        repeated_tool_schema_chars = 0

        for row in rows:
            group_name = self._group_name(row, group_by)
            bucket = groups.setdefault(group_name, empty_bucket())
            sent = _provider_request_sent(row)

            for target in (totals, bucket):
                if sent:
                    target["provider_attempts"] += 1
                    if str(row.get("status") or "").casefold() == "success":
                        target["successful_attempts"] += 1
                    else:
                        target["failed_attempts"] += 1
                for field in fields:
                    # A pre-send guard/rebuild can have local character and
                    # latency telemetry, but it cannot have provider tokens.
                    if field in _TOKEN_FIELDS and not sent:
                        continue
                    target[field] += _field_value(row, field)

            # Duplicate fingerprints represent repeated provider payloads, so
            # guard/rebuild rows that never left the process are excluded.
            if sent:
                request_key = str(row.get("request_fingerprint") or "")
                if request_key:
                    request_counts[request_key] = request_counts.get(request_key, 0) + 1
                    if request_counts[request_key] > 1:
                        repeated_chars = _integer(row, "message_chars") + _integer(
                            row, "tool_schema_chars"
                        )
                        repeated_request_chars += max(0, repeated_chars)
                        bucket["duplicate_request_attempts"] += 1
                        bucket["repeated_request_chars"] += max(0, repeated_chars)

                message_key = str(row.get("message_fingerprint") or "")
                if message_key:
                    message_counts[message_key] = message_counts.get(message_key, 0) + 1
                    if message_counts[message_key] > 1:
                        repeated_chars = _integer(row, "message_chars")
                        repeated_message_chars += max(0, repeated_chars)
                        bucket["repeated_message_chars"] += max(0, repeated_chars)

                tool_key = str(row.get("tool_schema_fingerprint") or "")
                if tool_key:
                    tool_schema_counts[tool_key] = tool_schema_counts.get(tool_key, 0) + 1
                    if tool_schema_counts[tool_key] > 1:
                        repeated_chars = _integer(row, "tool_schema_chars")
                        repeated_tool_schema_chars += max(0, repeated_chars)
                        bucket["repeated_tool_schema_chars"] += max(0, repeated_chars)

        totals.update(
            {
                "duplicate_request_attempts": sum(
                    max(0, count - 1) for count in request_counts.values()
                ),
                "repeated_request_chars": repeated_request_chars,
                "repeated_message_chars": repeated_message_chars,
                "repeated_tool_schema_chars": repeated_tool_schema_chars,
                "pricing_status": "unconfigured",
                "pricing_table_version": None,
                "pricing_currency": None,
                "estimated_cost": None,
            }
        )
        return {
            "group_by": group_by,
            "totals": totals,
            "groups": dict(sorted(groups.items())),
        }
