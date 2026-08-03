"""Server, filesystem, deployment, and desktop trust-boundary locks."""

from pathlib import Path

import pytest

from manyselves.application.workspace_files import UnsafeWorkspacePath, WorkspaceFiles


@pytest.mark.parametrize(
    "untrusted",
    ["../secret", "/etc/passwd", "C:/Windows/System32", "Inputs\\secret", "%2e%2e/secret"],
)
def test_untrusted_workspace_paths_never_escape_project(tmp_path: Path, untrusted: str) -> None:
    root = tmp_path / "workspace"
    root.mkdir()
    files = WorkspaceFiles(root)
    with pytest.raises(UnsafeWorkspacePath):
        files.resolve(untrusted)


def test_compose_does_not_publish_api_or_persist_deployment_secrets() -> None:
    compose = Path("deploy/compose.yaml").read_text(encoding="utf-8")
    bootstrap = Path("deploy/api/init_config.py").read_text(encoding="utf-8")
    assert "127.0.0.1:8000" not in compose
    assert "api_key" not in bootstrap.casefold()
    assert "MANYSELVES_ACCESS_TOKEN" in compose
    assert "read_only: true" in compose


def test_electron_preload_exposes_only_reviewed_capabilities() -> None:
    preload = Path("desktop/src/preload.cjs").read_text(encoding="utf-8")
    main = Path("desktop/src/main.ts").read_text(encoding="utf-8")
    assert "contextBridge.exposeInMainWorld" in preload
    assert "ipcRenderer.send(" not in preload
    assert "requireTrustedSender" in main
    assert "nodeIntegration: false" in Path("desktop/src/security.ts").read_text(encoding="utf-8")
