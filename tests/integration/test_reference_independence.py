import json
import shutil
import subprocess
from importlib import resources
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
TEST_ROOT = (REPO_ROOT / ".test-projects" / "independence").resolve()


def fresh_project() -> Path:
    if TEST_ROOT.exists():
        shutil.rmtree(TEST_ROOT)
    project = TEST_ROOT / "customer"
    (project / "Inputs").mkdir(parents=True)
    (project / "Inputs" / "巡检记录.md").write_text(
        "主进线柜温度记录为 80°C。\n",
        encoding="utf-8",
    )
    return project


def test_packaged_resources_contain_agents_and_workflow() -> None:
    root = resources.files("pds_report.resources")

    assert root.joinpath("agents/manifest-builder.md").is_file()
    assert root.joinpath("agents/revision-router.md").is_file()
    assert root.joinpath("workflows/phase-a.yml").is_file()


def test_headless_entrypoint_runs_from_customer_project_directory() -> None:
    project = fresh_project()
    command = [
        str(REPO_ROOT / ".venv" / "bin" / "pds-report"),
        "--headless",
        "--project",
        str(project),
        "--message",
        "写作配电报告，要求深度思考，先做2.4",
    ]

    completed = subprocess.run(
        command,
        cwd=project,
        check=False,
        capture_output=True,
        text=True,
    )

    assert completed.returncode == 0, completed.stderr
    reply = json.loads(completed.stdout)
    assert reply["status"] == "completed"
    assert reply["files"]


def test_runtime_source_has_no_reference_repository_dependency() -> None:
    forbidden = (
        "../AutoReport",
        "../Nexgent",
        "Documents/Codex/AutoReport",
        "Documents/Codex/Nexgent",
        "import AutoReport",
        "import Nexgent",
    )

    for path in (REPO_ROOT / "src").rglob("*"):
        if path.suffix not in {".py", ".md", ".yml", ".yaml"}:
            continue
        text = path.read_text(encoding="utf-8")
        assert all(token not in text for token in forbidden), path
