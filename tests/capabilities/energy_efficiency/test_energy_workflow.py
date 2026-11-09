"""Both entrypoints run on the generic Runtime with a scripted loss finder."""

from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.energy_efficiency import CAPABILITY_FILE
from manyselves.capabilities.energy_efficiency.adapters.runtime import build_runtime_binding
from manyselves.capabilities.energy_efficiency.domain.methods import MENU
from manyselves.config.schema import AgentDefaults
from manyselves.interfaces.types import UserMessage
from manyselves.kernel.definitions import load_capability
from manyselves.runtime.capability_binding import CapabilityRunInputError
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.providers.base import LLMToolCall
from manyselves.runtime.providers.base import Message as LLMMessage
from manyselves.runtime.services import RuntimeServicesView
from tests.capabilities.energy_efficiency.synthetic import write_site
from tests.capabilities.energy_efficiency.test_energy_steps import categories
from tests.runtime.scripted_loop import ScriptedLoop


class FinderLoop(ScriptedLoop):
    """A scripted finder that can call its tools before it submits.

    A ``("work", calls, build)`` step calls each ``(tool, arguments)`` in
    turn and submits ``build(results)``; other steps are the base loop's.
    """

    async def start(self) -> None:
        await super().start()
        base = self._callback
        self.bus.unsubscribe(UserMessage, base)

        async def respond(message: UserMessage) -> None:
            if message.agent_type != self.kwargs["agent_type"]:
                return
            step = self.script(message)
            if step[0] != "work":
                script = self.script
                self.script = lambda _message: step
                try:
                    await base(message)
                finally:
                    self.script = script
                return
            self.received.append(message)
            self.history.append(LLMMessage(role="user", content=message.content))
            results = []
            for name, arguments in step[1]:
                result = await self.tools.get(name)(**arguments)
                self.feedback.append(result)
                results.append(result)
            for arguments in step[2](results):
                self.history.append(
                    LLMMessage(
                        role="assistant",
                        content="",
                        tool_calls=[LLMToolCall("c", "submit_result", arguments)],
                    )
                )
                feedback = await self.tools.get("submit_result")(**arguments)
                self.feedback.append(feedback)
                if feedback.get("status") != "correction_required":
                    return

        self._callback = respond
        self.bus.subscribe(UserMessage, respond)


def _input_of(content: str) -> dict[str, Any]:
    match = re.search(r"<input contract=\"[^\"]+\">\n(.*?)\n</input>", content, re.S)
    assert match is not None
    return json.loads(match.group(1))


def _coverage(meter_ids: list[str], found: dict[str, list[str]]) -> list[dict[str, Any]]:
    return [
        {
            "method_id": method_id,
            "meter_ids": meter_ids,
            "outcome": "found" if method_id in found else "none_found",
            "note": "查过",
            "item_keys": found.get(method_id, []),
        }
        for method_id in MENU
    ]


def _cause(evidence: str) -> list[dict[str, str]]:
    return [
        {"cause": "设备没有停", "kind": "loss", "basis": "data", "basis_detail": evidence, "how_to_check": "问值班人员"},
        {"cause": "有必须连续运行的服务", "kind": "necessary_service", "basis": "general_knowledge", "basis_detail": "公辅常识", "how_to_check": "问设备科"},
    ]


CATEGORY = [{"item_id": "LOSS-IDLE", "why": "非生产时段功率不降"}]


NIGHT = {"start_time": "20:00", "end_time": "08:00", "min_avg_kw": 5}
WEEKEND = {"state": "non_production", "min_avg_kw": 1}
GAPS = {"machine": "IM-01", "min_gap_minutes": 30, "max_gap_minutes": 120}
LIGHT_WEEK = [
    {"start": "2026-03-02 00:00", "end": "2026-03-07 00:00"},
    {"start": "2026-03-07 00:00", "end": "2026-03-09 00:00"},
]


def _p01(brief: dict[str, Any]):
    scope = brief["scope"]

    def build(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        evidence = results[0]["evidence_id"]
        return [
            {
                "partition_id": "P01",
                "items": [
                    {
                        "key": "A1",
                        "meter_ids": ["AC1"],
                        "windows": [{"start": "2026-03-02 20:00", "end": "2026-03-03 08:00"}],
                        "finding_method": {
                            "method_id": "daily-clock-window",
                            "params": NIGHT,
                            "description": "每天 20:00-08:00 的平均功率",
                        },
                        "phenomenon": "夜间平均 12 kW，13 个夜晚都在 5 kW 以上",
                        "evidence_ids": [evidence],
                        "reasoning": "画像里夜间和周末都是 12 kW，不随生产下降。",
                        "root_cause_candidates": _cause(evidence),
                        "candidate_categories": CATEGORY,
                        "missing_info": ["夜间是否有用气设备"],
                    }
                ],
                "coverage": _coverage(["AC1"], {"daily-clock-window": ["A1"]}),
                "category_coverage": categories(["AC1"], [{"meter_ids": ["AC1"], "candidate_categories": CATEGORY}]),
                "rule_item_notes": [{"rule_key": "R1", "note": "与 A1 是同一现象"}],
            }
        ]

    calls = [("try-method", {"scope": scope, "meter_ids": ["AC1"], "method_id": "daily-clock-window", "params": NIGHT})]
    return ("work", calls, build)


def _p02_first(brief: dict[str, Any]):
    scope = brief["scope"]

    def build(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        gaps = results[0]["evidence_id"]
        return [
            {
                "partition_id": "P02",
                "items": [
                    {
                        "key": "B1",
                        "meter_ids": ["IM1"],
                        "windows": [{"start": "2026-03-02 11:30", "end": "2026-03-02 12:30"}],
                        "finding_method": {"method_id": "production-gaps", "params": GAPS, "description": "午间批次间隙"},
                        "phenomenon": "午间间隙平均 8 kW",
                        "evidence_ids": [gaps],
                        "reasoning": "批次之间功率没有降到夜间水平。",
                        "root_cause_candidates": _cause(gaps),
                        "candidate_categories": CATEGORY,
                    },
                    {
                        "key": "B2",
                        "meter_ids": ["LT1"],
                        "windows": [{"start": "2026-03-07 07:00", "end": "2026-03-07 22:00"}],
                        "finding_method": {"method_id": "calendar-state", "params": WEEKEND, "description": "非生产日的照明"},
                        "phenomenon": "周末照明和工作日一样",
                        # Wrong: cites the moulding machine's evidence.
                        "evidence_ids": [gaps],
                        "reasoning": "照明画像周末和工作日相同。",
                        "root_cause_candidates": _cause(gaps),
                        "candidate_categories": CATEGORY,
                        "related_keys": ["B1"],
                        "relation_types": {"B1": "independent"},
                    },
                ],
                "coverage": _coverage(["IM1", "LT1"], {"production-gaps": ["B1"], "calendar-state": ["B2"]}),
                "category_coverage": categories(
                    ["IM1", "LT1"], [{"meter_ids": ["IM1", "LT1"], "candidate_categories": CATEGORY}]
                ),
            }
        ]

    calls = [("try-method", {"scope": scope, "meter_ids": ["IM1"], "method_id": "production-gaps", "params": GAPS})]
    return ("work", calls, build)


def _p02_fix(request: dict[str, Any]):
    assert request["check"]["ok"] is False
    assert [issue["key"] for issue in request["check"]["issues"]] == ["B2"]

    def build(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        submission = json.loads(json.dumps(request["submission"]))
        light = results[0]["evidence_id"]
        b2 = next(item for item in submission["items"] if item["key"] == "B2")
        b2["evidence_ids"] = [light]
        b2["root_cause_candidates"] = _cause(light)
        return [submission]

    calls = [("compare-windows", {"scope": request["scope"], "meter_id": "LT1", "windows": LIGHT_WEEK})]
    return ("work", calls, build)


STOPPED = {"state": "plant_stopped", "min_minutes": 600}


def _plant(brief: dict[str, Any]):
    scope = brief["scope"]
    assert brief["partition_id"] == "PLANT"
    assert {item["ref"] for item in brief["partition_items"]} >= {"P01:A1", "P02:B1"}
    assert len(brief["categories"]) == 17

    def build(results: list[dict[str, Any]]) -> list[dict[str, Any]]:
        evidence = results[0]["evidence_id"]
        item = {
            "key": "X1",
            "meter_ids": ["AC1"],
            "windows": [{"start": "2026-03-07 00:00", "end": "2026-03-08 00:00"}],
            "finding_method": {"method_id": "state-windows", "params": STOPPED, "description": "全厂停产时段的空压机"},
            "phenomenon": "全厂停产时空压机平均 12 kW",
            "evidence_ids": [evidence],
            "reasoning": "状态轴上全厂停产的长段里空压机没有停。",
            "root_cause_candidates": _cause(evidence),
            "candidate_categories": [{"item_id": "LOSS-SUPPORT", "why": "停产时公辅仍运行"}],
        }
        return [
            {
                "partition_id": "PLANT",
                "items": [item],
                "category_coverage": categories(["AC1"], [item]),
                "links": [{"left": "X1", "right": "P01:A1", "relation": "same_event_evidence", "note": "同是空压机不随生产停"}],
            }
        ]

    calls = [("try-method", {"scope": scope, "meter_ids": ["AC1"], "method_id": "state-windows", "params": STOPPED})]
    return ("work", calls, build)


def _answer(message):
    content = message.content
    if '<task id="find-plant">' in content:
        return _plant(_input_of(content))
    if '<task id="find-partition">' in content:
        brief = _input_of(content)
        assert '<context name="profile_text">' in content
        return _p01(brief) if brief["partition_id"] == "P01" else _p02_first(brief)
    if '<task id="fix-partition-items">' in content:
        return _p02_fix(_input_of(content))
    return ("say", "still looking")


def _binding(workspace: Path, bus: MessageBus, loops: list[FinderLoop]):
    def build(**kwargs: Any) -> FinderLoop:
        loop = FinderLoop(bus, _answer, kwargs)
        loops.append(loop)
        return loop

    return build_runtime_binding(
        workspace=workspace,
        services=RuntimeServicesView(
            workspace=workspace,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        loop_builder=build,
    )


def _values(binding: Any, run_id: str) -> dict[str, Any]:
    outputs = binding.get_outputs(run_id)["outputs"]
    return {item["id"]: item["value"] for item in outputs if item["kind"] == "value"}


def test_definitions_are_neutral() -> None:
    _capability, registry = load_capability(CAPABILITY_FILE)
    serialized = "\n".join(item.model_dump_json() for item in registry.all()).casefold()
    for term in ("distribution", "chief", "module-2.", "ledger", "photo"):
        assert term not in serialized


@pytest.mark.asyncio
async def test_candidates_then_report_on_a_made_up_site(tmp_path: Path) -> None:
    write_site(tmp_path / "data/site")
    bus = MessageBus()
    loops: list[FinderLoop] = []
    binding = _binding(tmp_path, bus, loops)
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        accepted = await binding.start(
            UUID("60000000-0000-4000-8000-000000000001"),
            "energy-candidates",
            {"input_dir": "data/site"},
        )
        run_id = accepted["run_id"]
        run = binding.get_run(run_id)
        outputs = _values(binding, run_id)

        assert run["run"]["status"] == "completed"
        register = outputs["register"]
        assert register["counts"] == {
            "model_items": 4,
            "rule_items": 1,
            "partitions": 2,
            "partitions_failed": 0,
            "plant_items": 1,
            "plant_pass": "ok",
        }
        assert {"path": f"Work/runs/{run_id}/energy/register.xlsx"} in register["output_artifacts"]
        artifacts = {item["path"] for item in binding.get_outputs(run_id)["outputs"] if item["kind"] == "artifact"}
        assert f"Work/runs/{run_id}/energy/register.xlsx" in artifacts

        data = json.loads((tmp_path / "Work/runs" / run_id / "energy/register.json").read_text(encoding="utf-8"))
        items = {item["local_key"]: item for item in data["items"]}
        assert items["B2"]["check_issues"] == []
        assert items["X1"]["partition_id"] == "PLANT" and items["X1"]["check_issues"] == []
        assert items["A1"]["id"] in items["X1"]["related_ids"] and items["X1"]["id"] in items["A1"]["related_ids"]
        # Weekends and weekday nights after 17:00 are a stopped plant: 12 kW there.
        assert items["X1"]["scalars"]["window_avg_kw"] == 12.0
        assert items["B2"]["related_ids"] == [items["B1"]["id"]]
        assert items["A1"]["scalars"]["replay_item_kwh"] == 13 * 12 * 12
        # Lighting is instant power: no energy is made up for it.
        assert items["B2"]["scalars"]["replay_item_kwh"] is None
        # One calendar row per weekend day; touching rows are one stretch: two weekends.
        assert items["B2"]["scalars"]["replay_hits"] == 2

        # One correction round, only in the partition whose check failed.
        by_partition = {_input_of(loop.received[0].content)["partition_id"]: loop for loop in loops}
        assert len(by_partition["P01"].received) == 1
        assert ['<task id="fix-partition-items">' in m.content for m in by_partition["P02"].received] == [False, True]
        state = run["state"]["variables"]
        assert set(state["partition_results"]) == {"P01", "P02"}
        assert state["partition_errors"] == {}

        with (tmp_path / "marks.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["编号", "核对码", "选择", "备注"])
            writer.writerow([items["B1"]["id"], items["B1"]["check_code"], "保留", "午休不停机"])
            writer.writerow([items["B2"]["id"], items["B2"]["check_code"], "需补资料", "问周末是否有人"])
        (tmp_path / "rules.yaml").write_text(
            "rules:\n- when:\n  - {field: source, op: eq, value: rule}\n  decision: drop\n  note: 与模型项重复\n",
            encoding="utf-8",
        )
        with pytest.raises(CapabilityRunInputError):
            await binding.start(
                UUID("60000000-0000-4000-8000-000000000002"),
                "energy-report",
                {"source_run_id": "energy-candidates-missing", "rules_file": "rules.yaml"},
            )
        report_run = await binding.start(
            UUID("60000000-0000-4000-8000-000000000003"),
            "energy-report",
            {"source_run_id": run_id, "marks_file": "marks.csv", "rules_file": "rules.yaml"},
        )
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task

    report_id = report_run["run_id"]
    assert binding.get_run(report_id)["run"]["status"] == "completed"
    report = _values(binding, report_id)["report"]
    assert report["counts"] == {"keep": 1, "drop": 1, "needs_data": 1, "unselected": 2}
    # The marks and rules were copied into the run when it started.
    assert (tmp_path / "Work/runs" / report_id / "energy-selection/marks.csv").is_file()
    assert (tmp_path / "Work/runs" / report_id / "energy-selection/source-register/register.json").is_file()
    text = (tmp_path / "Work/runs" / report_id / "energy-report/report.md").read_text(encoding="utf-8")
    kept = text.split("## 保留的候选损耗项")[1].split("## 需补资料的项")[0]
    assert f"### {items['B1']['id']}" in kept
    assert "午休不停机" in kept
    assert f"### {items['A1']['id']}" not in kept
