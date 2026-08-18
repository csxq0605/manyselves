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


def test_build_local_script_uses_versioned_release_directory() -> None:
    script = Path("scripts/build-local.sh").read_text(encoding="utf-8")

    assert "VERSION_FILE" in script
    assert "--version" in script
    assert 'RELEASE_DIR="${OUTPUT_DIR}/manyselves-release-${VERSION}"' in script
    assert "manyselves-${VERSION}-linux-amd64-images.tar" in script


def test_build_local_script_uses_git_archive_for_source_package() -> None:
    script = Path("scripts/build-local.sh").read_text(encoding="utf-8")

    assert "git archive" in script
    assert "--prefix=\"manyselves-${VERSION}/\"" in script
    assert "tar -czf manyselves-deploy.tar.gz" not in script


def test_build_local_script_keeps_env_and_archives_outside_repo_root() -> None:
    script = Path("scripts/build-local.sh").read_text(encoding="utf-8")

    assert 'BUILD_ENV="${RELEASE_DIR}/manyselves-build.env"' in script
    assert "deploy/.env.build" not in script
    assert "rm -f manyselves-images.tar" not in script


def test_build_local_script_supports_optional_data_package() -> None:
    script = Path("scripts/build-local.sh").read_text(encoding="utf-8")

    assert "--with-data" in script
    assert "manyselves-${VERSION}-data.tar.gz" in script
    assert "tar -C deploy/data" in script


def test_build_local_script_builds_linux_amd64_images_and_checksum() -> None:
    script = Path("scripts/build-local.sh").read_text(encoding="utf-8")

    assert "DOCKER_DEFAULT_PLATFORM=linux/amd64 docker compose" in script
    assert "docker save -o \"$IMAGES_ARCHIVE\"" in script
    assert "sha256sum" in script


def test_centos_podman_script_supports_versioned_offline_release() -> None:
    script = Path("scripts/deploy-centos-podman.sh").read_text(encoding="utf-8")

    assert "--version" in script
    assert "--artifact-dir" in script
    assert "--install-dir" in script
    assert "--data-dir" in script
    assert "manyselves-${VERSION}-linux-amd64-images.tar" in script
    assert "manyselves-${VERSION}-source.tar.gz" in script
    assert "sha256sum -c" in script
    assert "podman load" in script
    assert "--no-build" in script


def test_centos_podman_script_handles_rootless_podman_volume_permissions() -> None:
    script = Path("scripts/deploy-centos-podman.sh").read_text(encoding="utf-8")

    assert "/etc/subuid" in script
    assert "/etc/subgid" in script
    assert "loginctl enable-linger" in script
    assert "XDG_RUNTIME_DIR" in script
    assert "podman unshare chown" in script
    assert "podman unshare chmod" in script
    assert "container_file_t" in script
    assert "chmod -R 777" not in script
    assert "chmod 666" not in script


def test_centos_podman_script_preserves_existing_env_secrets() -> None:
    script = Path("scripts/deploy-centos-podman.sh").read_text(encoding="utf-8")

    assert "[ ! -f \"$ENV_FILE\" ]" in script
    assert "cp \"$APP_DIR/deploy/env.example\" \"$ENV_FILE\"" in script
    assert "upsert_env" in script
    assert "MANYSELVES_OPENAI_API_KEY=" not in script
    assert "MANYSELVES_ANTHROPIC_API_KEY=" not in script


def test_legacy_centos_deploy_scripts_are_removed() -> None:
    assert not Path("scripts/deploy-server.sh").exists()
    assert not Path("scripts/fix-selinux.sh").exists()

    searched_files = [
        Path("deploy.sh"),
        Path("docs/DEPLOY.md"),
        Path("docs/QUICKSTART.md"),
        Path("docs/PODMAN_FIX.md"),
    ]
    for path in searched_files:
        if path.exists():
            text = path.read_text(encoding="utf-8")
            assert "scripts/deploy-server.sh" not in text
            assert "scripts/fix-selinux.sh" not in text
