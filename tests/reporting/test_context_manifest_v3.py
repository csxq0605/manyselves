from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from manyselves.capabilities.distribution_reporting.runtime.models.agentic import TaskEnvelope
from manyselves.config.schema import AgentDefaults
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.providers.base import Message as LLMMessage
from manyselves.core.reporting.agent_runner import ReportingAgentRunner
from manyselves.core.reporting.config import load_packaged_agents
from manyselves.core.reporting.context_manifest import (
    HashOccurrenceTracker,
    build_manifest_payload,
    load_manifest,
)


class _Provider(LLMProvider):
    async def chat(self, messages, tools=None, **kwargs):  # pragma: no cover - not called
        raise AssertionError("Provider must not be called by manifest tests")


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_v3_tracker_scopes_repeats_to_full_namespace(tmp_path: Path) -> None:
    first = HashOccurrenceTracker(
        tmp_path,
        run_id="run-1",
        task_id="task-1",
        identity_key="agent-a",
        revision=0,
    )
    second = HashOccurrenceTracker(
        tmp_path,
        run_id="run-1",
        task_id="task-1",
        identity_key="agent-a",
        revision=1,
    )
    a = first.observe("system_prompt", "same", ref="a#system")
    b = first.observe("message", "same", ref="a#message")
    c = second.observe("system_prompt", "same", ref="b#system")
    assert a["status"] == "new"
    assert b["status"] == "repeated"
    assert b["first_occurrence"] == "a#system"
    assert c["status"] == "new"


def test_v3_tracker_concurrent_claim_has_one_first_occurrence(tmp_path: Path) -> None:
    def observe(index: int) -> dict:
        tracker = HashOccurrenceTracker(
            tmp_path,
            run_id="run-concurrent",
            task_id="task-concurrent",
            identity_key="agent-a",
            revision=0,
        )
        return tracker.observe("message", "same payload", ref=f"call-{index}#message")

    with ThreadPoolExecutor(max_workers=8) as pool:
        entries = list(pool.map(observe, range(32)))
    assert sum(entry["status"] == "new" for entry in entries) == 1
    first = next(entry["first_occurrence"] for entry in entries if entry["status"] == "new")
    assert all(entry["first_occurrence"] == first for entry in entries)


def test_v3_manifest_is_hash_only_and_reports_canonical_counters() -> None:
    payload = build_manifest_payload(
        run_id="run-1",
        task_id="task-1",
        identity_key="agent-a",
        revision=0,
        segments=[
            {
                "kind": "system_prompt",
                "ref": "m#system",
                "sha256": _sha("system"),
                "chars": 6,
                "status": "new",
                "stability": "stable",
            },
            {
                "kind": "tool_result",
                "ref": "m#tool",
                "sha256": _sha("tool"),
                "chars": 4,
                "status": "repeated",
                "stability": "dynamic",
                "duplicate": True,
            },
        ],
    )
    assert payload["context_manifest_version"] == 3
    assert payload["new_chars"] == 6
    assert payload["repeated_chars"] == 4
    assert payload["repeated_stable_chars"] == 0
    assert payload["repeated_dynamic_chars"] == 4
    assert payload["duplicate_tool_result_chars"] == 4
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "raw system body" not in serialized
    assert payload["manifest_sha256"] == _sha(
        json.dumps(
            {key: value for key, value in payload.items() if key != "manifest_sha256"},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    )


def test_runner_provider_manifest_records_bidirectional_call_identity(tmp_path: Path) -> None:
    runner = ReportingAgentRunner(tmp_path, MessageBus(), _Provider("test"), AgentDefaults())
    definition = load_packaged_agents()["module-2.1-specialist"]
    envelope = TaskEnvelope(
        task_id="task-manifest-v3",
        run_id="run-manifest-v3",
        agent_id=definition.id,
        objective="hash-only",
    )
    path = runner._write_provider_call_manifest(
        definition=definition,
        envelope=envelope,
        identity_key=definition.id,
        session_id="session-1",
        messages=[LLMMessage(role="user", content="private body")],
        tool_definitions=[],
        phase="initial",
        attempt=1,
        call_index=1,
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["manifest_kind"] == "provider_context_observation"
    assert load_manifest(raw, expected_kind="provider_context_observation").manifest_kind == (
        "provider_context_observation"
    )
    assert raw["provider_call_ref"] == path.relative_to(tmp_path).as_posix()
    assert len(raw["provider_call_hash"]) == 64
    assert all("content" not in segment for segment in raw["segments"])
    assert "private body" not in path.read_text(encoding="utf-8")


def test_manifest_loader_rejects_wrong_semantic_kind() -> None:
    payload = build_manifest_payload(
        run_id="run-1",
        task_id="task-1",
        identity_key="agent-a",
        revision=0,
        segments=[],
        manifest_kind="context_manifest_v3",
    )
    try:
        load_manifest(payload, expected_kind="provider_context_observation")
    except ValueError as exc:
        assert "kind mismatch" in str(exc)
    else:  # pragma: no cover - defensive assertion
        raise AssertionError("wrong manifest kind must fail closed")


def test_v3_manifest_reader_accepts_legacy_shape() -> None:
    from manyselves.core.reporting.context_manifest import ContextManifest

    manifest = ContextManifest.from_payload(
        {
            "context_manifest_version": 1,
            "run_id": "run-legacy",
            "task_id": "task-legacy",
            "revision": 0,
            "prompt_components": [
                {"kind": "system_prompt", "sha256": _sha("body"), "chars": 4}
            ],
            "messages": [{"role": "user", "content": "forensic body"}],
        }
    )
    assert manifest.context_manifest_version == 3
    assert manifest.run_id == "run-legacy"
    assert manifest.segments == []
