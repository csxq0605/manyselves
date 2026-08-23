"""Ownership characterization for Cross workflow Definition specialization."""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from importlib import import_module
from pathlib import Path

MODULE_NAME = (
    "manyselves.capabilities.distribution_reporting.runtime.cross_owner_definitions"
)


def test_cross_owner_definition_module_imports_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import json, sys\n"
                f"import {MODULE_NAME} as definitions\n"
                "print(json.dumps({\n"
                "    'module': definitions.__name__,\n"
                "    'core_reporting': sorted(name for name in sys.modules "
                "if name.startswith('manyselves.core.reporting')),\n"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(completed.stdout) == {
        "module": MODULE_NAME,
        "core_reporting": [],
    }


def test_cross_owner_definition_module_has_no_core_imports() -> None:
    module = import_module(MODULE_NAME)
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
    assert module.register_cross_owner_pipeline_specializations.__module__ == MODULE_NAME
