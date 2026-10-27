"""Child processes must not outlive the call that started them."""

import asyncio
import os
import sys
from pathlib import Path

import pytest

from manyselves.runtime.subprocesses import run_captured, safe_child_env
from manyselves.runtime.tools.exec_tools import ExecTool
from manyselves.runtime.tools.pdf_tool import PDFParseTool

pytestmark = pytest.mark.skipif(os.name != "posix", reason="process groups are POSIX")

# The child spawns a grandchild; both write a file after a delay.
LATE_WRITER = (
    "import pathlib, subprocess, sys, time\n"
    "subprocess.Popen([sys.executable, '-c', "
    "\"import pathlib, time; time.sleep(1.2); pathlib.Path('grandchild.txt').write_text('x')\"])\n"
    "time.sleep(1.2)\n"
    "pathlib.Path('child.txt').write_text('x')\n"
)


def _script(tmp_path: Path) -> Path:
    path = tmp_path / "late.py"
    path.write_text(LATE_WRITER, encoding="utf-8")
    return path


async def _assert_nothing_written_later(tmp_path: Path) -> None:
    await asyncio.sleep(2.0)
    assert not (tmp_path / "child.txt").exists()
    assert not (tmp_path / "grandchild.txt").exists()


@pytest.mark.asyncio
async def test_cancel_kills_child_and_grandchild(tmp_path: Path) -> None:
    task = asyncio.create_task(
        run_captured([sys.executable, str(_script(tmp_path))], cwd=tmp_path)
    )
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_nothing_written_later(tmp_path)


@pytest.mark.asyncio
async def test_timeout_kills_child_and_grandchild(tmp_path: Path) -> None:
    run = await run_captured(
        [sys.executable, str(_script(tmp_path))], cwd=tmp_path, timeout=0.4
    )
    assert run.timed_out is True
    await _assert_nothing_written_later(tmp_path)


@pytest.mark.asyncio
async def test_exec_tool_cancel_stops_the_command(tmp_path: Path) -> None:
    _script(tmp_path)
    tool = ExecTool(working_dir=tmp_path)
    task = asyncio.create_task(
        tool(command="python3 late.py", command_description="Write late")
    )
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_nothing_written_later(tmp_path)


@pytest.mark.asyncio
async def test_pdf_parse_cancel_stops_mineru(tmp_path: Path, monkeypatch) -> None:
    fake_cli = tmp_path / "fake-mineru"
    fake_cli.write_text(
        f"#!{sys.executable}\n" + LATE_WRITER.replace("pathlib.Path('", f"pathlib.Path('{tmp_path}/"),
        encoding="utf-8",
    )
    fake_cli.chmod(0o755)
    (tmp_path / "References").mkdir()
    (tmp_path / "References" / "a.pdf").write_bytes(b"%PDF-1.4")
    tool = PDFParseTool(workspace=tmp_path)
    tool._cli_name = str(fake_cli)  # noqa: SLF001
    monkeypatch.setattr(PDFParseTool, "is_available", lambda self: True)

    task = asyncio.create_task(tool(file_paths="References/a.pdf"))
    await asyncio.sleep(0.4)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await _assert_nothing_written_later(tmp_path)


def test_safe_child_env_drops_secrets(monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "secret")
    monkeypatch.setenv("MANYSELVES_ACCOUNT_ADMIN_PASSWORD", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setenv("LC_ALL", "C.UTF-8")

    env = safe_child_env()

    assert "OPENAI_API_KEY" not in env
    assert "MANYSELVES_ACCOUNT_ADMIN_PASSWORD" not in env
    assert env["PATH"] == "/usr/bin"
    assert env["LC_ALL"] == "C.UTF-8"


@pytest.mark.asyncio
async def test_exec_tool_never_passes_service_secrets(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "service-key-must-not-leak")
    tool = ExecTool(working_dir=tmp_path)

    result = await tool(
        command="python3 -c \"import os; print(os.environ.get('OPENAI_API_KEY'))\"",
        command_description="Print a variable",
    )

    assert result["returncode"] == 0
    assert result["stdout"].strip() == "None"
