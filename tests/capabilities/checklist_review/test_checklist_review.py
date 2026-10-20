"""Checklist review runs on the same Kernel and Runtime as every Capability."""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.checklist_review import CAPABILITY_FILE
from manyselves.capabilities.checklist_review.adapters.runtime import (
    build_runtime_binding,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import load_capability
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from tests.runtime.scripted_loop import ScriptedLoop

DOCUMENT = "\n".join(
    [
        "# 设备维护规程",
        "## 巡检",
        "每月巡检一次，记录温度。",
        "## 应急",
        "停电时由值班人员负责切换备用电源。",
    ]
)
CHECKLIST = [
    {"id": "c1", "question": "是否规定了巡检周期？"},
    {"id": "c2", "question": "是否规定了备件库存？"},
    {"id": "c3", "question": "是否说明了停电时谁负责？"},
]


def _input_of(content: str) -> dict[str, Any]:
    match = re.search(r"<input contract=\"[^\"]+\">\n(.*?)\n</input>", content, re.S)
    assert match is not None
    return json.loads(match.group(1))


def _answer(message):
    content = message.content
    if '<task id="read-document">' in content:
        return (
            "submit",
            [
                {
                    "summary": "一份设备维护规程。",
                    "sections": [
                        {"title": "巡检", "points": ["每月一次"]},
                        {"title": "应急", "points": ["值班人员切换备用电源"]},
                    ],
                }
            ],
        )
    if '<task id="check-item">' in content:
        item = _input_of(content)
        if item["id"] == "c3":
            return ("say", "我觉得应该是值班人员。")
        verdict = {"c1": "met", "c2": "insufficient"}[item["id"]]
        evidence = ["每月巡检一次，记录温度。"] if verdict == "met" else []
        return (
            "submit",
            [
                {
                    "item_id": item["id"],
                    "verdict": verdict,
                    "evidence": evidence,
                    "note": "" if evidence else "文件没有写备件库存。",
                }
            ],
        )
    return ("say", "still talking")


def test_definitions_are_neutral() -> None:
    _capability, registry = load_capability(CAPABILITY_FILE)
    serialized = "\n".join(item.model_dump_json() for item in registry.all()).casefold()
    for term in ("reporting", "editor", "auditor", "cross", "chief", "module-2."):
        assert term not in serialized


@pytest.mark.asyncio
async def test_review_reads_once_forks_per_item_and_keeps_a_finding_table(
    tmp_path: Path,
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs/rules.md").write_text(DOCUMENT, encoding="utf-8")
    bus = MessageBus()
    loops: list[ScriptedLoop] = []

    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, _answer, kwargs)
        loops.append(loop)
        return loop

    binding = build_runtime_binding(
        workspace=tmp_path,
        services=RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        loop_builder=build,
    )
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        accepted = await binding.start(
            UUID("50000000-0000-4000-8000-000000000001"),
            "checklist-review",
            {"document_path": "docs/rules.md", "checklist": CHECKLIST},
        )
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task

    run = binding.get_run(accepted["run_id"])
    outputs = {
        item["id"]: item["value"]
        for item in binding.get_outputs(accepted["run_id"])["outputs"]
    }
    report = outputs["report"]

    assert run["run"]["status"] == "completed"
    assert report["counts"] == {"met": 1, "not_met": 0, "insufficient": 1, "failed": 1}
    assert report["open_items"] == ["c2"]
    assert [item["verdict"] for item in report["items"]] == ["met", "insufficient", "failed"]
    assert report["items"][2]["note"]
    assert outputs["result"] == report

    reading_loop = loops[0]
    assert len(reading_loop.received) == 1
    assert "停电时由值班人员负责切换备用电源。" in reading_loop.received[0].content
    item_loops = loops[1:]
    assert len(item_loops) == 3
    for loop in item_loops:
        # Each item conversation starts from the reading conversation.
        assert loop.restored[0][0].content == reading_loop.received[0].content
    state = run["state"]
    assert set(state["variables"]["findings"]) == {"c1", "c2"}
    assert set(state["variables"]["item_errors"]) == {"c3"}
