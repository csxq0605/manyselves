import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

PARALLEL_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.state.parallel"
)
STORAGE_MODULE = "manyselves.capabilities.distribution_reporting.runtime.storage"
SOURCE_LEDGER_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.source_ledger"
)


def test_state_storage_modules_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {PARALLEL_MODULE} as parallel",
                    f"import {STORAGE_MODULE} as storage",
                    f"import {SOURCE_LEDGER_MODULE} as source_ledger",
                    "print(json.dumps({",
                    "    'parallel': parallel.__name__,",
                    "    'storage': storage.__name__,",
                    "    'source_ledger': source_ledger.__name__,",
                    "    'core_reporting': sorted(",
                    "        name for name in sys.modules",
                    "        if name.startswith('manyselves.core.reporting')",
                    "    ),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "parallel": PARALLEL_MODULE,
        "storage": STORAGE_MODULE,
        "source_ledger": SOURCE_LEDGER_MODULE,
        "core_reporting": [],
    }


def test_state_storage_types_are_physically_capability_owned() -> None:
    parallel = import_module(PARALLEL_MODULE)
    storage = import_module(STORAGE_MODULE)
    source_ledger = import_module(SOURCE_LEDGER_MODULE)

    for type_name in (
        "ArtifactRef",
        "TaskCorrelation",
        "TaskAttemptStore",
        "LaneTaskSpec",
        "LaneCompletion",
        "StageState",
        "LaneState",
        "AggregateState",
        "RecoveryPlan",
        "RecoveryStateStore",
        "WorkflowReducer",
    ):
        assert getattr(parallel, type_name).__module__ == PARALLEL_MODULE
    assert storage.ReportingStore.__module__ == STORAGE_MODULE
    assert source_ledger.SourceLedger.__module__ == SOURCE_LEDGER_MODULE

    for compatibility_name in (
        "LaneAttempt",
        "LaneCompletionRecord",
        "LaneRecoveryStore",
        "RecoveryStore",
    ):
        assert compatibility_name not in vars(parallel)

    for module in (parallel, storage, source_ledger):
        source = Path(module.__file__).read_text(encoding="utf-8")
        imports = [
            node
            for node in ast.walk(ast.parse(source))
            if isinstance(node, (ast.Import, ast.ImportFrom))
        ]
        assert all(
            not any(
                name.name.startswith("manyselves.core.reporting")
                for name in node.names
            )
            if isinstance(node, ast.Import)
            else not (node.module or "").startswith("manyselves.core.reporting")
            for node in imports
        )

    assert find_spec("manyselves.core.reporting") is None
