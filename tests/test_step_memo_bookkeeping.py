"""The Run's bookkeeping files are not a program step's writes.

Model calls (budget notes, the Tool result index) may change these files
while a program step runs.  The step memo must not record them as the step's
files, so a replay restores the step's own files and leaves them alone.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from manyselves.kernel.ports import ToolInvocationOutcome
from manyselves.runtime.step_memo import MEMO_DIR, RunStepMemo

RUN_ID = "run-1"


def _run_dir(workspace: Path) -> Path:
    return workspace / "Work" / "runs" / RUN_ID


class _Step:
    """A program step that writes a business file while a model call notes its budget."""

    definition = None

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace
        self.calls = 0

    def invoke(self, arguments: Any, *, task_id: str) -> ToolInvocationOutcome:
        self.calls += 1
        run_dir = _run_dir(self.workspace)
        (run_dir / "report.txt").write_text("business result", encoding="utf-8")
        # A model call running at the same time appends to budget notes and
        # starts the Tool result index.
        with (run_dir / "budget-notes.jsonl").open("a", encoding="utf-8") as handle:
            handle.write('{"note": "during step"}\n')
        (run_dir / "tool-result-index.jsonl").write_text('{"entry": 1}\n', encoding="utf-8")
        return ToolInvocationOutcome(status="ok", result={"done": True})


def _records(workspace: Path) -> list[dict[str, Any]]:
    root = _run_dir(workspace) / MEMO_DIR
    return [json.loads(path.read_text(encoding="utf-8")) for path in sorted(root.glob("*.json"))]


async def test_bookkeeping_files_changed_during_a_step_are_not_its_writes(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    (run_dir / "budget-notes.jsonl").write_text('{"note": "before"}\n', encoding="utf-8")

    step = _Step(tmp_path)
    memo = RunStepMemo(tmp_path, RUN_ID, replay=False)
    outcome = await memo.invoke(step, {"x": 1}, task_id="make_report")

    assert outcome.status == "ok"
    [record] = _records(tmp_path)
    prefix = f"Work/runs/{RUN_ID}/"
    assert set(record["writes"]) == {prefix + "report.txt"}
    assert record["removes"] == []


async def test_replay_restores_the_step_file_and_leaves_bookkeeping_alone(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    (run_dir / "budget-notes.jsonl").write_text('{"note": "before"}\n', encoding="utf-8")

    first = _Step(tmp_path)
    await RunStepMemo(tmp_path, RUN_ID, replay=False).invoke(first, {"x": 1}, task_id="make_report")
    assert first.calls == 1

    # Later: the business file is gone and model calls have kept noting.
    (run_dir / "report.txt").unlink()
    current_notes = '{"note": "before"}\n{"note": "during step"}\n{"note": "later"}\n'
    (run_dir / "budget-notes.jsonl").write_text(current_notes, encoding="utf-8")
    (run_dir / "tool-result-index.jsonl").write_text('{"entry": 2}\n', encoding="utf-8")
    notes_mtime = (run_dir / "budget-notes.jsonl").stat().st_mtime_ns
    index_mtime = (run_dir / "tool-result-index.jsonl").stat().st_mtime_ns

    second = _Step(tmp_path)
    memo = RunStepMemo(tmp_path, RUN_ID, replay=True)
    outcome = await memo.invoke(second, {"x": 1}, task_id="make_report")

    assert second.calls == 0
    assert outcome.reused is True
    assert memo.skipped == 1
    assert (run_dir / "report.txt").read_text(encoding="utf-8") == "business result"
    assert (run_dir / "budget-notes.jsonl").read_text(encoding="utf-8") == current_notes
    assert (run_dir / "budget-notes.jsonl").stat().st_mtime_ns == notes_mtime
    assert (run_dir / "tool-result-index.jsonl").read_text(encoding="utf-8") == '{"entry": 2}\n'
    assert (run_dir / "tool-result-index.jsonl").stat().st_mtime_ns == index_mtime


async def test_a_bookkeeping_file_removed_during_a_step_is_not_a_remove(
    tmp_path: Path,
) -> None:
    run_dir = _run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    (run_dir / "tool-result-index.jsonl").write_text('{"entry": 0}\n', encoding="utf-8")

    class _Clears(_Step):
        def invoke(self, arguments: Any, *, task_id: str) -> ToolInvocationOutcome:
            (_run_dir(self.workspace) / "tool-result-index.jsonl").unlink()
            (_run_dir(self.workspace) / "report.txt").write_text("r", encoding="utf-8")
            return ToolInvocationOutcome(status="ok")

    await RunStepMemo(tmp_path, RUN_ID, replay=False).invoke(
        _Clears(tmp_path), {}, task_id="make_report"
    )

    [record] = _records(tmp_path)
    assert set(record["writes"]) == {f"Work/runs/{RUN_ID}/report.txt"}
    assert record["removes"] == []


def test_read_basis_still_ignores_bookkeeping_and_sees_step_files(tmp_path: Path) -> None:
    run_dir = _run_dir(tmp_path)
    run_dir.mkdir(parents=True)
    memo = RunStepMemo(tmp_path, RUN_ID, replay=False)
    basis = memo.read_basis()

    for name in ("step-code.json", "run-options.json", "budget-notes.jsonl", "tool-result-index.jsonl"):
        (run_dir / name).write_text("changed", encoding="utf-8")
    assert memo.read_basis() == basis

    (run_dir / "report.txt").write_text("business", encoding="utf-8")
    assert memo.read_basis() != basis
