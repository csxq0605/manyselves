"""Characterize Capability ownership of reporting artifact access compilation."""

import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

ARTIFACT_ACCESS_MODULE = (
    "manyselves.capabilities.distribution_reporting.runtime.artifact_access"
)


def test_artifact_access_imports_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {ARTIFACT_ACCESS_MODULE} as module",
                    "print(json.dumps({",
                    "    'module': module.__name__,",
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
        "module": ARTIFACT_ACCESS_MODULE,
        "core_reporting": [],
    }


def test_artifact_access_symbols_are_physically_capability_owned() -> None:
    module = import_module(ARTIFACT_ACCESS_MODULE)
    for symbol_name in (
        "Capability",
        "CompiledAgentAccess",
        "collect_artifact_refs",
        "collect_photo_ids",
        "collect_reference_refs",
        "collect_task_refs",
        "compile_agent_access",
        "scoped_gateway",
    ):
        assert getattr(module, symbol_name).__module__ == ARTIFACT_ACCESS_MODULE

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


def test_configuration_error_remains_a_value_error() -> None:
    from manyselves.capabilities.distribution_reporting.runtime.artifact_access import (
        ConfigurationError as CapabilityConfigurationError,
    )

    assert issubclass(CapabilityConfigurationError, ValueError)
