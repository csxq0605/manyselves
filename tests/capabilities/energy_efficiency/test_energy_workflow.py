"""Both entrypoints run on the generic Runtime with a scripted analyst."""

from __future__ import annotations

import asyncio
import csv
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest
import yaml

from manyselves.capabilities.energy_efficiency import CAPABILITY_FILE
from manyselves.capabilities.energy_efficiency.adapters.runtime import build_runtime_binding
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import load_capability
from manyselves.runtime.capability_binding import CapabilityRunInputError
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from tests.capabilities.energy_efficiency.synthetic import write_site
from tests.runtime.scripted_loop import ScriptedLoop

EXTRA_LIGHT = ("2026-03-10", "2026-03-11", "2026-03-12")


def _site(folder: Path) -> Path:
    """The made-up site, with the workshop lights at 12 kW instead of 6 kW on three working days from 09:00 to 17:00."""

    write_site(folder, prechecks=False)
    path = folder / "lighting.csv"
    with path.open(encoding="utf-8") as handle:
        rows = list(csv.reader(handle))
    for row in rows[1:]:
        if row[0][:10] in EXTRA_LIGHT and "09:00" <= row[0][11:] < "17:00":
            row[1] = "12.0"
    with path.open("w", encoding="utf-8", newline="") as handle:
        csv.writer(handle).writerows(rows)
    return folder


def _input_of(content: str) -> dict[str, Any]:
    match = re.search(r"<input contract=\"[^\"]+\">\n(.*?)\n</input>", content, re.S)
    assert match is not None
    return json.loads(match.group(1))


def _task(content: str) -> str | None:
    match = re.search(r'<task id="([^"]+)">', content)
    return match.group(1) if match else None


NIGHT = {
    "kind": "reference",
    "id": "night",
    "objects": ["AC1"],
    "when": {"plant": "STOP", "clock": "22:00-06:00"},
    "reference": {"type": "zero"},
    "alternatives": [{"type": "fixed_kw", "value": 3, "label": "保压"}],
    "basis": [{"source": "领域常识"}],
    "why": "全厂停的夜里不用压缩空气",
}
QUESTION = {"question": "三月十到十二日白天车间为什么开了加装的灯？", "ask_role": "production", "objects": ["LT1"]}


def _draft(brief: dict[str, Any]) -> dict[str, Any]:
    assert brief["key"] == "CALIBER"
    return {
        "assumptions": [
            {
                "topic": "AC1 的计量范围",
                "inference": "AC1 可能只量空压机，不带别的设备",
                "reasoning": "停产时功率稳定，不随机台起落；也可能只是别的负载同样不停。",
                "basis_fact_ids": ["AC1-COVER"],
                "meter_ids": ["AC1"],
                "affects": "停产时空压机的电量",
                "how_to_confirm": "看配电柜回路标签",
            }
        ]
    }


NOT_A_LOSS = {"id": "C2", "cause": "有设备要保压或值班用电", "kind": "necessary"}


def _proposal(local: str, title: str, differences: list[str] | None = None, spec: str | None = None, test: str | None = None) -> dict[str, Any]:
    loss: dict[str, Any] = {"id": "C1", "cause": "该关的时候没人关", "kind": "loss"}
    if test:
        loss["test"] = {"type": test}
    return {
        "id": local,
        "title": title,
        "differences": differences or [],
        **({"spec": spec} if spec else {}),
        "finding": "从程序差额和问卷的作息看出来的",
        "reasoning": "这段时间没有生产，用电却没有降下来",
        "causes": [loss, NOT_A_LOSS],
        "missing_info": "开关记录",
    }


def _look(brief: dict[str, Any]) -> dict[str, Any]:
    if brief["system"] == "动力站":
        assert brief["item_prefix"] == "S1" and brief["must_answer"] == ["L1-AC1-stop"]
        # Forgets the mechanisms: the check sends it back once.
        return {
            "key": brief["key"],
            "items": [
                _proposal("I1", "停产时空压机高于底功率", differences=["L1-AC1-stop"], test="base_load_when_stopped"),
                _proposal("I2", "停产的夜里空压机还在加载", spec="night"),
            ],
            "triggers": [],
            "mechanisms": [],
            "specs": [NIGHT],
        }
    assert [trigger["id"] for trigger in brief["triggers"]] == ["T-RES-LT1"]
    assert sorted(brief["must_answer"]) == ["L2c-LT1", "L4-LT1"]
    return {
        "key": brief["key"],
        "items": [_proposal("I1", "三个工作日白天车间灯比平常亮一倍", differences=["L2c-LT1", "L4-LT1"])],
        "triggers": [{"trigger": "T-RES-LT1", "disposition": "needs_data", "note": "数据里没有照明的开关记录"}],
        "mechanisms": [
            {"mechanism": "午休批次间隙", "objects": ["IM1"], "outcome": "no_evidence", "note": "L1-IM1-between 不到电量下限"},
            {"mechanism": "照明随作息", "objects": ["LT1"], "outcome": "item", "items": ["I1"], "note": "三天白天加灯"},
        ],
        "site_questions": [{**QUESTION, "settles": ["T-RES-LT1"]}],
    }


def _fix_look(request: dict[str, Any]) -> dict[str, Any]:
    assert any("机理" in issue["problem"] for issue in request["check"]["issues"])
    fixed = json.loads(json.dumps(request["submission"]))
    fixed["mechanisms"] = [{"mechanism": "停产夜间空压机加载", "objects": ["AC1"], "outcome": "item", "items": ["I2"], "note": "停产时管网没人用气"}]
    return fixed


def _residual(brief: dict[str, Any]) -> dict[str, Any]:
    assert brief["round"] == 1 and brief["item_prefix"] == "R1"
    slices = [entry["id"] for entry in brief["slices"]]
    assert slices and all(entry.startswith("S-IM1-") for entry in slices)
    assert [entry["id"] for entry in brief["differences"]] == ["L1-IM1-between"]
    first, *rest = slices
    return {
        "key": brief["key"],
        "slices": [
            {"slice": first, "disposition": "explained", "specs": ["lunch-heat"], "note": "午休时机台保温"},
            *({"slice": entry, "disposition": "new_item", "items": ["I1"], "note": "午休时机台没停"} for entry in rest),
        ],
        "items": [_proposal("I1", "午休时注塑机没停", differences=["L1-IM1-between"])] if rest else [],
        "specs": [
            {
                "kind": "residual",
                "id": "lunch-heat",
                "objects": ["IM1"],
                "when": {"weekdays": [1, 2, 3, 4, 5], "clock": "11:30-12:30"},
                "type": "necessary_service",
                "request": "问生产科午休时机台是否要保温",
                "why": "注塑机午休保温",
            }
        ],
        "site_questions": [{**QUESTION, "settles": ["lunch-heat"]}],
    }


def _judgment(entry: dict[str, Any]) -> dict[str, Any]:
    # Keeps the proposal's causes, whatever the tests said: the check sends back the contradicted ones.
    causes = [{"id": "C1", "cause": "停产后没人关", "kind": "loss", "status": "kept"}, {**NOT_A_LOSS, "status": "open"}]
    return {
        "candidate": entry["id"],
        "opinion": "likely_loss",
        "reference_choice": entry["default"],
        "reference_reason": "本表自己在可比时段的功率",
        "causes": causes,
        "measures": [{"type": "management", "action": "交班时检查并关闭", "owner_role": "equipment"}],
    }


class Analyst:
    """Answers each brief as a person might, and remembers what it was asked."""

    def __init__(self) -> None:
        self.seen: dict[str, list[dict[str, Any]]] = {}

    def __call__(self, message: Any) -> tuple[Any, ...]:
        task = _task(message.content)
        if task is None:
            return ("say", "还在看")
        value = _input_of(message.content)
        self.seen.setdefault(task, []).append(value)
        if task == "draft-assumptions":
            return ("submit", [_draft(value)])
        if task == "look-system":
            return ("submit", [_look(value)])
        if task == "fix-look":
            return ("submit", [_fix_look(value)])
        if task == "look-residual":
            return ("submit", [_residual(value)])
        if task == "judge-system":
            return ("submit", [{"key": value["key"], "judgments": [_judgment(entry) for entry in value["judge"]]}])
        if task == "fix-judgment":
            fixed = json.loads(json.dumps(value["submission"]))
            for judgment in fixed["judgments"]:
                for cause in judgment["causes"]:
                    if any(cause["id"] in issue["key"] and judgment["candidate"] in issue["key"] for issue in value["check"]["issues"]):
                        cause["status"] = "dropped"
            return ("submit", [fixed])
        raise AssertionError(f"no answer for {task}")


def _binding(workspace: Path, bus: MessageBus, script: Any):
    return build_runtime_binding(
        workspace=workspace,
        services=RuntimeServicesView(
            workspace=workspace,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        loop_builder=lambda **kwargs: ScriptedLoop(bus, script, kwargs),
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
    _site(tmp_path / "data/site")
    bus = MessageBus()
    analyst = Analyst()
    binding = _binding(tmp_path, bus, analyst)
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        accepted = await binding.start(
            UUID("60000000-0000-4000-8000-000000000001"),
            "energy-candidates",
            {"input_dir": "data/site"},
        )
        run_id = accepted["run_id"]
        run = binding.get_run(run_id)
        register = _values(binding, run_id)["register"]
        data = json.loads((tmp_path / "Work/runs" / run_id / "energy/register.json").read_text(encoding="utf-8"))
        items = {item["id"]: item for item in data["items"]}

        assert run["run"]["status"] == "completed", run["run"]
        # One look per system; only the power station's was sent back, once.
        assert sorted(brief["key"] for brief in analyst.seen["look-system"]) == ["look-动力站", "look-注塑车间"]
        assert [request["key"] for request in analyst.seen["fix-look"]] == ["look-动力站"]
        # The items' rows: the adopted differences carry their whole-period numbers, the night spec is computed over the whole period.
        assert {row: items[row]["item"] for row in ("L1-AC1-stop", "M-night-AC1", "L2c-LT1", "L4-LT1", "L1-IM1-between")} == {
            "L1-AC1-stop": "S1-I1",
            "M-night-AC1": "S1-I2",
            "L2c-LT1": "S2-I1",
            "L4-LT1": "S2-I1",
            "L1-IM1-between": "R1-I1",
        }
        night = items["M-night-AC1"]
        assert night["excess_kwh"] == 1329.0 and night["screen"]["rule"] == "S8"
        # The extra light is all in the second half of the period: pending as a possible one-off.
        assert items["L2c-LT1"]["screen"]["rule"] == "S10"
        # The lunch hour adopted in the residual round is under the floor.
        assert items["L1-IM1-between"]["screen"]["rule"] == "S2"
        # One residual round: the analyst took up all of it, so the next round had nothing to brief.
        assert len(analyst.seen["look-residual"]) == 1
        assert data["rounds"][0]["gain_kwh"] == 70.0 and data["rounds"][0]["stopped"] is False
        assert data["summary"]["residual_open_kwh"] == 0.0
        assert {entry["id"] for entry in data["loss_items"]} == {"S1-I1", "S1-I2", "S2-I1", "R1-I1"}
        # One round of judgments per system, with the proposal's test already run; the contradicted cause was sent back and dropped.
        assert sorted(brief["key"] for brief in analyst.seen["judge-system"]) == ["judge-动力站", "judge-注塑车间"]
        power = next(value for value in analyst.seen["judge-system"] if value["key"] == "judge-动力站")
        stop_brief = next(entry for entry in power["judge"] if entry["id"] == "L1-AC1-stop")
        assert {entry["result"] for entry in stop_brief["tests"]} == {"contradicts"}
        assert [request["key"] for request in analyst.seen["fix-judgment"]] == ["judge-动力站"]
        stop = items["L1-AC1-stop"]["judgment"]
        assert stop["causes"][0]["status"] == "dropped"
        # The trigger's answer and the site questions reach the workbook.
        assert data["triggers"][0]["answer"]["disposition"] == "needs_data"
        assert any(question["question"] == QUESTION["question"] for question in data["site_questions"])
        assert register["counts"]["systems_looked"] == 2 and register["counts"]["assumptions_drafted"] == 1
        for name in ("register.xlsx", "register.html", "分析偏好.xlsx"):
            assert {"path": f"Work/runs/{run_id}/energy/{name}"} in register["output_artifacts"]

        with (tmp_path / "marks.csv").open("w", encoding="utf-8", newline="") as handle:
            writer = csv.writer(handle)
            writer.writerow(["编号", "核对码", "取舍", "理由", "选定参照"])
            writer.writerow(["M-night-AC1", items["M-night-AC1"]["check_code"], "保留", "夜里不用气", "保压"])
            writer.writerow(["L2c-LT1", items["L2c-LT1"]["check_code"], "需补资料", "问三天白天加灯的原因", ""])
        (tmp_path / "rules.yaml").write_text(
            "rules:\n- when:\n  - {field: lens, op: eq, value: L4}\n  decision: drop\n  note: 和日电量先例是同一现象\n",
            encoding="utf-8",
        )
        with pytest.raises(CapabilityRunInputError):
            await binding.start(
                UUID("60000000-0000-4000-8000-000000000002"),
                "energy-report",
                {"source_run_id": "energy-candidates-missing"},
            )
        report_run = await binding.start(
            UUID("60000000-0000-4000-8000-000000000003"),
            "energy-report",
            {"source_run_id": run_id, "marks_file": "marks.csv", "rules_file": "rules.yaml"},
        )
        screened = await binding.start(
            UUID("60000000-0000-4000-8000-000000000004"),
            "energy-report",
            {"source_run_id": run_id},
        )
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task

    report_id = report_run["run_id"]
    assert binding.get_run(report_id)["run"]["status"] == "completed"
    assert (tmp_path / "Work/runs" / report_id / "energy-selection/marks.csv").is_file()
    text = (tmp_path / "Work/runs" / report_id / "energy-report/report.md").read_text(encoding="utf-8")
    kept = text.split("## 保留的损耗项")[1].split("## 需补资料或待定的项")[0]
    assert "### M-night-AC1" in kept and "夜里不用气" in kept and "997 kWh（参照：保压，人选" in kept
    assert "### L1-AC1-stop" in kept  # screened valid, nobody marked it
    dropped = text.split("## 人或规则不保留的项")[1].split("## 覆盖与剩余")[0]
    assert "L4-LT1（规则 1）" in dropped
    # Without marks or rules the screening decides alone.
    assert binding.get_run(screened["run_id"])["run"]["status"] == "completed"
    counts = _values(binding, screened["run_id"])["report"]["counts"]
    assert counts["keep"] == data["counts"]["valid"] and counts["needs_data"] == data["counts"]["pending"]


@pytest.mark.asyncio
async def test_a_folder_without_manifest_is_read_with_a_drafted_one(tmp_path: Path) -> None:
    folder = _site(tmp_path / "data/site")
    written = yaml.safe_load((folder / "manifest.yaml").read_text(encoding="utf-8"))
    (folder / "manifest.yaml").unlink()
    wrong = json.loads(json.dumps(written))
    # The first draft takes the moulding machine's readings for interval energy.
    moulding = next(meter for meter in wrong["meters"] if meter["id"] == "IM1")
    moulding.update({"semantics": "interval_energy", "timestamp_marks": "end"})
    wrong["assumptions"] = [{"id": "M1", "topic": "IM1 读数", "inference": "可能是每段电量", "basis": "列名"}]
    written["assumptions"] = [{"id": "M1", "topic": "IM1 读数", "inference": "可能是累计读数", "basis": "读数从不下降"}]
    seen: dict[str, Any] = {}
    analyst = Analyst()

    def answer(message: Any) -> tuple[Any, ...]:
        task = _task(message.content)
        if task == "draft-manifest":
            survey = _input_of(message.content)
            seen["files"] = sorted({table["file"] for table in survey["tables"]})
            return ("submit", [wrong])
        if task == "fix-manifest":
            seen["issues"] = _input_of(message.content)["check"]["issues"]
            return ("submit", [written])
        return analyst(message)

    bus = MessageBus()
    binding = _binding(tmp_path, bus, answer)
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        accepted = await binding.start(
            UUID("60000000-0000-4000-8000-000000000011"), "energy-candidates", {"input_dir": "data/site"}
        )
        run_id = accepted["run_id"]
        run = binding.get_run(run_id)
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task

    assert run["run"]["status"] == "completed", run["run"]
    assert "compressor.csv" in seen["files"] and "production.csv" in seen["files"]
    assert any("IM1" in issue and "累计" in issue for issue in seen["issues"])
    data = json.loads((tmp_path / "Work/runs" / run_id / "energy/register.json").read_text(encoding="utf-8"))
    assert data["manifest_origin"]["kept"] == "corrected"
    assert "M-night-AC1" in {item["id"] for item in data["items"]}
    drafted = next(entry for entry in data["assumptions"] if entry["id"] == "M1")
    assert drafted["origin"] == "manifest-drafted" and drafted["inference"] == "可能是累计读数"
