from pathlib import Path


def test_root_start_script_changes_to_script_directory_before_delegating() -> None:
    script = Path("start.sh").read_text(encoding="utf-8")

    assert '\ncd "$(dirname "$0")"\n' in script


def test_start_script_default_env_heredoc_is_well_formed() -> None:
    script = Path("scripts/start.sh").read_text(encoding="utf-8")

    assert "cat > .env << EOF\n" in script
    assert "\nEOF\n    echo" in script


def test_start_script_accepts_windows_node_exe_in_wsl() -> None:
    script = Path("scripts/start.sh").read_text(encoding="utf-8")

    assert "command -v node.exe" in script
    assert "NODE_BIN" in script
    assert '"$NODE_BIN" --version' in script


def test_start_script_checks_npm_before_frontend_build() -> None:
    script = Path("scripts/start.sh").read_text(encoding="utf-8")

    assert "NPM_BIN" in script
    assert "command -v npm" in script
    assert '"$NPM_BIN" run build' in script


def test_start_script_does_not_query_python_package_updates_on_every_run() -> None:
    script = Path("scripts/start.sh").read_text(encoding="utf-8")

    assert "pip list --outdated" not in script


def test_frontend_start_script_accepts_windows_node_exe_in_wsl() -> None:
    script = Path("scripts/start-frontend.sh").read_text(encoding="utf-8")

    assert "command -v node.exe" in script
    assert "NODE_BIN" in script
    assert '"$NODE_BIN" --version' in script


def test_frontend_start_script_uses_detected_npm() -> None:
    script = Path("scripts/start-frontend.sh").read_text(encoding="utf-8")

    assert "NPM_BIN" in script
    assert "command -v npm" in script
    assert '"$NPM_BIN" run dev' in script
