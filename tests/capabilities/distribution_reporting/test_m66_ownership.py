"""Characterization for the M6.6 Capability-owned source/runtime helpers."""

import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

MODULES = {
    "special_topics": (
        "manyselves.capabilities.distribution_reporting.runtime.intake.special_topics"
    ),
    "project_evidence": (
        "manyselves.capabilities.distribution_reporting.runtime.research.project_evidence"
    ),
    "photo_bindings": (
        "manyselves.capabilities.distribution_reporting.domain.photo_bindings"
    ),
}


def test_m66_helpers_import_without_core_reporting() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    *(
                        f"import {module_name} as {alias}"
                        for alias, module_name in MODULES.items()
                    ),
                    "print(json.dumps({",
                    "    'modules': sorted((",
                    *(f"        {alias}.__name__," for alias in MODULES),
                    "    )),",
                    "    'core_reporting': sorted(name for name in sys.modules if name.startswith('manyselves.core.reporting')),",
                    "}))",
                )
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload == {
        "modules": sorted(MODULES.values()),
        "core_reporting": [],
    }


def test_m66_symbols_are_physically_capability_owned() -> None:
    expected_symbols = {
        "special_topics": ("load_special_topic_plan",),
        "project_evidence": ("ProjectEvidenceIndex", "project_evidence_locator"),
        "photo_bindings": ("runtime_photo_ids",),
    }
    for alias, module_name in MODULES.items():
        module = import_module(module_name)
        for symbol_name in expected_symbols[alias]:
            assert getattr(module, symbol_name).__module__ == module_name

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
