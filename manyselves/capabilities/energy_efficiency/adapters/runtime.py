"""Python Tools and runtime binding for the energy-efficiency Capability.

Every Tool is one deterministic step.  Finding and explaining loss items is
the loss finder's task; the order of the steps, the per-partition fan-out
and the one correction round are declared in the workflow files.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from manyselves.kernel.workflow.models import WorkflowStatus
from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding
from manyselves.runtime.state_store import FileWorkflowStateStore

from .. import CAPABILITY_FILE
from ..domain import finder_tools
from ..domain.check import check_submission, settle_partition
from ..domain.inputs import InputFormatError
from ..domain.prepare import parse_inputs, partition_meters, profile_meters, run_prechecks
from ..domain.quantify import quantify_register
from ..domain.register import write_register
from ..domain.report import build_report, write_report
from ..domain.requests import (
    CandidatesRequest,
    CheckRequest,
    PartitionRequest,
    QuantifyRequest,
    RenderRequest,
    ReportRequest,
    RunRef,
    SelectRequest,
    SettleRequest,
)
from ..domain.selection import SelectionError, load_rules, read_marks, select
from ..domain.store import DATA_DIR, RunDataError, freeze_inputs, read_json, run_dir, write_json

SELECTION_INPUTS = "energy-selection"
SOURCE_COPY = "source-register"
SOURCE_FILES = ("register.json", "sources.json")
REPORT_DIR = "energy-report"
FROZEN = ("energy-inputs", SELECTION_INPUTS)


def _relative(workspace: Path, path: Path) -> str:
    return path.resolve().relative_to(Path(workspace).resolve()).as_posix()


def _project_file(workspace: Path, name: str) -> Path:
    root = Path(workspace).resolve()
    path = (root / name).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        raise ValueError(f"not a project file: {name}")
    return path


def _value(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def tool_implementations(workspace: Path) -> dict[str, Callable[[Any], Any]]:
    """Bind the Capability Tools to one project workspace."""

    workspace = Path(workspace)

    def parse(values: Any) -> dict[str, Any]:
        return parse_inputs(workspace, RunRef.model_validate(_value(values)).run_id)

    def profile(values: Any) -> dict[str, Any]:
        return profile_meters(workspace, RunRef.model_validate(_value(values)).run_id)

    def prechecks(values: Any) -> dict[str, Any]:
        return run_prechecks(workspace, RunRef.model_validate(_value(values)).run_id)

    def partitions(values: Any) -> dict[str, Any]:
        request = PartitionRequest.model_validate(_value(values))
        return partition_meters(workspace, request.run_id, request.max_meters)

    def check(values: Any) -> dict[str, Any]:
        request = CheckRequest.model_validate(_value(values))
        return check_submission(workspace, request.scope, request.submission.model_dump(mode="json"))

    def settle(values: Any) -> dict[str, Any]:
        request = SettleRequest.model_validate(_value(values))
        return settle_partition(
            request.partition_id,
            request.first_submission.model_dump(mode="json"),
            request.first_check,
            {key: value.model_dump(mode="json") for key, value in request.corrected.items()},
            request.correction_errors,
        )

    def quantify(values: Any) -> dict[str, Any]:
        request = QuantifyRequest.model_validate(_value(values))
        results = {key: value.model_dump(mode="json") for key, value in request.partition_results.items()}
        register = quantify_register(workspace, request.run_id, results, request.partition_errors)
        paths = write_register(run_dir(workspace, request.run_id) / DATA_DIR, register)
        files = [_relative(workspace, path) for path in paths]
        return {
            "run_id": request.run_id,
            "site": register["site"],
            "period": register["period"],
            "counts": register["counts"],
            "files": files,
            "output_artifacts": [{"path": path} for path in files],
        }

    def choose(values: Any) -> dict[str, Any]:
        request = SelectRequest.model_validate(_value(values))
        report_request = ReportRequest.model_validate(request.request)
        frozen = run_dir(workspace, request.run_id) / SELECTION_INPUTS
        register = read_json(frozen / SOURCE_COPY / "register.json")
        marks = None
        rules = None
        if report_request.marks_file:
            codes = {item["id"]: item["check_code"] for item in register["items"]}
            marks = read_marks(frozen / Path(report_request.marks_file).name, codes)
        if report_request.rules_file:
            rules = load_rules(frozen / Path(report_request.rules_file).name)
        selection = select(register, marks, rules)
        write_json(run_dir(workspace, request.run_id) / REPORT_DIR / "selection.json", selection)
        return selection

    def render(values: Any) -> dict[str, Any]:
        request = RenderRequest.model_validate(_value(values))
        report_request = ReportRequest.model_validate(request.request)
        source = run_dir(workspace, request.run_id) / SELECTION_INPUTS / SOURCE_COPY
        register = read_json(source / "register.json")
        sources = read_json(source / "sources.json")["files"]
        markdown, page = build_report(register, request.selection, sources)
        paths = write_report(run_dir(workspace, request.run_id) / REPORT_DIR, markdown, page)
        files = [_relative(workspace, path) for path in paths]
        return {
            "run_id": request.run_id,
            "source_run_id": report_request.source_run_id,
            "counts": request.selection["counts"],
            "files": files,
            "output_artifacts": [{"path": path} for path in files],
        }

    def model_tool(handler: Callable[[Path, dict[str, Any]], dict[str, Any]]) -> Callable[[Any], Any]:
        return lambda values: handler(workspace, _value(values))

    return {
        "parse-energy-inputs": parse,
        "profile-energy-data": profile,
        "run-energy-prechecks": prechecks,
        "partition-energy-meters": partitions,
        "check-partition-items": check,
        "settle-partition-items": settle,
        "quantify-candidates": quantify,
        "select-energy-items": choose,
        "render-energy-report": render,
        "energy-series": model_tool(finder_tools.energy_series),
        "compare-windows": model_tool(finder_tools.compare_windows),
        "try-method": model_tool(finder_tools.try_method),
        "production-records": model_tool(finder_tools.production_records),
    }


def _source_completed(workspace: Path, source_run_id: str) -> None:
    """Refuse a candidates run the runtime recorded as not completed."""

    try:
        state = FileWorkflowStateStore(workspace).load(source_run_id)
    except FileNotFoundError:
        return  # steps run outside the runtime (tests, tools) keep no state
    if state.status != WorkflowStatus.COMPLETED:
        raise ValueError(f"run {source_run_id} did not complete ({state.status}); its register is not final")


def _freeze_selection(workspace: Path, run_id: str, request: ReportRequest) -> None:
    """Copy the source register and the person's files into the run, then check them.

    A problem in the marks or the rules refuses the start, so the person can
    fix the file and start again; nothing half-copied is left behind.
    """

    source = run_dir(workspace, request.source_run_id) / DATA_DIR
    frozen = run_dir(workspace, run_id) / SELECTION_INPUTS
    names = [name for name in (request.marks_file, request.rules_file) if name]
    if len({Path(name).name for name in names}) != len(names):
        raise ValueError("marks_file and rules_file need different file names")
    if any(Path(name).name == SOURCE_COPY for name in names):
        raise ValueError(f"a marks or rules file cannot be named {SOURCE_COPY}")
    fresh = not frozen.exists()
    try:
        copy = frozen / SOURCE_COPY
        if not (copy / SOURCE_FILES[0]).is_file():
            _source_completed(workspace, request.source_run_id)
        copy.mkdir(parents=True, exist_ok=True)
        for name in SOURCE_FILES:
            if (copy / name).is_file():
                continue
            if not (source / name).is_file():
                raise ValueError(f"run {request.source_run_id} has no candidate register ({name})")
            shutil.copy2(source / name, copy / name)
        for name in names:
            target = frozen / Path(name).name
            if not target.is_file():
                shutil.copy2(_project_file(workspace, name), target)
        register = read_json(copy / "register.json")
        if request.marks_file:
            codes = {item["id"]: item["check_code"] for item in register["items"]}
            marked = read_marks(frozen / Path(request.marks_file).name, codes)
            if not any(mark["decision"] for mark in marked.values()) and not request.rules_file:
                raise SelectionError(
                    "marks file marks no item: fill 选择 (保留/不保留/需补资料) in the register you marked, "
                    "or give a rules file"
                )
        if request.rules_file:
            load_rules(frozen / Path(request.rules_file).name)
    except Exception:
        if fresh:
            shutil.rmtree(frozen, ignore_errors=True)
        raise


def start_input(workspace: Path) -> Callable[[str, str, Any], dict[str, Any]]:
    """Validate each entrypoint's public input and freeze the files it names."""

    def project(run_id: str, workflow_id: str, values: Any) -> dict[str, Any]:
        try:
            if workflow_id == "energy-candidates":
                request = CandidatesRequest.model_validate(values)
                freeze_inputs(workspace, run_id, request.input_dir)
                return {"request": request.model_dump(mode="json"), "run-id": run_id}
            if workflow_id == "energy-report":
                request = ReportRequest.model_validate(values)
                _freeze_selection(workspace, run_id, request)
                return {"request": request.model_dump(mode="json"), "run-id": run_id}
        except (ValidationError, InputFormatError, RunDataError, SelectionError, OSError) as exc:
            raise ValueError(str(exc)) from exc
        raise ValueError(f"{workflow_id} is not an entrypoint of energy-efficiency")

    return project


def build_runtime_binding(
    *,
    workspace: Path,
    services: Any,
    **options: Any,
) -> GenericCapabilityRuntimeBinding:
    return GenericCapabilityRuntimeBinding(
        capability_file=CAPABILITY_FILE,
        workspace=workspace,
        services=services,
        tool_implementations=tool_implementations(Path(workspace)),
        start_input=start_input(Path(workspace)),
        frozen_inputs=FROZEN,
        **options,
    )


__all__ = ["build_runtime_binding", "start_input", "tool_implementations"]
