from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.core.reporting.context_state import (
    ContextManifest,
    EvidenceSlice,
    ResultPartRef,
    RunEvidenceIndex,
    TaskStateCapsule,
    TaskStateStore,
    ToolResultMemo,
)


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def test_v3_context_state_models_are_hashable_and_alias_compatible() -> None:
    evidence = EvidenceSlice(
        ref="Inputs/evidence.json",
        sha256=_sha("evidence"),
        content="现场证据",
        chars=4,
    )
    index = RunEvidenceIndex(
        run_id="run-1",
        task_id="task-1",
        items=[evidence],
        module_evidence_ids={"2.1": ["E-1"]},
    )
    assert index.evidence == [evidence]
    assert index.with_hash().index_sha256 == index.computed_sha256

    capsule = TaskStateCapsule(
        run_id="run-1",
        task_id="task-1",
        phase="authoring",
        completed_result_parts=["analysis"],
        open_questions=["待核对"],
    )
    manifest = ContextManifest(
        run_id="run-1",
        task_id="task-1",
        task_state_capsule=capsule,
        evidence_index=index,
    ).with_hash()
    assert manifest.task_state is capsule
    assert manifest.manifest_sha256 == manifest.computed_sha256


def test_consumed_payload_is_ref_hash_only() -> None:
    with pytest.raises(ValidationError, match="ref/hash"):
        ToolResultMemo(
            call_id="call-1",
            tool_name="read",
            ref="Work/tool.json",
            sha256=_sha("tool"),
            content="已经消费的完整结果",
        )
    with pytest.raises(ValidationError, match="ref/hash"):
        ResultPartRef(
            part_id="analysis",
            ref="Work/part.json",
            sha256=_sha("part"),
            content="已经消费的正文",
        )


def test_task_state_store_atomic_pointer_sequence_and_fail_closed(tmp_path: Path) -> None:
    store = TaskStateStore(tmp_path, "run-1", "task-1", 2)
    manifest = ContextManifest(
        run_id="run-1",
        task_id="task-1",
        revision=2,
        task_state_capsule=TaskStateCapsule(
            run_id="run-1", task_id="task-1", revision=2, status="running"
        ),
    )
    path = store.save(manifest)
    loaded = store.load()
    assert path.is_file()
    assert loaded.manifest_sha256 == loaded.computed_sha256
    assert len(json.loads(store.sequence_path.read_text())) == 1
    assert store.current_pointer()["manifest_sha256"] == loaded.manifest_sha256

    raw = json.loads(path.read_text())
    raw["task_id"] = "other-task"
    path.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="context manifest is invalid"):
        store.load(path)
