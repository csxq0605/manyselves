"""Characterization for Capability-owned report asset assembly and validation."""

import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

ASSETS_MODULE = "manyselves.capabilities.distribution_reporting.runtime.assets"


def test_assets_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    f"import {ASSETS_MODULE} as assets",
                    "print(json.dumps({",
                    "    'module': assets.__name__,",
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
        "module": ASSETS_MODULE,
        "core_reporting": [],
    }


def test_assets_symbols_are_physically_capability_owned() -> None:
    assets = import_module(ASSETS_MODULE)

    for symbol_name in (
        "ReportAssetAssembler",
        "approved_module_marker",
        "expand_approved_module_markers",
        "validate_editor_protection",
        "validate_editor_quality",
        "validate_module_markdown_consistency",
        "validate_existing_markdown_modules",
        "validate_final_report_markdown",
        "validate_aggregate_retention",
    ):
        assert getattr(assets, symbol_name).__module__ == ASSETS_MODULE

    source = Path(assets.__file__).read_text(encoding="utf-8")
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
