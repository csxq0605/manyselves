"""Bind the run's own section tree around every tool call of this capability.

Chapter 2's tree is parsed once from the run's 评估信息汇总表 and carried in
the run state as ``report_taxonomy`` and persisted with the run's
preparation.  A tool call may run in a fresh task or thread, so each call
binds the tree from its own argument, or else from the persisted copy of the
run it names; a call made before the tree exists runs unbound, and any read
of the tree there fails.
"""

from __future__ import annotations

import functools
import inspect
import json
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any

from ..domain.taxonomy import bound_report_taxonomy, report_taxonomy_bound, run_taxonomy_source

_KEY = "report_taxonomy"


def _field(value: Any, key: str) -> Any:
    return value.get(key) if isinstance(value, Mapping) else getattr(value, key, None)


def _children(value: Any) -> list[Any]:
    if isinstance(value, Mapping):
        return list(value.values())
    return list(vars(value).values()) if hasattr(value, "__dict__") else []


def _find(value: Any, key: str, depth: int = 0) -> Any:
    found = _field(value, key)
    if found:
        return found
    if depth >= 1:
        return None
    for child in _children(value):
        if isinstance(child, Mapping) or hasattr(child, "__dict__"):
            if (nested := _find(child, key, depth + 1)) is not None:
                return nested
    return None


def persisted_taxonomy_ref(run_id: str) -> str:
    return f"Work/runs/{run_id}/preparation/report-taxonomy.json"


def _snapshot(argument: Any, workspace: Path | None) -> Mapping[str, Any] | None:
    found = _find(argument, _KEY)
    if isinstance(found, Mapping) and found:
        return found
    if workspace is None:
        return None
    # The run itself, or the earlier report run it works on.  Most specific
    # first: a triage or aggregate run names the report run it reads.
    for key in ("reporting_run_id", "source_run_id", "run_id", "run-id"):
        run_id = _find(argument, key)
        if isinstance(run_id, str) and run_id:
            path = Path(workspace) / persisted_taxonomy_ref(run_id)
            if path.is_file():
                return json.loads(path.read_text(encoding="utf-8"))
    return None


@contextmanager
def run_taxonomy(argument: Any, workspace: Path | None = None) -> Iterator[None]:
    """Bind the tree the argument (or its run) carries; else leave the context alone."""

    snapshot = _snapshot(argument, workspace)
    with bound_report_taxonomy(snapshot) if snapshot is not None else nullcontext():
        yield


def project_taxonomy(workspace: Path) -> dict[str, Any]:
    """The tree of the project's own 评估总表 in Inputs, for work outside a run.

    Distilling Skills aligns the expert report's sections to the product's
    own; those come from the same table a report run reads.
    """

    from ..domain.taxonomy import parse_report_taxonomy_workbook
    from .intake.manifest import _purpose_for

    inputs = Path(workspace) / "Inputs"
    tables = sorted(
        path for path in inputs.rglob("*")
        if path.is_file() and _purpose_for(path) == "s4-6"
    ) if inputs.is_dir() else []
    if len(tables) != 1:
        raise ValueError(
            "需要 Inputs 里恰好一份评估总表（S4-6）来读取第二章小节树；"
            f"找到 {len(tables)} 份"
        )
    return parse_report_taxonomy_workbook(
        tables[0], source_ref=tables[0].relative_to(workspace).as_posix()
    )


def run_taxonomy_scope(workspace: Path, run_id: str):
    """For a whole run: its persisted tree, read wherever no tree is bound."""

    path = Path(workspace) / persisted_taxonomy_ref(run_id)

    def read() -> Mapping[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    return run_taxonomy_source(read)


def with_project_taxonomy(
    implementations: Mapping[str, Callable[[Any], Any]], workspace: Path
) -> dict[str, Callable[[Any], Any]]:
    """Bind the project's own tree around calls that run outside a report run.

    A caller that already bound a tree keeps it.
    """

    def wrap(implementation: Callable[[Any], Any]) -> Callable[[Any], Any]:
        @functools.wraps(implementation)
        def bound(argument: Any) -> Any:
            if report_taxonomy_bound():
                return implementation(argument)
            with bound_report_taxonomy(project_taxonomy(workspace)):
                return implementation(argument)

        return bound

    return {name: wrap(impl) for name, impl in implementations.items()}


def with_run_taxonomy(
    implementation: Callable[[Any], Any], workspace: Path | None = None
) -> Callable[[Any], Any]:
    if inspect.iscoroutinefunction(implementation):

        @functools.wraps(implementation)
        async def bound_async(argument: Any) -> Any:
            with run_taxonomy(argument, workspace):
                return await implementation(argument)

        return bound_async

    @functools.wraps(implementation)
    def bound(argument: Any) -> Any:
        with run_taxonomy(argument, workspace):
            result = implementation(argument)
        if inspect.isawaitable(result):

            async def awaited() -> Any:
                with run_taxonomy(argument, workspace):
                    return await result

            return awaited()
        return result

    return bound


def bind_run_taxonomy(
    implementations: Mapping[str, Callable[[Any], Any]],
    workspace: Path | None = None,
) -> dict[str, Callable[[Any], Any]]:
    return {
        name: with_run_taxonomy(impl, workspace) for name, impl in implementations.items()
    }


__all__ = [
    "bind_run_taxonomy",
    "persisted_taxonomy_ref",
    "project_taxonomy",
    "run_taxonomy_scope",
    "run_taxonomy",
    "with_project_taxonomy",
    "with_run_taxonomy",
]
