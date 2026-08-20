"""Keep the extracted kernel independent from application and capability code."""

import ast
from pathlib import Path

import manyselves.kernel

FORBIDDEN_IMPORT_PREFIXES = (
    "core.reporting",
    "webapi",
    "gui",
    "capabilities",
)


def _imported_modules(source_path: Path) -> list[str]:
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.append(node.module)
    return modules


def test_kernel_has_no_application_or_capability_imports() -> None:
    kernel_root = Path(manyselves.kernel.__file__).resolve().parent
    violations = {
        source_path.relative_to(kernel_root).as_posix(): module
        for source_path in sorted(kernel_root.rglob("*.py"))
        for module in _imported_modules(source_path)
        if (normalized := module.removeprefix("manyselves."))
        if any(
            normalized == prefix or normalized.startswith(f"{prefix}.")
            for prefix in FORBIDDEN_IMPORT_PREFIXES
        )
    }

    assert violations == {}
