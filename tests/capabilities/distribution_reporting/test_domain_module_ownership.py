import ast
import json
import subprocess
import sys
from importlib import import_module
from importlib.util import find_spec
from pathlib import Path

DOMAIN_ROOT = "manyselves.capabilities.distribution_reporting.domain"
DOMAIN_MODULES = {
    "claim_ledger": f"{DOMAIN_ROOT}.claim_ledger",
    "coverage": f"{DOMAIN_ROOT}.coverage",
}


def test_pure_domain_modules_import_without_core_reporting() -> None:
    imports = [
        f"import {module_name} as {alias}"
        for alias, module_name in DOMAIN_MODULES.items()
    ]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "\n".join(
                (
                    "import json",
                    "import sys",
                    *imports,
                    "print(json.dumps({",
                    "    'modules': sorted((",
                    *(
                        f"        {alias}.__name__,"
                        for alias in DOMAIN_MODULES
                    ),
                    "    )),",
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
        "modules": sorted(DOMAIN_MODULES.values()),
        "core_reporting": [],
    }


def test_pure_domain_symbols_are_physically_capability_owned() -> None:
    expected_symbols = {
        "claim_ledger": ("CitationBindingError", "ClaimLedger", "claim_citation_marker"),
        "coverage": ("evaluate_coverage",),
    }

    for alias, module_name in DOMAIN_MODULES.items():
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


