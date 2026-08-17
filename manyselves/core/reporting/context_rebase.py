"""Reporting-specific typed context reconstruction.

``ReportingContextRebuilder`` deliberately does not treat the conversation
trace as a prompt.  It maintains a small v3 manifest and reconstructs a
Provider request in this order: stable prefix, typed task/capsule, bounded
evidence and knowledge, complete unconsumed tool units, partial tail, and a
minimal tool schema.  Consumed prose and tool results are represented only by
their immutable reference/hash.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
from collections import OrderedDict
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..loops.context_rebase import (
    AtomicToolUnit,
    RebasedContext,
    atomic_tool_units,
)
from ..providers.base import Message as LLMMessage
from ..providers.base import LLMToolCall
from .context_state import (
    ContextManifest,
    EvidenceSlice,
    KnowledgeSlice,
    ResultPartRef,
    RunEvidenceIndex,
    TaskStateCapsule,
    TaskStateStore,
    ToolResultMemo,
    ToolResultMemoStore,
    canonical_sha256,
)


def _string(value: Any) -> str:
    return str(value or "")


def _bounded_summary(value: str | None, *, max_chars: int = 768) -> str | None:
    """Return a small deterministic reminder for already delivered context.

    Provider APIs are stateless, so a ref/hash alone is too weak for a later
    reasoning round.  Retaining the whole evidence slice, however, recreates
    the repeated-context problem this module is meant to solve.  The summary is
    therefore a bounded exact excerpt (whitespace-normalized, never model
    generated) that keeps orientation while the immutable artifact remains the
    authority for any detail that must be reopened.
    """

    text = re.sub(r"\s+", " ", _string(value)).strip()
    if not text:
        return None
    return text[: max(1, int(max_chars))]


def _dump(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dump(item) for item in value]
    return value


def _message_dict(message: Any) -> dict[str, Any]:
    payload = {
        "role": _string(getattr(message, "role", "user")),
        "content": _string(getattr(message, "content", "")),
        "tool_call_id": getattr(message, "tool_call_id", None),
        "is_tool_result": bool(getattr(message, "is_tool_result", False)),
        "thinking": getattr(message, "thinking", None),
        "cache_control": bool(getattr(message, "cache_control", False)),
    }
    calls = []
    for call in getattr(message, "tool_calls", None) or ():
        calls.append(
            {
                "id": _string(getattr(call, "id", "")),
                "name": _string(getattr(call, "name", "")),
                "arguments": _dump(getattr(call, "arguments", {}) or {}),
            }
        )
    if calls:
        payload["tool_calls"] = calls
    return payload


def _message_from_dict(value: Mapping[str, Any]) -> LLMMessage:
    calls = [
        LLMToolCall(
            id=_string(item.get("id")),
            name=_string(item.get("name")),
            arguments=dict(item.get("arguments") or {}),
        )
        for item in (value.get("tool_calls") or ())
        if isinstance(item, Mapping)
    ]
    return LLMMessage(
        role=_string(value.get("role") or "user"),
        content=_string(value.get("content")),
        tool_calls=calls or None,
        tool_call_id=(
            _string(value.get("tool_call_id"))
            if value.get("tool_call_id") is not None
            else None
        ),
        is_tool_result=bool(value.get("is_tool_result", False)),
        thinking=(
            _string(value.get("thinking")) if value.get("thinking") is not None else None
        ),
        cache_control=bool(value.get("cache_control", False)),
    )


def _call(value: Any, *, fallback_id: str | None = None) -> LLMToolCall:
    if isinstance(value, LLMToolCall):
        return value
    if isinstance(value, Mapping):
        return LLMToolCall(
            id=_string(value.get("id") or value.get("call_id") or fallback_id),
            name=_string(value.get("name") or value.get("tool_name")),
            arguments=dict(value.get("arguments") or value.get("args") or {}),
        )
    return LLMToolCall(
        id=_string(getattr(value, "id", None) or fallback_id),
        name=_string(getattr(value, "name", None) or getattr(value, "tool_name", None)),
        arguments=dict(getattr(value, "arguments", None) or getattr(value, "args", None) or {}),
    )


def _result_message(
    call_id: str,
    result: Any,
    *,
    role: str = "tool",
    consumed: bool = False,
) -> LLMMessage:
    if isinstance(result, LLMMessage):
        return result
    if isinstance(result, Mapping):
        content = json.dumps(result, ensure_ascii=False, sort_keys=True, default=str)
    elif isinstance(result, (list, tuple)):
        content = json.dumps(result, ensure_ascii=False, default=str)
    else:
        content = _string(result)
    return LLMMessage(
        role=role,
        content=content,
        tool_call_id=call_id,
        is_tool_result=True,
    )


class ReportingContextRebuilder:
    """Maintain and rebuild one run/task/revision context manifest."""

    def __init__(
        self,
        workspace: Path | str | None = None,
        *,
        store: TaskStateStore | None = None,
        memo_store: ToolResultMemoStore | None = None,
        max_evidence_chars: int = 12_000,
        max_knowledge_chars: int = 12_000,
        max_tool_units: int = 8,
        max_partial_chars: int = 8_000,
        persist: bool | None = None,
    ) -> None:
        self.workspace = Path(workspace).resolve() if workspace is not None else None
        self.store = store
        self.memo_store = memo_store
        self.max_evidence_chars = max(1, int(max_evidence_chars))
        self.max_knowledge_chars = max(1, int(max_knowledge_chars))
        self.max_tool_units = max(1, int(max_tool_units))
        self.max_partial_chars = max(1, int(max_partial_chars))
        self.persist = bool(store) if persist is None else bool(persist)
        self.manifest: ContextManifest | None = None
        self._calls: OrderedDict[str, LLMToolCall] = OrderedDict()
        self._results: OrderedDict[str, LLMMessage] = OrderedDict()
        self._consumed_calls: set[str] = set()
        self._call_batches: list[list[str]] = []
        self._active_batch: list[str] | None = None
        self._partial_tail: list[dict[str, Any]] = []

    # ------------------------------------------------------------------
    # Typed state lifecycle
    # ------------------------------------------------------------------
    def begin_task(
        self,
        run_id: Any = None,
        task_id: str | None = None,
        revision: int = 0,
        *,
        envelope: Any | None = None,
        task_attempt_id: str | None = None,
        objective: str = "",
        stable_prefix: Sequence[Any] | None = None,
        input_refs: Sequence[str] = (),
        evidence: Sequence[EvidenceSlice | Mapping[str, Any]] = (),
        knowledge: Sequence[KnowledgeSlice | Mapping[str, Any]] = (),
        required_tool_names: Sequence[str] = (),
        allowed_tool_names: Sequence[str] = (),
        capsule: TaskStateCapsule | None = None,
        task_capsule: TaskStateCapsule | None = None,
        persist: bool | None = None,
    ) -> ContextManifest:
        """Start a fresh task, accepting either ids or a TaskEnvelope-like object."""

        if envelope is not None:
            run_id = envelope
        envelope = None
        if not isinstance(run_id, str) and task_id is None:
            envelope = run_id
            run_id = _string(getattr(envelope, "run_id", ""))
            task_id = _string(getattr(envelope, "task_id", ""))
            revision = int(getattr(envelope, "revision", revision) or revision)
            task_attempt_id = task_attempt_id or getattr(envelope, "task_attempt_id", None)
            objective = objective or _string(getattr(envelope, "objective", ""))
            input_refs = input_refs or tuple(getattr(envelope, "input_refs", ()) or ())
            required_tool_names = required_tool_names or tuple(
                getattr(envelope, "required_tool_names", ()) or ()
            )
            allowed_tool_names = allowed_tool_names or tuple(
                getattr(envelope, "allowed_tools", ()) or ()
            )
        run_id = _string(run_id)
        task_id = _string(task_id)
        if not run_id or not task_id:
            raise ValueError("begin_task requires run_id and task_id")
        capsule = capsule or task_capsule or TaskStateCapsule(
            run_id=run_id,
            task_id=task_id,
            revision=revision,
            task_attempt_id=task_attempt_id,
            objective=objective,
            input_refs=list(dict.fromkeys(_string(item) for item in input_refs if _string(item))),
            required_tool_names=list(dict.fromkeys(_string(item) for item in required_tool_names if _string(item))),
            allowed_tool_names=list(dict.fromkeys(_string(item) for item in allowed_tool_names if _string(item))),
        )
        if capsule.run_id != run_id or capsule.task_id != task_id or capsule.revision != revision:
            raise ValueError("typed task capsule identity mismatch")
        prefix = [_message_dict(item) if not isinstance(item, Mapping) else dict(item) for item in (stable_prefix or ())]
        typed_evidence = [
            item if isinstance(item, EvidenceSlice) else EvidenceSlice.model_validate(item)
            for item in evidence
        ]
        typed_knowledge = [
            item if isinstance(item, KnowledgeSlice) else KnowledgeSlice.model_validate(item)
            for item in knowledge
        ]
        index = RunEvidenceIndex(
            run_id=run_id,
            task_id=task_id,
            revision=revision,
            evidence=typed_evidence,
            knowledge=typed_knowledge,
        )
        self.manifest = ContextManifest(
            run_id=run_id,
            task_id=task_id,
            revision=revision,
            task_attempt_id=task_attempt_id,
            stable_prefix=prefix,
            task_state_capsule=capsule,
            evidence_index=index,
            evidence=typed_evidence,
            knowledge=typed_knowledge,
        )
        self._calls.clear()
        self._results.clear()
        self._consumed_calls.clear()
        self._call_batches.clear()
        self._active_batch = None
        self._partial_tail = []
        self._maybe_save(persist=persist)
        return self.manifest

    def _require_manifest(self) -> ContextManifest:
        if self.manifest is None:
            raise RuntimeError("begin_task must be called before recording context state")
        return self.manifest

    def _maybe_save(self, *, persist: bool | None = None) -> None:
        if not (self.persist if persist is None else persist) or self.store is None or self.manifest is None:
            return
        self.store.save(self.manifest)

    def _update_manifest(self, **changes: Any) -> ContextManifest:
        current = self._require_manifest()
        if "task_state_capsule" in changes and changes["task_state_capsule"] is not None:
            capsule = changes["task_state_capsule"]
            changes["task_state_capsule"] = capsule.model_copy(update={"capsule_sha256": None}).with_hash()
        if "evidence_index" in changes and changes["evidence_index"] is not None:
            index = changes["evidence_index"]
            changes["evidence_index"] = index.model_copy(update={"index_sha256": None}).with_hash()
        updated = current.model_copy(update={**changes, "manifest_sha256": None})
        self.manifest = updated.model_copy(update={"manifest_sha256": updated.computed_sha256})
        self._maybe_save()
        return self.manifest

    # ------------------------------------------------------------------
    # Recording typed events
    # ------------------------------------------------------------------
    def record_tool_call(
        self,
        call: Any,
        *,
        assistant_message: Any | None = None,
        consumed: bool = False,
    ) -> AtomicToolUnit | LLMToolCall:
        # An assistant message may contain several calls; preserve all of
        # them in one batch so their matching results stay one atomic unit.
        if getattr(call, "role", None) == "assistant" and getattr(call, "tool_calls", None):
            parsed_calls = [_call(item) for item in (call.tool_calls or ())]
            self._active_batch = None
            for parsed_item in parsed_calls:
                self.record_tool_call(parsed_item, consumed=consumed)
            return parsed_calls[0] if len(parsed_calls) == 1 else parsed_calls  # type: ignore[return-value]
        if isinstance(call, Mapping) and call.get("role") == "assistant" and call.get("tool_calls"):
            parsed_calls = [_call(item) for item in (call.get("tool_calls") or ())]
            self._active_batch = None
            for parsed_item in parsed_calls:
                self.record_tool_call(parsed_item, consumed=consumed)
            return parsed_calls[0] if len(parsed_calls) == 1 else parsed_calls  # type: ignore[return-value]
        parsed = _call(call)
        if not parsed.id or not parsed.name:
            raise ValueError("tool call requires id and name")
        # Calls emitted by one assistant message are one atomic batch.  A new
        # call after a completed result starts the next batch; callers that
        # provide ``assistant_message`` can explicitly group a fresh message.
        if assistant_message is not None or self._active_batch is None or any(
            call_id in self._results or call_id in self._consumed_calls
            for call_id in self._active_batch
        ):
            self._active_batch = []
            self._call_batches.append(self._active_batch)
        if parsed.id not in self._active_batch:
            self._active_batch.append(parsed.id)
        self._calls[parsed.id] = parsed
        if consumed:
            self._consumed_calls.add(parsed.id)
        # Keep the current manifest's allowed/required tool hints in sync.
        manifest = self._require_manifest()
        capsule = manifest.task_state
        if capsule is not None and parsed.name not in capsule.required_tool_names:
            capsule = capsule.model_copy(
                update={"required_tool_names": [*capsule.required_tool_names, parsed.name]}
            )
            self._update_manifest(task_state_capsule=capsule, task=None, capsule=None)
        self._maybe_save()
        return parsed

    def record_tool_result(
        self,
        call_id: Any,
        result: Any = None,
        *,
        tool_name: str | None = None,
        ref: str | None = None,
        sha256: str | None = None,
        result_ref: str | None = None,
        result_hash: str | None = None,
        result_chars: int | None = None,
        consumed: bool = False,
        is_consumed: bool | None = None,
        summary: str | None = None,
        status: str = "completed",
        role: str = "tool",
    ) -> ToolResultMemo | LLMMessage:
        if isinstance(call_id, LLMToolCall) or (
            isinstance(call_id, Mapping) and (call_id.get("id") or call_id.get("call_id"))
        ):
            call = _call(call_id)
            if call.id not in self._calls:
                self.record_tool_call(call)
            call_id = call.id
        elif not isinstance(call_id, (str, bytes)) and getattr(call_id, "id", None):
            call = _call(call_id)
            if call.id not in self._calls:
                self.record_tool_call(call)
            call_id = call.id
        if isinstance(call_id, LLMMessage):
            message = call_id
            call_id = message.tool_call_id
            result = message
        call_id = _string(call_id)
        ref = ref or result_ref
        sha256 = sha256 or result_hash
        if is_consumed is not None:
            consumed = is_consumed
        if not call_id:
            raise ValueError("tool result requires a call id")
        call = self._calls.get(call_id)
        tool_name = tool_name or (call.name if call is not None else "unknown_tool")
        message = _result_message(call_id, result, role=role, consumed=consumed)
        content = _string(message.content)
        digest = sha256 or hashlib.sha256(content.encode("utf-8")).hexdigest()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("tool result sha256 must be a lowercase SHA-256 digest")
        reference = ref or f"memory://tool-result/{call_id}/{digest[:16]}"
        if consumed:
            self._consumed_calls.add(call_id)
            memo = ToolResultMemo(
                call_id=call_id,
                tool_name=tool_name,
                ref=reference,
                sha256=digest,
                chars=(result_chars if result_chars is not None else len(content)),
                consumed=True,
                status=status,
                summary=summary,
                run_id=self.manifest.run_id if self.manifest else None,
                task_id=self.manifest.task_id if self.manifest else None,
                revision=self.manifest.revision if self.manifest else None,
            )
            if self.memo_store is not None and self.persist:
                self.memo_store.save(memo)
            manifest = self._require_manifest()
            memos = [*manifest.tool_results, memo]
            index = manifest.evidence_index
            if index is not None:
                index = index.model_copy(update={"tool_results": memos})
            self._update_manifest(tool_results=memos, evidence_index=index)
            return memo
        self._results[call_id] = message
        return message

    def record_result_part(
        self,
        part: Any,
        ref: str | None = None,
        sha256: str | None = None,
        *,
        characters: int | None = None,
        result_ref: str | None = None,
        content_hash: str | None = None,
        ready: bool = True,
        consumed: bool = True,
        content: str | None = None,
        revision: int | None = None,
    ) -> ResultPartRef:
        if isinstance(part, ResultPartRef):
            item = part
        elif isinstance(part, Mapping):
            payload = dict(part)
            if ref is not None or result_ref is not None:
                payload.setdefault("ref", ref or result_ref)
            if sha256 is not None or content_hash is not None:
                payload.setdefault("sha256", sha256 or content_hash)
            item = ResultPartRef.model_validate(payload)
        else:
            part_id = _string(getattr(part, "part_id", None) or getattr(part, "id", None) or part)
            content = content if content is not None else getattr(part, "content", None)
            digest = sha256 or content_hash or hashlib.sha256(_string(content).encode("utf-8")).hexdigest()
            item = ResultPartRef(
                part_id=part_id,
                ref=ref or result_ref or f"memory://result-part/{part_id}/{digest[:16]}",
                sha256=digest,
                chars=(characters if characters is not None else len(_string(content))),
                ready=ready,
                consumed=consumed,
                revision=(revision if revision is not None else (self.manifest.revision if self.manifest else 0)),
            )
        manifest = self._require_manifest()
        # Keep one latest reference per part id without mutating earlier state.
        part_id = item.part_id
        existing = [item for item in manifest.result_parts if item.part_id != part_id]
        result_parts = [*existing, item]
        index = manifest.evidence_index
        if index is not None:
            index = index.model_copy(update={"result_parts": result_parts})
        capsule = manifest.task_state
        if capsule is not None and item.ready and item.part_id not in capsule.completed_result_parts:
            capsule = capsule.model_copy(
                update={"completed_result_parts": [*capsule.completed_result_parts, item.part_id]}
            )
        self._update_manifest(
            result_parts=result_parts,
            evidence_index=index,
            task_state_capsule=capsule,
            task=None,
            capsule=None,
        )
        return item

    def mark_provider_context_delivered(
        self,
        messages: Sequence[Any] | None = None,
        *,
        response: Any | None = None,
    ) -> ContextManifest:
        """Commit the exact context a successful Provider round consumed.

        The first request for a typed task receives bounded evidence/knowledge
        content.  Once the Provider has successfully returned, later requests
        retain only a deterministic short reminder plus ref/hash.  Complete
        tool-call/result units present in that successful request are likewise
        converted to hash-only memos, so only newly produced tool units remain
        Provider-visible on the next round.

        This method must be called *after* a successful physical request.  A
        definitely-rejected retry therefore keeps the original full payload.
        """

        manifest = self._require_manifest()
        delivered_result_ids = {
            _string(getattr(message, "tool_call_id", ""))
            for message in (messages or ())
            if bool(getattr(message, "is_tool_result", False))
            and _string(getattr(message, "tool_call_id", ""))
        }
        # A first response that only asks for a tool has not yet converted the
        # base evidence into a durable conclusion.  Keep the full bounded slice
        # for the immediate tool-result follow-up.  Once a response consumes a
        # result (or returns without requesting a tool), summary/ref mode is
        # safe.  This deliberately trades at most one repeat for correctness.
        defer_base_consumption = bool(
            getattr(response, "tool_calls", None)
        ) and not delivered_result_ids

        def consume_slice(item: EvidenceSlice | KnowledgeSlice):
            if item.consumed and item.content is None:
                return item
            return item.model_copy(
                update={
                    "summary": item.summary or _bounded_summary(item.content),
                    "content": None,
                    "consumed": True,
                }
            )

        evidence = [
            item if defer_base_consumption else consume_slice(item)
            for item in manifest.evidence
        ]
        knowledge = [
            item if defer_base_consumption else consume_slice(item)
            for item in manifest.knowledge
        ]
        index = manifest.evidence_index
        if index is not None:
            index = index.model_copy(
                update={
                    "items": [
                        item if defer_base_consumption else consume_slice(item)
                        for item in index.items
                    ],
                    "knowledge": [
                        item if defer_base_consumption else consume_slice(item)
                        for item in index.knowledge
                    ],
                    "index_sha256": None,
                }
            )

        memos_by_call = {item.call_id: item for item in manifest.tool_results}
        for call_id in sorted(delivered_result_ids):
            message = self._results.get(call_id)
            if message is None:
                self._consumed_calls.add(call_id)
                continue
            content = _string(message.content)
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            call = self._calls.get(call_id)
            memo = ToolResultMemo(
                call_id=call_id,
                tool_name=(call.name if call is not None else "unknown_tool"),
                ref=f"memory://tool-result/{call_id}/{digest[:16]}",
                sha256=digest,
                chars=len(content),
                consumed=True,
                status="completed",
                summary=_bounded_summary(content),
                run_id=manifest.run_id,
                task_id=manifest.task_id,
                revision=manifest.revision,
            )
            memos_by_call[call_id] = memo
            if self.memo_store is not None and self.persist:
                self.memo_store.save(memo)
            self._consumed_calls.add(call_id)
            self._results.pop(call_id, None)

        tool_results = list(memos_by_call.values())
        if index is not None:
            index = index.model_copy(
                update={"tool_results": tool_results, "index_sha256": None}
            )

        capsule = manifest.task_state
        if capsule is not None:
            acquired = list(capsule.evidence_acquired)
            for item in evidence:
                identity = f"{item.ref}#sha256={item.sha256}"
                if identity not in acquired:
                    acquired.append(identity)
            result_refs = list(capsule.tool_result_refs)
            for memo in tool_results:
                if memo.ref not in result_refs:
                    result_refs.append(memo.ref)
            state = dict(capsule.state)
            state["provider_context_delivery_count"] = int(
                state.get("provider_context_delivery_count", 0) or 0
            ) + 1
            state["context_delivery_policy"] = "initial_full_then_ref_summary_v1"
            state["base_context_consumption_deferred"] = defer_base_consumption
            capsule = capsule.model_copy(
                update={
                    "evidence_acquired": acquired,
                    "tool_result_refs": result_refs,
                    "state": state,
                    "sequence": capsule.sequence + 1,
                }
            )

        return self._update_manifest(
            evidence=evidence,
            knowledge=knowledge,
            evidence_index=index,
            tool_results=tool_results,
            task_state_capsule=capsule,
            task=None,
            capsule=None,
        )

    def record_terminal(
        self,
        status: str,
        *,
        next_action: str | None = None,
        continuation_required: bool = False,
        partial_tail: Sequence[Any] | None = None,
        reason: str | None = None,
    ) -> ContextManifest:
        manifest = self._require_manifest()
        capsule = manifest.task_state
        if capsule is None:
            raise RuntimeError("manifest has no task state capsule")
        capsule = capsule.model_copy(
            update={
                "status": status,
                "next_action": next_action,
                "continuation_required": continuation_required,
            }
        )
        tail = [
            _message_dict(item) if not isinstance(item, Mapping) else dict(item)
            for item in (partial_tail or ())
        ]
        self._partial_tail = tail
        return self._update_manifest(
            task_state_capsule=capsule,
            task=None,
            capsule=None,
            partial_tail=tail,
        )

    # ------------------------------------------------------------------
    # Rebuild and verification
    # ------------------------------------------------------------------
    def _complete_units(self, messages: Sequence[Any]) -> list[AtomicToolUnit]:
        units: list[AtomicToolUnit] = []
        # Explicitly recorded units take precedence and are guaranteed to be
        # paired.  Otherwise parse complete units from the current history.
        if self._calls:
            for batch in self._call_batches:
                calls = [
                    self._calls[call_id]
                    for call_id in batch
                    if call_id in self._calls and call_id not in self._consumed_calls and call_id in self._results
                ]
                if not calls:
                    continue
                results = tuple(self._results[call.id] for call in calls)
                assistant = LLMMessage(role="assistant", content="", tool_calls=calls)
                try:
                    units.append(AtomicToolUnit(assistant, results))
                except ValueError:
                    continue
            if units:
                return units
        parsed = atomic_tool_units(messages)
        memos = {memo.call_id for memo in (self.manifest.tool_results if self.manifest else ())}
        return [unit for unit in parsed if not memos.intersection(unit.call_ids)]

    def _bounded_slices(
        self,
        items: Sequence[EvidenceSlice | KnowledgeSlice],
        *,
        budget: int,
    ) -> list[dict[str, Any]]:
        selected: list[dict[str, Any]] = []
        used = 0
        for item in items:
            text = item.content
            if item.consumed or text is None:
                text = f"ref={item.ref} sha256={item.sha256}"
                if item.summary:
                    text += f" summary={item.summary}"
            else:
                remaining = max(0, budget - used)
                if remaining <= 0:
                    break
                text = text[:remaining]
            payload = {
                "ref": item.ref,
                "sha256": item.sha256,
                "content": text,
                "consumed": item.consumed,
            }
            selected.append(payload)
            used += len(text)
            if used >= budget:
                break
        return selected

    def _minimal_tools(
        self,
        definitions: Sequence[dict[str, Any]],
        units: Sequence[AtomicToolUnit],
    ) -> list[dict[str, Any]]:
        manifest = self._require_manifest()
        capsule = manifest.task_state
        required = set(capsule.required_tool_names if capsule else ())
        required.update(
            _string(getattr(call, "name", ""))
            for unit in units
            for call in unit.tool_calls
        )
        required.update(
            memo.tool_name
            for memo in manifest.tool_results
            if memo.tool_name
        )
        # A continuation that still needs a typed submission must retain the
        # durable-part tools, but no unrelated project tools.
        if capsule and capsule.continuation_required:
            required.update({"submit_result", "list_result_parts"})
        if not required:
            # A fresh hook with no recorded state should not accidentally make
            # the legacy tool contract unusable.
            return [dict(item) for item in definitions]
        selected = [
            dict(item)
            for item in definitions
            if _string(item.get("name") or item.get("function", {}).get("name")) in required
        ]
        return selected

    def rebuild(
        self,
        messages: Sequence[LLMMessage],
        tool_definitions: Sequence[dict[str, Any]] | None = None,
        *,
        max_chars: int | None = None,
        max_evidence_chars: int | None = None,
        max_knowledge_chars: int | None = None,
        max_tool_units: int | None = None,
        manifest: ContextManifest | None = None,
        **_: Any,
    ) -> RebasedContext:
        if manifest is not None:
            self.load_verify(manifest)
        manifest = self._require_manifest()
        if manifest.manifest_sha256 and manifest.manifest_sha256 != manifest.computed_sha256:
            raise ValueError("context manifest canonical hash mismatch")
        units = self._complete_units(messages)
        units = units[-max(1, int(max_tool_units or self.max_tool_units)) :]

        # Stable prefix is copied first and deduplicated by exact role/content.
        prefix = [_message_from_dict(item) for item in manifest.stable_prefix]
        if not prefix:
            prefix = [item for item in messages if item.role == "system"][:1]
        out: list[LLMMessage] = list(prefix)
        seen = {(item.role, item.content, tuple(getattr(call, "id", "") for call in (item.tool_calls or ()))) for item in out}

        capsule = manifest.task_state
        if capsule is not None:
            typed_payload = json.dumps(
                capsule.model_dump(mode="json"),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            out.append(
                LLMMessage(
                    role="user",
                    content=f"<typed_task_state>\n{typed_payload}\n</typed_task_state>",
                )
            )

        evidence = manifest.evidence
        knowledge = manifest.knowledge
        if manifest.evidence_index is not None:
            evidence = [*manifest.evidence_index.evidence, *evidence]
            knowledge = [*manifest.evidence_index.knowledge, *knowledge]
        # De-duplicate hash-addressed slices while preserving source order.
        evidence = list({(item.ref, item.sha256): item for item in evidence}.values())
        knowledge = list({(item.ref, item.sha256): item for item in knowledge}.values())
        evidence_payload = self._bounded_slices(
            evidence,
            budget=max(1, int(max_evidence_chars or self.max_evidence_chars)),
        )
        knowledge_payload = self._bounded_slices(
            knowledge,
            budget=max(1, int(max_knowledge_chars or self.max_knowledge_chars)),
        )
        if evidence_payload or knowledge_payload:
            context_payload = json.dumps(
                {"evidence": evidence_payload, "knowledge": knowledge_payload},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            out.append(LLMMessage(role="user", content=f"<bounded_context>\n{context_payload}\n</bounded_context>"))

        for unit in units:
            out.extend(unit.messages)

        tail = manifest.partial_tail or self._partial_tail
        if not tail:
            # Keep only the current non-tool tail; older prose is intentionally
            # not resurrected from a forensic trace.
            tail = [
                _message_dict(item)
                for item in messages[-4:]
                if not getattr(item, "is_tool_result", False)
                and not (item.role == "assistant" and getattr(item, "tool_calls", None))
            ]
        tail_chars = 0
        for raw in tail:
            item = _message_from_dict(raw)
            if item.role == "system" or getattr(item, "is_tool_result", False):
                continue
            remaining = max(0, int(max_chars or self.max_partial_chars) - tail_chars)
            if remaining <= 0:
                break
            if item.content and len(item.content) > remaining:
                item = LLMMessage(
                    role=item.role,
                    content=item.content[-remaining:],
                    thinking=item.thinking,
                    cache_control=item.cache_control,
                )
            key = (item.role, item.content, tuple(getattr(call, "id", "") for call in (item.tool_calls or ())))
            if key not in seen:
                out.append(item)
                seen.add(key)
            tail_chars += len(item.content or "")

        tools = self._minimal_tools(tool_definitions or (), units)
        rebased = RebasedContext(
            messages=out,
            tool_definitions=tools,
            manifest=manifest,
            dropped_message_count=max(0, len(messages) - len(out)),
            reason="typed_context_manifest_v3",
        )
        return rebased

    def load_verify(
        self,
        source: ContextManifest | Path | str | Mapping[str, Any] | None = None,
        *,
        run_id: str | None = None,
        task_id: str | None = None,
        revision: int | None = None,
        expected_sha256: str | None = None,
        forensic: bool = False,
    ) -> ContextManifest:
        """Load a v3 manifest, or parse an old checkpoint/trace safely.

        Legacy traces are accepted for inspection only.  They are converted to
        a manifest with a stable system prefix; ``rebuild`` never opens them as
        a source unless their typed state has been explicitly recorded.
        """

        raw: Mapping[str, Any]
        if source is None:
            if self.store is None:
                raise FileNotFoundError("no context manifest source or store configured")
            manifest = self.store.load(
                run_id=run_id,
                task_id=task_id,
                revision=revision,
                expected_sha256=expected_sha256,
            )
            self.manifest = manifest
            return manifest
        if isinstance(source, ContextManifest):
            manifest = source
        elif isinstance(source, Mapping):
            raw = source
            manifest = self._manifest_from_payload(
                raw,
                run_id=run_id,
                task_id=task_id,
                revision=revision,
                forensic=forensic,
            )
        else:
            path = Path(source).expanduser().resolve()
            if not path.is_file():
                raise FileNotFoundError(str(source))
            if path.suffix == ".gz":
                payload = json.loads(gzip.decompress(path.read_bytes()))
            else:
                payload = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(payload, Mapping):
                raise ValueError("context source must contain an object")
            manifest = self._manifest_from_payload(
                payload,
                run_id=run_id,
                task_id=task_id,
                revision=revision,
                forensic=forensic,
            )
        expected = (
            run_id or (self.manifest.run_id if self.manifest else None),
            task_id or (self.manifest.task_id if self.manifest else None),
            revision if revision is not None else (self.manifest.revision if self.manifest else None),
        )
        if expected[0] is not None and manifest.run_id != expected[0]:
            raise ValueError("context manifest run_id mismatch")
        if expected[1] is not None and manifest.task_id != expected[1]:
            raise ValueError("context manifest task_id mismatch")
        if expected[2] is not None and manifest.revision != expected[2]:
            raise ValueError("context manifest revision mismatch")
        if manifest.manifest_sha256 and manifest.manifest_sha256 != manifest.computed_sha256:
            raise ValueError("context manifest canonical hash mismatch")
        if expected_sha256 and manifest.computed_sha256 != expected_sha256:
            raise ValueError("context manifest hash mismatch")
        self.manifest = manifest
        return manifest

    def _manifest_from_payload(
        self,
        payload: Mapping[str, Any],
        *,
        run_id: str | None,
        task_id: str | None,
        revision: int | None,
        forensic: bool,
    ) -> ContextManifest:
        if payload.get("schema_version") == 3 or payload.get("manifest_version") == 3:
            return ContextManifest.model_validate(payload)
        # v1 checkpoint and v2 compressed-trace manifests are read only to
        # recover identity and a small stable prefix.  The full message array
        # remains forensic and is deliberately not copied into provider state.
        source_run = _string(payload.get("run_id") or run_id)
        source_task = _string(payload.get("task_id") or task_id)
        source_revision = int(payload.get("revision", revision or 0) or 0)
        if not source_run or not source_task:
            raise ValueError("legacy context source lacks run/task identity")
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = payload.get("conversation_history")
        if not isinstance(messages, list):
            messages = []
        prefix = [dict(item) for item in messages if isinstance(item, Mapping) and item.get("role") == "system"][:1]
        capsule = TaskStateCapsule(
            run_id=source_run,
            task_id=source_task,
            revision=source_revision,
            objective="legacy checkpoint/trace (forensic only)",
            status="legacy_forensic",
        )
        return ContextManifest(
            run_id=source_run,
            task_id=source_task,
            revision=source_revision,
            stable_prefix=prefix,
            task_state_capsule=capsule,
            forensic_trace_ref=_string(payload.get("transcript_ref") or payload.get("trace_ref")) or None,
            forensic_trace_sha256=(
                _string(payload.get("transcript_sha256")) or None
            ),
        )


__all__ = ["ReportingContextRebuilder"]
