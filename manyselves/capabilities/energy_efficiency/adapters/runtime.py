"""Python Tools and runtime binding for the energy-efficiency Capability.

Every Tool is one deterministic step.  The program runs the lenses, computes
every item the analyst proposes over the whole period and screens its rows;
the analyst's turns (items per system, the residual rounds, the judgments)
and the one correction round of each are declared in the workflow files.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from pydantic_core import to_jsonable_python

from manyselves.kernel.workflow.models import WorkflowStatus
from manyselves.runtime.generic_binding import GenericCapabilityRuntimeBinding
from manyselves.runtime.state_store import FileWorkflowStateStore

from .. import CAPABILITY_FILE
from ..domain.analysis import (
    apply_looks,
    apply_residual,
    check_judgment,
    check_look,
    judge_briefs,
    lens_step,
    residual_brief,
    save_judgments,
    settle,
)
from ..domain.caliber import caliber_brief, settle_assumptions
from ..domain.foundation import build_foundation
from ..domain.inputs import MANIFEST_NAME, InputFormatError, load_manifest
from ..domain.ledger import compose, write_ledger
from ..domain.manifest_draft import settle_manifest, survey_brief, try_manifest
from ..domain.preparation import load_task, merged_scope
from ..domain.prepare import parse_inputs, profile_meters
from ..domain.report import build_report, write_report
from ..domain.requests import (
    ApplyLooksRequest,
    ApplyResidualRequest,
    CandidatesRequest,
    CheckRequest,
    RegisterRequest,
    RenderRequest,
    ReportRequest,
    ResidualRoundRequest,
    RunRef,
    SelectRequest,
    SettleAssumptionsRequest,
    SettleManifestRequest,
    SettleRequest,
    TryManifestRequest,
)
from ..domain.selection import SelectionError, load_rules, read_marks, select
from ..domain.store import (
    DATA_DIR,
    RunDataError,
    SiteData,
    freeze_inputs,
    narrowed,
    read_json,
    run_dir,
    write_json,
    write_task_scope,
)
from ..domain.workbook import build_register, write_register

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
    """A tool's input as plain data: the runtime may hand over models, also nested ones (an agent's answer)."""

    return dict(to_jsonable_python(value))


def _dumped(values: dict[str, Any]) -> dict[str, Any]:
    return {key: _value(value) for key, value in values.items()}


def register_candidates(workspace: Path, request: RegisterRequest) -> dict[str, Any]:
    """S6 and S7: recompose the ledger, keep the judgments and write the workbook."""

    site = SiteData(workspace, request.run_id)
    ledger = compose(site)
    write_ledger(site, ledger)
    results = _dumped(request.judge_results)
    save_judgments(site, results, request.judge_errors)
    register = build_register(site, ledger, results, request.judge_errors, request.assumption_errors)
    paths = write_register(site, run_dir(workspace, request.run_id) / DATA_DIR, register)
    files = [_relative(workspace, path) for path in paths]
    return {
        "run_id": request.run_id,
        "site": register["site"],
        "period": register["period"],
        "counts": register["counts"],
        "files": files,
        "output_artifacts": [{"path": path} for path in files],
    }


def tool_implementations(workspace: Path) -> dict[str, Callable[[Any], Any]]:
    """Bind the Capability Tools to one project workspace."""

    workspace = Path(workspace)

    def run_ref(handler: Callable[[Path, str], dict[str, Any]]) -> Callable[[Any], Any]:
        return lambda values: handler(workspace, RunRef.model_validate(_value(values)).run_id)

    def checked(handler: Callable[[Path, str, str, dict[str, Any]], dict[str, Any]]) -> Callable[[Any], Any]:
        def call(values: Any) -> dict[str, Any]:
            request = CheckRequest.model_validate(_value(values))
            return handler(workspace, request.run_id, request.key, request.submission)

        return call

    def check_draft(values: Any) -> dict[str, Any]:
        request = TryManifestRequest.model_validate(_value(values))
        return try_manifest(workspace, request.run_id, request.manifest.model_dump(mode="json"))

    def settle_draft(values: Any) -> dict[str, Any]:
        request = SettleManifestRequest.model_validate(_value(values))
        corrected = next(iter(request.corrected.values()), None)
        return settle_manifest(
            workspace,
            request.run_id,
            request.first.model_dump(mode="json"),
            corrected.model_dump(mode="json") if corrected else None,
        )

    def settle_drafted(values: Any) -> dict[str, Any]:
        request = SettleAssumptionsRequest.model_validate(_value(values))
        return settle_assumptions(workspace, request.run_id, request.draft.model_dump(mode="json"))

    def settled(values: Any) -> dict[str, Any]:
        request = SettleRequest.model_validate(_value(values))
        return settle(
            request.first_submission,
            request.first_check,
            _dumped(request.corrected),
            request.correction_errors,
        )

    def applied(values: Any) -> dict[str, Any]:
        request = ApplyLooksRequest.model_validate(_value(values))
        return apply_looks(workspace, request.run_id, _dumped(request.results), request.errors)

    def round_brief(values: Any) -> dict[str, Any]:
        request = ResidualRoundRequest.model_validate(_value(values))
        return residual_brief(workspace, request.run_id, request.round)

    def round_applied(values: Any) -> dict[str, Any]:
        request = ApplyResidualRequest.model_validate(_value(values))
        return apply_residual(workspace, request.run_id, request.round, _dumped(request.results), request.errors)

    def registered(values: Any) -> dict[str, Any]:
        return register_candidates(workspace, RegisterRequest.model_validate(_value(values)))

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

    return {
        "survey-energy-inputs": run_ref(survey_brief),
        "check-manifest": check_draft,
        "settle-manifest": settle_draft,
        "parse-energy-inputs": run_ref(parse_inputs),
        "profile-energy-data": run_ref(profile_meters),
        "caliber-facts": run_ref(caliber_brief),
        "settle-assumptions": settle_drafted,
        "build-foundation": run_ref(build_foundation),
        "run-lenses": run_ref(lens_step),
        "check-look": checked(check_look),
        "settle-look": settled,
        "apply-looks": applied,
        "residual-brief": round_brief,
        "apply-residual": round_applied,
        "judge-briefs": run_ref(judge_briefs),
        "check-judgment": checked(check_judgment),
        "settle-judgment": settled,
        "register-candidates": registered,
        "select-energy-items": choose,
        "render-energy-report": render,
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

    Neither file is needed: without them the screening by the preference
    rules decides.  A problem in a file refuses the start, so the person can
    fix it and start again; nothing half-copied is left behind.
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
            read_marks(frozen / Path(request.marks_file).name, codes)
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
                frozen = freeze_inputs(workspace, run_id, request.input_dir)
                scope = merged_scope(request.task_scope(), load_task(frozen))
                write_task_scope(workspace, run_id, scope)
                if (frozen / MANIFEST_NAME).is_file():
                    narrowed(load_manifest(frozen), scope)
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
