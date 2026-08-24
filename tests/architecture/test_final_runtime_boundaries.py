"""Characterize the final definition-driven runtime ownership boundaries.

These tests intentionally describe the target architecture rather than the
temporary compatibility layout.  Each assertion isolates one migration seam
so production code can move toward the final shape one boundary at a time.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
PACKAGE_ROOT = REPOSITORY_ROOT / "manyselves"
REPORTING_CAPABILITY_ROOT = PACKAGE_ROOT / "capabilities" / "distribution_reporting"

GENERIC_LAYER_ROOTS = (
    PACKAGE_ROOT / "runtime",
    PACKAGE_ROOT / "application",
    PACKAGE_ROOT / "webapi",
)
REPORTING_DOMAIN_PREFIX = "manyselves.core.reporting"

REPORTING_ENTRYPOINTS = {
    "full-report",
    "module-report",
    "aggregate-existing",
    "render-existing",
    "distill-template-skill",
}


FINAL_ARCHITECTURE_GAP = pytest.mark.xfail(
    strict=True,
    reason="final Capability Runtime extraction is still in progress",
)


def _source_module(source_path: Path) -> tuple[str, list[str]]:
    relative = source_path.relative_to(REPOSITORY_ROOT).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        module_parts = parts[:-1]
        package_parts = module_parts
    else:
        module_parts = parts
        package_parts = parts[:-1]
    return ".".join(module_parts), package_parts


def _read_python_source(source_path: Path) -> str:
    return source_path.read_text(encoding="utf-8-sig")


def _resolved_imports(source_path: Path) -> list[str]:
    """Return absolute import targets, including targets written relatively."""

    tree = ast.parse(_read_python_source(source_path), filename=str(source_path))
    _module, package_parts = _source_module(source_path)
    imports: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
            continue
        if not isinstance(node, ast.ImportFrom):
            continue
        if node.level:
            ascend = node.level - 1
            base = package_parts[: len(package_parts) - ascend]
            suffix = node.module.split(".") if node.module else []
            imports.append(".".join([*base, *suffix]))
        elif node.module:
            imports.append(node.module)
    return imports


def _python_sources(root: Path) -> list[Path]:
    return sorted(path for path in root.rglob("*.py") if "__pycache__" not in path.parts)


def _reporting_domain_import_violations(root: Path) -> dict[str, list[str]]:
    violations: dict[str, list[str]] = {}
    for source_path in _python_sources(root):
        forbidden = sorted(
            {
                module
                for module in _resolved_imports(source_path)
                if module == REPORTING_DOMAIN_PREFIX
                or module.startswith(f"{REPORTING_DOMAIN_PREFIX}.")
            }
        )
        if forbidden:
            violations[source_path.relative_to(REPOSITORY_ROOT).as_posix()] = forbidden
    return violations


def test_generic_runtime_application_and_webapi_do_not_import_reporting_domain() -> None:
    """Generic infrastructure must not acquire a Distribution Reporting identity."""

    violations = {
        relative_path: imports
        for root in GENERIC_LAYER_ROOTS
        for relative_path, imports in _reporting_domain_import_violations(root).items()
    }

    assert violations == {}


def test_distribution_runtime_binding_does_not_depend_on_legacy_reporting_host() -> None:
    """The Capability binding must own its domain runtime, not borrow the old facade."""

    source_path = REPORTING_CAPABILITY_ROOT / "adapters" / "runtime.py"
    imports = _resolved_imports(source_path)
    forbidden_imports = sorted(
        module
        for module in imports
        if module == "manyselves.application.reporting_facade"
        or module.startswith("manyselves.application.reporting_facade.")
        or module == REPORTING_DOMAIN_PREFIX
        or module.startswith(f"{REPORTING_DOMAIN_PREFIX}.")
    )

    tree = ast.parse(_read_python_source(source_path), filename=str(source_path))
    legacy_constructor_arguments = sorted(
        {
            argument.arg
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name in {"__init__", "build_runtime_binding"}
            for argument in [*node.args.args, *node.args.kwonlyargs]
            if argument.arg in {"host", "reporting_adapter"}
        }
    )

    assert forbidden_imports == []
    assert legacy_constructor_arguments == []


def test_webapi_does_not_mount_or_construct_legacy_reporting_facade() -> None:
    """Generic Workflow HTTP is the only production reporting run boundary."""

    main_source = _read_python_source(PACKAGE_ROOT / "webapi" / "main.py")
    lifespan_source = _read_python_source(PACKAGE_ROOT / "webapi" / "lifespan.py")
    tenant_source = _read_python_source(
        PACKAGE_ROOT / "webapi" / "tenant_runtime.py"
    )

    assert "reporting_router" not in main_source
    assert "ReportingFacade" not in lifespan_source
    assert "ReportingFacade" not in tenant_source
    assert not (PACKAGE_ROOT / "application" / "reporting_facade.py").exists()
    assert not (PACKAGE_ROOT / "webapi" / "routes" / "reporting.py").exists()
    assert not (PACKAGE_ROOT / "webapi" / "schemas" / "reporting.py").exists()


def test_main_agent_does_not_expose_legacy_reporting_orchestration_tools() -> None:
    """Conversation runtime must not bypass file workflows through Core Reporting."""

    manager_source = _read_python_source(PACKAGE_ROOT / "core" / "loops" / "manager.py")
    loop_source = _read_python_source(PACKAGE_ROOT / "core" / "loops" / "agent_loop.py")
    prompt_source = (PACKAGE_ROOT / "templates" / "agents" / "main_agent.md").read_text(
        encoding="utf-8"
    )

    for legacy_tool in (
        "run_reporting_workflow",
        "resume_reporting_workflow",
        "revise_reporting_workflow",
        "cancel_reporting_workflow",
        "get_reporting_workflow_status",
    ):
        assert legacy_tool not in manager_source
        assert legacy_tool not in loop_source
        assert legacy_tool not in prompt_source
    assert not (PACKAGE_ROOT / "core" / "tools" / "reporting_tool.py").exists()


def test_distribution_contract_models_are_owned_by_the_capability() -> None:
    """File contracts must resolve into the Capability package, never core.reporting."""

    violations: dict[str, str] = {}
    for contract_path in sorted((REPORTING_CAPABILITY_ROOT / "contracts").glob("*.yaml")):
        payload = yaml.safe_load(contract_path.read_text(encoding="utf-8"))
        model = payload.get("model") if isinstance(payload, dict) else None
        if isinstance(model, str) and (
            model == REPORTING_DOMAIN_PREFIX
            or model.startswith(f"{REPORTING_DOMAIN_PREFIX}.")
        ):
            violations[contract_path.name] = model

    assert violations == {}


@FINAL_ARCHITECTURE_GAP
def test_declarative_reporting_runner_does_not_inherit_legacy_runner() -> None:
    """Declarative execution must compose Capability services directly."""

    violations: dict[str, list[str]] = {}
    for source_path in _python_sources(PACKAGE_ROOT):
        tree = ast.parse(_read_python_source(source_path), filename=str(source_path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            if node.name != "DeclarativeReportWorkflowRunner":
                continue
            bases = [ast.unparse(base) for base in node.bases]
            if any(base.rsplit(".", maxsplit=1)[-1] == "ReportWorkflowRunner" for base in bases):
                violations[source_path.relative_to(REPOSITORY_ROOT).as_posix()] = bases

    assert violations == {}


def test_all_reporting_operations_are_file_declared_workflow_entrypoints() -> None:
    """The five public report operations must start compiled file workflows."""

    capability_path = REPORTING_CAPABILITY_ROOT / "capability.yaml"
    capability = yaml.safe_load(capability_path.read_text(encoding="utf-8"))
    declared_entrypoints = set(capability["entrypoints"])
    workflow_ids = {
        payload["id"]
        for workflow_path in sorted((REPORTING_CAPABILITY_ROOT / "workflows").glob("*.yaml"))
        for payload in [yaml.safe_load(workflow_path.read_text(encoding="utf-8"))]
        if isinstance(payload, dict) and isinstance(payload.get("id"), str)
    }

    assert REPORTING_ENTRYPOINTS <= declared_entrypoints
    assert declared_entrypoints <= workflow_ids
