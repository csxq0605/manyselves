"""Skip program steps whose definition, code and input did not change.

Every Tool step a Run executes is recorded under ``step-memo/``: its result
and the files it wrote or removed, keyed by what determines that result:

- the Tool definition and the step id;
- the code: the source of the implementation's module and of every package
  module it imports, plus the package's data files (not its definition files,
  which a plan already pins);
- the input value the step was given;
- the sources outside the Run: the workspace's own files (everything but
  ``Work/``, ``Outputs/`` and logs), the global knowledge library, and the
  content of the Run's frozen inputs.

When a Run is executed again in place, a step with a recorded key is skipped:
its files are written back exactly as recorded and its result is returned.
This holds only while every earlier step was skipped.  A step may read files
that earlier steps wrote, and which files it reads is not known; so from the
first step that has to run, every later step runs too.  Model turns are not
affected by this rule: their whole input is in their own memo key.

Tool steps run one at a time while a Run is recorded, so the files each one
wrote are its own.
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import importlib.util
import inspect
import json
import os
import shutil
import time
from collections.abc import Iterable, Mapping
from functools import partial
from pathlib import Path
from typing import Any

from loguru import logger

from manyselves.kernel.ports import ToolInvocationOutcome

from .tools.result_memory import canonical_tool_arguments

MEMO_DIR = "step-memo"
# Run-directory entries the runtime itself writes; they are never a step's files.
RUNTIME_OWNED = frozenset(
    {
        MEMO_DIR,
        "agent-memo",
        "agent-results",
        "agent-conversations",
        "conversations",
        "versions",
        "runtime-state.json",
        "workflow-events.jsonl",
        "resolved-plan.json",
        "start-input.json",
    }
)
# Workspace-level runtime files under ``Work/``.
_RUNTIME_WORK = frozenset({"leases", "conversations.json"})
# Workspace entries that are not a source of any step.
_NOT_SOURCES = frozenset({"Work", "Outputs", "logs"})
_PACKAGE = "manyselves"
_PACKAGE_DIR = Path(__file__).resolve().parents[1]


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _file_sha(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _listing(root: Path, *, skip: frozenset[str] = frozenset()) -> list[tuple[str, int, int]]:
    """(relative path, size, mtime) of every file under ``root``."""

    rows: list[tuple[str, int, int]] = []
    if not root.is_dir():
        return rows
    for dirpath, dirnames, filenames in os.walk(root):
        relative_dir = Path(dirpath).relative_to(root)
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not name.startswith(".")
            and name != "__pycache__"
            and (relative_dir != Path(".") or name not in skip)
        )
        for name in sorted(filenames):
            if name.startswith(".") or (relative_dir == Path(".") and name in skip):
                continue
            path = Path(dirpath) / name
            try:
                stat = path.stat()
            except OSError:
                continue
            rows.append(((relative_dir / name).as_posix(), stat.st_size, stat.st_mtime_ns))
    return rows


# -- code ---------------------------------------------------------------------

_module_imports: dict[tuple[str, int, int], frozenset[str]] = {}
_file_digests: dict[tuple[str, int, int], str] = {}


def _module_file(name: str) -> Path | None:
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin or not spec.origin.endswith(".py"):
        return None
    return Path(spec.origin)


def _imports_of(name: str, path: Path) -> frozenset[str]:
    """Package modules ``name`` imports anywhere in its source."""

    stat = path.stat()
    cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
    cached = _module_imports.get(cache_key)
    if cached is not None:
        return cached
    tree = ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
    package = name if path.name == "__init__.py" else name.rpartition(".")[0]
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                base = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            found.add(module)
            found.update(f"{module}.{alias.name}" for alias in node.names)
    result = frozenset(
        module
        for module in found
        if module.split(".")[0] == _PACKAGE and _module_file(module) is not None
    )
    _module_imports[cache_key] = result
    return result


def module_closure(name: str) -> list[Path]:
    """Source files of ``name`` and of every package module it reaches."""

    seen: set[str] = set()
    pending = [name]
    files: dict[str, Path] = {}
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        path = _module_file(current)
        if path is None:
            continue
        files[current] = path
        # A module's parent packages run first on import.
        parent = current.rpartition(".")[0]
        if parent:
            pending.append(parent)
        pending.extend(_imports_of(current, path) - seen)
    return [files[key] for key in sorted(files)]


def _definition_dirs() -> set[Path]:
    """Directories whose files a compiled plan already pins."""

    found = {_PACKAGE_DIR / "definitions", _PACKAGE_DIR / "gui"}
    for manifest in _PACKAGE_DIR.glob("capabilities/*/capability.yaml"):
        try:
            import yaml

            data = yaml.safe_load(manifest.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        for value in data.values():
            if isinstance(value, str) and value.startswith("./"):
                found.add((manifest.parent / value).resolve())
    return found


def _digest_files(paths: Iterable[Path]) -> str:
    digest = hashlib.sha256()
    for path in paths:
        try:
            stat = path.stat()
        except OSError:
            continue
        cache_key = (str(path), stat.st_size, stat.st_mtime_ns)
        value = _file_digests.get(cache_key)
        if value is None:
            value = _file_sha(path)
            _file_digests[cache_key] = value
        relative = path.relative_to(_PACKAGE_DIR.parent).as_posix()
        digest.update(f"{relative}\0{value}\n".encode())
    return digest.hexdigest()


def package_data_digest() -> str:
    """The package's non-code files that are not definitions."""

    excluded = _definition_dirs()
    paths = [
        path
        for path in sorted(_PACKAGE_DIR.rglob("*"))
        if path.is_file()
        and path.suffix not in {".py", ".pyc"}
        and "__pycache__" not in path.parts
        and not path.name.startswith(".")
        and not any(parent in excluded for parent in path.resolve().parents)
    ]
    return _digest_files(paths)


def implementation_modules(implementation: Any) -> set[str]:
    """Modules of a Tool's implementation and of the callables it wraps.

    Implementations are often thin wrappers (a lambda, ``functools.partial``,
    a decorator) around the function that does the work; the walk follows
    ``__wrapped__``, partials, bound methods and closure cells.
    """

    modules: set[str] = set()
    seen: set[int] = set()
    pending = [implementation]
    while pending:
        target = pending.pop()
        if id(target) in seen or not callable(target):
            continue
        seen.add(id(target))
        if isinstance(target, partial):
            pending.append(target.func)
            continue
        wrapped = getattr(target, "__wrapped__", None)
        if wrapped is not None:
            pending.append(wrapped)
        function = getattr(target, "__func__", None)
        if function is not None:
            pending.append(function)
            owner = getattr(target, "__self__", None)
            if owner is not None:
                modules.add(type(owner).__module__)
            continue
        module = getattr(target, "__module__", None)
        if module is None:
            module = type(target).__module__
        modules.add(module)
        for cell in getattr(target, "__closure__", None) or ():
            try:
                pending.append(cell.cell_contents)
            except ValueError:
                continue
    return {module for module in modules if module}


def code_digest(implementation: Any) -> str:
    files: dict[Path, None] = {}
    for module in sorted(implementation_modules(implementation)):
        files.update(dict.fromkeys(module_closure(module)))
    return _digest_files(sorted(files)) if files else "unknown"


# -- memo ---------------------------------------------------------------------


def _plain(value: Any) -> Any:
    """JSON data of a value, the same whether it is a model or its dump.

    A step's input is a live model when the step before it ran, and plain
    JSON when that step was skipped; both must give the same key.
    """

    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, Mapping):
        return {str(key): _plain(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_plain(item) for item in value]
    return json.loads(canonical_tool_arguments(value))



class RunStepMemo:
    """Record each Tool step of one Run; replay them while nothing ran yet."""

    def __init__(
        self,
        workspace: Path,
        run_id: str,
        *,
        replay: bool,
        kept: Iterable[str] = (),
        frozen_inputs: Iterable[str] = (),
        source_roots: Iterable[Path] = (),
    ) -> None:
        self.workspace = Path(workspace).resolve()
        self.run_id = run_id
        self.run_dir = self.workspace / "Work" / "runs" / run_id
        self.root = self.run_dir / MEMO_DIR
        self.replaying = replay
        self._skip_in_run = RUNTIME_OWNED | frozenset(kept)
        self._lock = asyncio.Lock()
        self._sources = self._source_digest(source_roots, frozenset(frozen_inputs))
        self._data = package_data_digest()
        self.skipped = 0
        self.executed = 0
        # (step, seconds) of the steps this execution ran.
        self.timings: list[tuple[str, float]] = []

    # A key -------------------------------------------------------------------

    def _source_digest(self, roots: Iterable[Path], kept: frozenset[str]) -> str:
        digest = hashlib.sha256()
        for row in _listing(self.workspace, skip=_NOT_SOURCES):
            digest.update(repr(row).encode())
        for root in roots:
            for row in _listing(Path(root)):
                digest.update(repr((str(root), *row)).encode())
        # The Run's frozen inputs are sources too.  They may be frozen again
        # with the same content, so their content counts, not their times.
        for name in sorted(kept):
            entry = self.run_dir / name
            files = (
                [(name, entry)]
                if entry.is_file()
                else [
                    (f"{name}/{relative}", entry / relative)
                    for relative, _, _ in _listing(entry)
                ]
            )
            for relative, path in files:
                digest.update(relative.encode())
                digest.update(_sha(path.read_bytes()).encode())
        return digest.hexdigest()

    def key(
        self,
        *,
        task_id: str,
        definition: Any,
        implementation: Any,
        arguments: Any,
    ) -> str:
        definition_data = (
            definition.model_dump(mode="json") if hasattr(definition, "model_dump") else definition
        )
        payload = {
            "task": task_id,
            "definition": definition_data,
            "code": code_digest(implementation),
            "data": self._data,
            "sources": self._sources,
            "arguments": _plain(arguments),
        }
        return _sha(canonical_tool_arguments(payload).encode("utf-8"))

    # Files -------------------------------------------------------------------

    def _scan(self) -> dict[str, tuple[int, int]]:
        """Every file a step may write, with size and mtime."""

        files: dict[str, tuple[int, int]] = {}
        for dirpath, dirnames, filenames in os.walk(self.workspace):
            parts = Path(dirpath).relative_to(self.workspace).parts
            dirnames[:] = [
                name for name in sorted(dirnames) if self._may_hold_step_files((*parts, name))
            ]
            for name in sorted(filenames):
                if name.startswith(".") or not self._may_hold_step_files((*parts, name)):
                    continue
                path = Path(dirpath) / name
                try:
                    stat = path.stat()
                except OSError:
                    continue
                files["/".join((*parts, name))] = (stat.st_size, stat.st_mtime_ns)
        return files

    def _may_hold_step_files(self, parts: tuple[str, ...]) -> bool:
        name = parts[-1]
        if name.startswith(".") or name == "__pycache__" or name.endswith((".tmp", ".memo-tmp")):
            # Half-written files: the runtime's own atomic saves, and ours.
            return False
        if parts[0] == "logs":
            return False
        if parts[0] != "Work" or len(parts) < 2:
            return True
        if parts[1] in _RUNTIME_WORK:
            return False
        if parts[1] != "runs" or len(parts) < 3:
            return True
        if parts[2] != self.run_id:
            return False
        return len(parts) < 4 or parts[3] not in self._skip_in_run

    def _blob(self, digest: str) -> Path:
        return self.root / "blobs" / digest[:2] / digest

    def _record_path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def _save(self, key: str, outcome: ToolInvocationOutcome, before: Mapping, after: Mapping) -> None:
        writes: dict[str, str] = {}
        for relative, signature in after.items():
            if before.get(relative) == signature:
                continue
            path = self.workspace / relative
            digest = _file_sha(path)
            blob = self._blob(digest)
            if not blob.is_file():
                blob.parent.mkdir(parents=True, exist_ok=True)
                temporary = blob.with_suffix(".tmp")
                shutil.copyfile(path, temporary)
                os.replace(temporary, blob)
            writes[relative] = digest
        record = {
            "outcome": outcome.model_dump(mode="json"),
            "writes": writes,
            "removes": sorted(set(before) - set(after)),
        }
        path = self._record_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, path)

    def _restore(self, key: str) -> ToolInvocationOutcome | None:
        try:
            record = json.loads(self._record_path(key).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        writes: Mapping[str, str] = record.get("writes") or {}
        if not all(self._blob(digest).is_file() for digest in writes.values()):
            return None
        for relative, digest in writes.items():
            target = self.workspace / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + ".memo-tmp")
            shutil.copyfile(self._blob(digest), temporary)
            os.replace(temporary, target)
        for relative in record.get("removes") or ():
            (self.workspace / relative).unlink(missing_ok=True)
        return ToolInvocationOutcome.model_validate(record["outcome"]).model_copy(
            update={"reused": True}
        )

    def report(self) -> None:
        """Say how many steps this execution skipped and executed."""

        logger.info(
            "run {}: {} program steps skipped, {} executed",
            self.run_id,
            self.skipped,
            self.executed,
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            (self.root / "last-execution.json").write_text(
                json.dumps(
                    {
                        "skipped": self.skipped,
                        "executed": self.executed,
                        "steps": [[task, round(seconds, 2)] for task, seconds in self.timings],
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except OSError:
            pass

    # A step ------------------------------------------------------------------

    async def invoke(self, adapter: Any, arguments: Any, *, task_id: str) -> ToolInvocationOutcome:
        implementation = getattr(getattr(adapter, "tool", None), "_implementation", adapter)
        key = self.key(
            task_id=task_id,
            definition=getattr(adapter, "definition", None),
            implementation=implementation,
            arguments=arguments,
        )
        async with self._lock:
            if self.replaying:
                outcome = self._restore(key)
                if outcome is not None:
                    self.skipped += 1
                    return outcome
                logger.info(
                    "run {}: step {} changed; it and every later step run again",
                    self.run_id,
                    task_id,
                )
                self.replaying = False
            started = time.monotonic()
            before = self._scan()
            outcome = adapter.invoke(arguments, task_id=task_id)
            if inspect.isawaitable(outcome):
                outcome = await outcome
            self.executed += 1
            if isinstance(outcome, ToolInvocationOutcome) and outcome.status == "ok":
                try:
                    self._save(key, outcome, before, self._scan())
                except OSError:
                    logger.exception("run {}: step {} was not recorded", self.run_id, task_id)
            self.timings.append((task_id, time.monotonic() - started))
            return outcome


class MemoizedTool:
    """A Tool adapter whose steps go through a Run's step memo."""

    def __init__(self, adapter: Any, memo: RunStepMemo) -> None:
        self._adapter = adapter
        self._memo = memo
        self.definition = getattr(adapter, "definition", None)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._adapter, name)

    async def invoke(self, arguments: Any, *, task_id: str) -> ToolInvocationOutcome:
        return await self._memo.invoke(self._adapter, arguments, task_id=task_id)


__all__ = [
    "MEMO_DIR",
    "RUNTIME_OWNED",
    "MemoizedTool",
    "RunStepMemo",
    "code_digest",
    "implementation_modules",
    "module_closure",
    "package_data_digest",
]
