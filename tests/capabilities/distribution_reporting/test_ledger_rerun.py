"""A finished or failed report runs again in place and reuses unchanged steps.

The run keeps its id; the previous version moves to ``versions/v<n>``; every
model turn whose context did not change is replayed from the run's turn memo,
and every program step that did not change is skipped with its files restored.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from manyselves.capabilities.distribution_reporting import CAPABILITY_FILE
from manyselves.capabilities.distribution_reporting.domain.taxonomy import REPORT_TAXONOMY
from manyselves.capabilities.distribution_reporting.runtime.ledger_report import (
    ledger_report_tool_implementations,
)
from manyselves.capabilities.distribution_reporting.runtime.ledger_tools import LedgerTools
from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
    REPORT_MODULE_IDS,
)
from manyselves.config.schema import AgentDefaults
from manyselves.kernel.definitions import DefinitionKind
from manyselves.runtime import generic_binding
from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding
from manyselves.runtime.loops.bus import MessageBus
from manyselves.runtime.services import RuntimeServicesView
from tests.capabilities.distribution_reporting.test_ledger_report import (
    _answer,
    _evidence,
    _input_of,
    _row,
    _task,
    run_ledger_report,
)
from tests.runtime.scripted_loop import ScriptedLoop

REPORT = "Outputs/Reports/配电安全专家咨询报告.md"
COMMAND = UUID("60000000-0000-4000-8000-000000000002")
# The kernel's run id, so the report's files live in the run's own directory.
RUN_ID = "report-ledger-60000000000040008000000000000001"


async def _rerun(
    tmp_path: Path, operation, frozen_inputs: tuple[str, ...] = (), answer=_answer
) -> tuple[GenericCapabilityRuntimeBinding, list[str]]:
    """Run ``operation(binding)`` on a fresh process; return the tasks the model saw."""

    bus = MessageBus()
    loops: list[ScriptedLoop] = []

    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, answer, kwargs)
        loops.append(loop)
        return loop

    binding = GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=tmp_path,
        services=RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        tool_implementations=ledger_report_tool_implementations(
            tmp_path, input_snapshot=None, snapshot_content=None
        ),
        loop_builder=build,
        start_input=lambda run_id, workflow_id, values: {"state": values},
        # The test seeds facts in the run directory instead of preparing them.
        kept_on_rerun=("preparation",),
        frozen_inputs=frozen_inputs,
    )
    binding.entrypoints = (*binding.entrypoints, "report-ledger")
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        await operation(binding)
    finally:
        await binding.close()
        bus.shutdown()
        await bus_task
    return binding, [_task(m.content) for loop in loops for m in loop.received]


@pytest.mark.asyncio
async def test_rerun_with_nothing_changed_calls_no_model(tmp_path: Path) -> None:
    _binding, _accepted, loops = await run_ledger_report(tmp_path, run_id=RUN_ID)
    first_turns = sum(len(loop.received) for loop in loops)
    report = (tmp_path / REPORT).read_text(encoding="utf-8")

    binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.rerun(COMMAND, RUN_ID)
    )

    assert first_turns > 20 and tasks == []
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    assert (tmp_path / REPORT).read_text(encoding="utf-8") == report
    run_dir = tmp_path / "Work/runs" / RUN_ID
    # The previous version is kept whole; the run id did not change.
    archived = run_dir / "versions/v1"
    assert (archived / "runtime-state.json").is_file()
    assert (archived / "ledger/judgments.json").is_file()
    assert binding.versions(RUN_ID)[0]["status"] == "completed"
    hits = (run_dir / "agent-memo/hits.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(hits) == first_turns
    # No program step ran: each came back from the step memo with its files.
    steps = _last_execution(tmp_path)
    assert steps["executed"] == 0 and steps["skipped"] > 10
    assert (run_dir / "ledger/judgments.json").read_bytes() == (
        archived / "ledger/judgments.json"
    ).read_bytes()


@pytest.mark.asyncio
async def test_changed_frozen_input_runs_the_program_steps_again(tmp_path: Path) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)
    frozen = tmp_path / "Work/runs" / RUN_ID / "frozen-project/table.txt"
    frozen.parent.mkdir()
    frozen.write_text("原资料", encoding="utf-8")

    def rerun(binding):
        return binding.rerun(COMMAND, RUN_ID)

    await _rerun(tmp_path, rerun, frozen_inputs=("frozen-project",))
    # The same frozen input again: nothing runs.
    await _rerun(tmp_path, rerun, frozen_inputs=("frozen-project",))
    assert _last_execution(tmp_path)["executed"] == 0
    # The input is frozen again with new content (same size): every step runs.
    frozen.write_text("新资料", encoding="utf-8")
    binding, _tasks = await _rerun(tmp_path, rerun, frozen_inputs=("frozen-project",))

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    steps = _last_execution(tmp_path)
    assert steps["skipped"] == 0 and steps["executed"] > 10


def _last_execution(tmp_path: Path) -> dict[str, int]:
    path = tmp_path / "Work/runs" / RUN_ID / "step-memo/last-execution.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    return {"skipped": data["skipped"], "executed": data["executed"]}


@pytest.mark.asyncio
async def test_interrupted_run_continues_without_running_finished_steps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerTools.compose_report
    calls: list[int] = []

    def flaky(self, value):
        # A passing fault (a full disk): the code does not change.
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("disk full")
        return original(self, value)

    monkeypatch.setattr(LedgerTools, "compose_report", flaky)
    with pytest.raises(Exception, match="disk full"):
        await run_ledger_report(tmp_path, run_id=RUN_ID)
    before = _last_execution(tmp_path)

    binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.resume(COMMAND, RUN_ID)
    )

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    # It went on from the failed step: nothing before it ran again.
    assert tasks == []
    assert before["executed"] > 10
    assert _last_execution(tmp_path) == {"skipped": 0, "executed": 1}
    assert not (tmp_path / "Work/runs" / RUN_ID / "versions").exists()
    assert (tmp_path / REPORT).is_file()


@pytest.mark.asyncio
async def test_changed_code_redoes_only_that_step_and_what_follows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = LedgerTools.compose_report

    def broken(self, value):
        raise RuntimeError("bug in compose")

    monkeypatch.setattr(LedgerTools, "compose_report", broken)
    with pytest.raises(Exception, match="bug in compose"):
        await run_ledger_report(tmp_path, run_id=RUN_ID)
    # The bug is fixed: compose's code is no longer what the run executed.
    monkeypatch.setattr(LedgerTools, "compose_report", original)
    binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.resume(COMMAND, RUN_ID)
    )

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    assert tasks == []
    steps = _last_execution(tmp_path)
    # Compose is the last program step; everything before it was skipped.
    assert steps["executed"] == 1 and steps["skipped"] > 10
    assert binding.versions(RUN_ID)[0]["reason"] == "code changed"
    assert (tmp_path / REPORT).is_file()


@pytest.mark.asyncio
async def test_changed_definition_reruns_only_the_affected_turns(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _binding, _accepted, loops = await run_ledger_report(tmp_path, run_id=RUN_ID)
    first = [_task(m.content) for loop in loops for m in loop.received]
    state_path = tmp_path / "Work/runs" / RUN_ID / "runtime-state.json"
    # Simulate an interrupted run: resume then recompiles under the new plan.
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["status"] = "failed"
    state_path.write_text(json.dumps(state, ensure_ascii=False), encoding="utf-8")

    original = generic_binding.load_capability

    def changed(path):
        capability, registry = original(path)
        tasks = registry._definitions[DefinitionKind.TASK]
        task = tasks["write-section"]
        tasks["write-section"] = task.model_copy(
            update={"objective": task.objective + "\n每节先写结论。"}
        )
        return capability, registry

    monkeypatch.setattr(generic_binding, "load_capability", changed)
    binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.resume(COMMAND, RUN_ID)
    )

    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"
    assert tasks and set(tasks) <= set(first)
    # Judgment, relations, review and the chief's chapters are not redone.
    for kept in ("read-project", "judge-module", "relate-judgments", "review-judgments"):
        assert kept not in tasks
    assert tasks.count("write-section") == first.count("write-section")
    assert len(tasks) < len(first)
    assert (tmp_path / "Work/runs" / RUN_ID / "versions/v1/version.json").is_file()


@pytest.mark.asyncio
async def test_unchanged_unfinished_run_resumes_in_place(tmp_path: Path) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)

    binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.resume(COMMAND, RUN_ID)
    )

    # A completed run with the same plan is left as it is.
    assert tasks == []
    assert not (tmp_path / "Work/runs" / RUN_ID / "versions").exists()
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed"


def _revising_binding(tmp_path: Path, answer, loops: list[ScriptedLoop], bus: MessageBus):
    def build(**kwargs: Any) -> ScriptedLoop:
        loop = ScriptedLoop(bus, answer, kwargs)
        loops.append(loop)
        return loop

    def rerun_input(run_id, workflow_id, values, previous, archive):
        return {
            "state": previous["state"],
            "revision": {**values, "base_version": int(archive.name[1:])},
        }

    binding = GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=tmp_path,
        services=RuntimeServicesView(
            workspace=tmp_path,
            bus=bus,
            active_provider=object(),
            agent_defaults=AgentDefaults(),
            global_knowledge_root=None,
        ),
        tool_implementations=ledger_report_tool_implementations(
            tmp_path, input_snapshot=None, snapshot_content=None
        ),
        loop_builder=build,
        start_input=lambda run_id, workflow_id, values: {"state": values},
        rerun_input=rerun_input,
        kept_on_rerun=("preparation",),
    )
    binding.entrypoints = (*binding.entrypoints, "report-ledger")
    return binding


async def _revise(tmp_path: Path, values: dict[str, Any], answer) -> tuple[list[str], list[Any]]:
    bus = MessageBus()
    loops: list[ScriptedLoop] = []
    binding = _revising_binding(tmp_path, answer, loops, bus)
    bus_task = asyncio.create_task(bus.process_queue())
    try:
        await binding.rerun(COMMAND, RUN_ID, values)
        await binding.close()
    finally:
        bus.shutdown()
        await bus_task
    assert binding.get_run(RUN_ID)["run"]["status"] == "completed", binding.get_run(RUN_ID)[
        "state"
    ].get("error")
    messages = [m for loop in loops for m in loop.received]
    return [_task(m.content) for m in messages], messages


@pytest.mark.asyncio
async def test_wording_change_reaches_only_the_writing_of_that_section(
    tmp_path: Path,
) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)

    tasks, messages = await _revise(
        tmp_path,
        {
            "instruction": "改写 2.1 第一节的措辞。",
            "changes": [{"target": "2.1.1", "instruction": "语气更客观。", "scope": "text"}],
        },
        _answer,
    )

    # No judgment, relation or chief turn is redone; the rewritten section is
    # checked again.
    assert set(tasks) <= {"write-section", "edit-sections", "revise-sections", "review-joint"}
    assert "write-section" in tasks
    first = next(m for m in messages if _task(m.content) == "write-section")
    assert "语气更客观。" in first.content and '"submodule_id":"2.1.1"' in first.content


@pytest.mark.asyncio
async def test_judgment_change_reopens_only_its_module_and_the_changed_chief_entries(
    tmp_path: Path,
) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)

    def answer(message):
        if _task(message.content) == "judge-module" and "previous_rows" in message.content:
            value = _input_of(message.content)
            assert value["module_id"] == "2.3"
            assert value["revision"]["keep_row_ids"] == []
            assert value["revision"]["section_instructions"][0]["instruction"] == "重新判断。"
            row = _row("2.3", 3)
            row["basis"] = "按新要求重新判断，记录完整。"
            return ("submit", [{"module_id": "2.3", "rows": [row]}])
        if _task(message.content) == "chief-chapter-3":
            assert "previous_chapter3" in message.content
            assert '"J-2.3-1"' in message.content
        return _answer(message)

    first_section = next(iter(REPORT_TAXONOMY["2.3"].submodules))
    tasks, _messages = await _revise(
        tmp_path,
        {
            "instruction": "重新判断 2.3 第一节。",
            "changes": [{"target": first_section, "instruction": "重新判断。"}],
        },
        answer,
    )

    assert tasks.count("judge-module") == 1
    # Only 2.3's author redoes relations and review.
    assert tasks.count("relate-judgments") == 1
    assert tasks.count("review-judgments") == 1
    # The rewritten row has no problem: the chief's input reads as before, so
    # chapters 1 and 3 are carried over; only the section on the row is written again.
    assert not {"chief-chapter-3", "chief-chapter-1", "chief-chapter-1-alone"} & set(tasks)
    assert tasks.count("write-section") == 1
    run_dir = tmp_path / "Work/runs" / RUN_ID
    plan = json.loads((run_dir / "ledger/revision-plan.json").read_text(encoding="utf-8"))
    assert plan["affected_modules"] == ["2.3"]
    assert (run_dir / "versions/v1/report").exists() or (run_dir / "versions/v1/ledger").exists()


def test_kept_and_resubmitted_rows_keep_the_base_order() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        splice_kept_rows,
    )

    base = [{"id": "J-1"}, {"id": "J-2", "x": 0}, {"id": "J-3"}]
    plan = {"keep_rows": {"2.3": [base[0], base[2]]}, "base_rows": {"2.3": base}}
    rows = [{"id": "J-9"}, {"id": "J-2", "x": 1}]

    assert splice_kept_rows("2.3", rows, plan) == [
        {"id": "J-1"}, {"id": "J-2", "x": 1}, {"id": "J-3"}, {"id": "J-9"}
    ]


def test_revision_keeps_the_rows_as_the_base_version_delivered_them(tmp_path: Path) -> None:
    """Kept rows come from the reviewed ledger, not the first draft."""

    from manyselves.capabilities.distribution_reporting.runtime.models.reporting import (
        ReportRevision,
    )
    from manyselves.capabilities.distribution_reporting.runtime.report_revision import (
        plan_revision,
    )

    ledger = tmp_path / "ledger"
    ledger.mkdir()
    draft = {"2.3": [{"id": "J-1", "submodule_id": "2.3.1", "basis": "初稿"},
                     {"id": "J-2", "submodule_id": "2.3.2", "basis": "初稿"}]}
    reviewed = {"2.3": [{"id": "J-1", "submodule_id": "2.3.1", "basis": "审定"},
                        {"id": "J-2", "submodule_id": "2.3.2", "basis": "审定"}]}
    (ledger / "judgments-r0.json").write_text(json.dumps(draft), encoding="utf-8")
    (ledger / "judgments.json").write_text(json.dumps(reviewed), encoding="utf-8")
    revision = ReportRevision(
        base_version=1,
        instruction="重新判断 2.3.2。",
        changes=[{"target": "2.3.2", "instruction": "重新判断。"}],
    )

    plan = plan_revision(
        revision, base=tmp_path, items=[], sections={"2.3": ["2.3.1", "2.3.2"]}
    )

    assert plan["keep_rows"]["2.3"] == [reviewed["2.3"][0]]
    assert plan["base_rows"]["2.3"] == reviewed["2.3"]


@pytest.mark.asyncio
async def test_recompute_redoes_the_turns_another_model_answered(tmp_path: Path) -> None:
    """Continue keeps every turn; recompute redoes those of another model."""

    from manyselves.runtime.agent_memo import FileAgentTurnMemo

    await run_ledger_report(tmp_path, run_id=RUN_ID)
    memo = FileAgentTurnMemo(tmp_path, RUN_ID)
    records = [path for path in memo.root.glob("*.json") if path.name != "model-reuse.json"]
    assert records and all(
        json.loads(path.read_text(encoding="utf-8"))["model"].startswith("object:@")
        for path in records
    )
    # As if an earlier model had answered every turn.
    for path in records:
        record = json.loads(path.read_text(encoding="utf-8"))
        record["model"] = "EarlierProvider:old-model"
        path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")

    _binding, kept = await _rerun(tmp_path, lambda binding: binding.rerun(COMMAND, RUN_ID))
    assert kept == []

    memo.set_current_model_only(True)
    _binding, redone = await _rerun(tmp_path, lambda binding: binding.rerun(COMMAND, RUN_ID))
    assert "judge-module" in redone

    memo.set_current_model_only(False)
    assert not memo.current_model_only()


def _split_evidence() -> list[Any]:
    """The test facts, with 2.1's second section holding a record of its own."""

    items = _evidence()
    sections = list(REPORT_TAXONOMY["2.1"].submodules)
    first = next(item for item in items if item.module_id == "2.1")
    items.append(first.model_copy(update={
        "id": "E-0101", "subject": "设备101", "fact": "设备101现场检查记录完整",
        "submodule_id": sections[1],
        "source": first.source.model_copy(update={"row": 101}),
    }))
    return items


def _split_answer(message):
    """``_answer``, with each group of a module judged in groups writing its own rows."""

    value = _input_of(message.content)
    if _task(message.content) == "revise-chief-facts":
        return _any_fix(message)
    if _task(message.content) != "judge-module" or not (
        value.get("group_note") or len(value["evidence_ids"]) > 1
    ):
        return _answer(message)
    module_id = value["module_id"]
    start = re.search(r"J-[\d.]+-(\d+)", value.get("group_note") or "J-0-1").group(1)
    sections = {item.id: item.submodule_id for item in _split_evidence()}
    rows = []
    for number, evidence_id in enumerate(value["evidence_ids"]):
        row = _row(module_id, REPORT_MODULE_IDS.index(module_id) + 1)
        rows.append({
            **row,
            "id": f"J-{module_id}-{int(start) + number:02d}",
            "submodule_id": sections.get(evidence_id, row["submodule_id"]),
            "evidence_ids": [evidence_id],
        })
    return ("submit", [{"module_id": module_id, "rows": rows}])


async def _split_report(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from manyselves.capabilities.distribution_reporting.runtime import ledger_tools

    # One record per group, so 2.1 is judged in two groups as large modules are.
    monkeypatch.setattr(ledger_tools, "JUDGE_GROUP_RECORDS", 1)
    await run_ledger_report(
        tmp_path, answer=_split_answer, evidence=_split_evidence(), run_id=RUN_ID
    )
    ledger = tmp_path / "Work/runs" / RUN_ID / "ledger"
    assert (ledger / "judge-materials-2.1#2.md").is_file(), "2.1 was not judged in groups"


JUDGE_STAGE = {"judge-module", "relate-judgments", "review-judgments", "revise-judgments"}


@pytest.mark.asyncio
async def test_chapter_change_reuses_every_judge_of_a_module_judged_in_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A module judged in groups is judged in the same groups by a revision.

    Its judges then see the same materials as in the base version and are
    replayed; changing only chapter 1 redoes no judgment, relation or review.
    """

    await _split_report(tmp_path, monkeypatch)

    tasks, _messages = await _revise(
        tmp_path,
        {
            "instruction": "改第一章的写法。",
            "changes": [{"target": "1", "instruction": "概述更简洁。"}],
        },
        _split_answer,
    )

    assert not JUDGE_STAGE & set(tasks), tasks
    assert "chief-chapter-1-alone" in tasks


@pytest.mark.asyncio
async def test_rerun_of_a_module_judged_in_groups_calls_no_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    await _split_report(tmp_path, monkeypatch)

    _binding, tasks = await _rerun(
        tmp_path, lambda binding: binding.rerun(COMMAND, RUN_ID), answer=_split_answer
    )

    assert tasks == []


@pytest.mark.asyncio
async def test_judgment_change_in_one_group_rejudges_only_that_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Asking to rejudge one section of a module judged in groups reopens only its group."""

    await _split_report(tmp_path, monkeypatch)
    second = list(REPORT_TAXONOMY["2.1"].submodules)[1]

    tasks, messages = await _revise(
        tmp_path,
        {
            "instruction": f"重新判断 {second}。",
            "changes": [{"target": second, "instruction": "再看一遍。", "scope": "judgment"}],
        },
        _split_answer,
    )

    judges = [_input_of(m.content) for m in messages if _task(m.content) == "judge-module"]
    assert [judge["group_id"] for judge in judges] == ["2.1#2"]
    brief = judges[0]["revision"]
    assert {row["submodule_id"] for row in brief["previous_rows"]} == {second}
    assert brief["section_instructions"] == [{"section": second, "instruction": "再看一遍。"}]


def _any_fix(message):
    """``_answer``; a chief fix it was not written for changes nothing."""

    try:
        return _answer(message)
    except (KeyError, AssertionError):
        if _task(message.content) != "revise-chief-facts":
            raise
        return ("submit", [{}])


def _version_files(tmp_path: Path) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Each section's text and the chief's chapters, in the base version and now."""

    run_dir = tmp_path / "Work/runs" / RUN_ID
    read = lambda path: json.loads(path.read_text(encoding="utf-8"))  # noqa: E731
    return (
        read(run_dir / "versions/v1/ledger/section-texts.json"),
        read(run_dir / "ledger/section-texts.json"),
        read(run_dir / "versions/v1/ledger/chief-outputs.json"),
        read(run_dir / "ledger/chief-outputs.json"),
    )


WRITING_AND_REVIEW = {
    "write-section", "revise-sections", "edit-sections",
    "judge-module", "relate-judgments", "review-judgments", "revise-judgments",
    "review-cross-module",
}


@pytest.mark.asyncio
async def test_chapter_1_change_keeps_chapter_3_and_every_section(tmp_path: Path) -> None:
    """Only chapter 1 is written again; chapter 3 and chapter 2 are carried over as delivered."""

    await run_ledger_report(tmp_path, run_id=RUN_ID)
    tasks, messages = await _revise(
        tmp_path,
        {"instruction": "改第一章。", "changes": [{"target": "1", "instruction": "概述更简洁。"}]},
        _any_fix,
    )

    assert "chief-chapter-1-alone" in tasks
    assert "chief-chapter-3" not in tasks and "chief-chapter-1" not in tasks
    assert not WRITING_AND_REVIEW & set(tasks), tasks
    # The checks look at chapter 1 only.
    for message in messages:
        if _task(message.content) == "review-joint":
            value = _input_of(message.content)
            assert "sections" not in value
            assert all(unit["unit_id"].startswith("1") for unit in value.get("units") or [])
    texts_before, texts_now, chief_before, chief_now = _version_files(tmp_path)
    assert texts_now == texts_before
    assert chief_now["chapter3"] == chief_before["chapter3"]
    alone = next(m for m in messages if _task(m.content) == "chief-chapter-1-alone")
    assert "概述更简洁。" in alone.content


@pytest.mark.asyncio
async def test_chapter_3_change_keeps_chapter_1_and_every_section(tmp_path: Path) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)
    tasks, messages = await _revise(
        tmp_path,
        {"instruction": "改第三章。", "changes": [{"target": "3", "instruction": "汇总更简洁。"}]},
        _any_fix,
    )

    assert "chief-chapter-3" in tasks
    assert "chief-chapter-1" not in tasks and "chief-chapter-1-alone" not in tasks
    assert not WRITING_AND_REVIEW & set(tasks), tasks
    for message in messages:
        if _task(message.content) == "review-joint":
            value = _input_of(message.content)
            assert "sections" not in value
            assert all(unit["unit_id"].startswith("3") for unit in value.get("units") or [])
    texts_before, texts_now, chief_before, chief_now = _version_files(tmp_path)
    assert texts_now == texts_before
    assert chief_now["chapter1"] == chief_before["chapter1"]


@pytest.mark.asyncio
async def test_section_wording_change_writes_only_that_section(tmp_path: Path) -> None:
    await run_ledger_report(tmp_path, run_id=RUN_ID)
    first, second = list(REPORT_TAXONOMY["2.1"].submodules)[:2]
    tasks, messages = await _revise(
        tmp_path,
        {
            "instruction": f"改写 {second} 的措辞。",
            "changes": [{"target": second, "instruction": "语气更客观。", "scope": "text"}],
        },
        _any_fix,
    )

    written = [_input_of(m.content) for m in messages if _task(m.content) == "write-section"]
    assert [section["submodule_id"] for section in written] == [second]
    # The writer starts from the section as delivered, with the group's material.
    assert written[0]["revision_instruction"] == "语气更客观。"
    assert written[0]["previous_text"]
    assert "chief-chapter-1" not in tasks and "chief-chapter-3" not in tasks
    assert "chief-chapter-1-alone" not in tasks
    for message in messages:
        if _task(message.content) == "review-joint":
            value = _input_of(message.content)
            assert set(value.get("sections") or {}) <= {second} and not value.get("units")
    texts_before, texts_now, chief_before, chief_now = _version_files(tmp_path)
    assert {k: v for k, v in texts_now.items() if k != second} == {
        k: v for k, v in texts_before.items() if k != second
    }
    assert texts_now[first] == texts_before[first]
    assert chief_now["chapter1"] == chief_before["chapter1"]
    assert chief_now["chapter3"] == chief_before["chapter3"]
