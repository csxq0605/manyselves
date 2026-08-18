from pathlib import Path

import yaml

from manyselves.webapi.settings import WebSettings


def test_compose_has_one_api_replica_and_persistent_data() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text("utf-8"))
    assert set(compose["services"]) == {"api", "web"}
    api = compose["services"]["api"]
    assert api["deploy"]["replicas"] == 1
    assert any("/data/manyselves" in volume for volume in api["volumes"])
    assert api["read_only"] is True
    assert api["environment"]["CONFIG_PATH"] == "${MANYSELVES_CONFIG_PATH:-/data/manyselves/manyselves.config.yaml}"
    assert api["environment"]["MANYSELVES_ADMIN_USERNAME"] == "${MANYSELVES_ADMIN_USERNAME:-admin}"
    assert api["environment"]["MANYSELVES_ADMIN_PASSWORD"] == "${MANYSELVES_ADMIN_PASSWORD:-yuanxi@2026}"
    assert "ACCESS_TOKEN" not in api["environment"]
    assert api["expose"] == ["9000"]
    assert "ports" not in api

    web = compose["services"]["web"]
    assert web["ports"] == [
        "${MANYSELVES_HTTP_BIND:-0.0.0.0}:${MANYSELVES_HTTP_PORT:-9090}:9090"
    ]


def test_nginx_disables_sse_buffering_and_caches_safely() -> None:
    config = Path("deploy/nginx/default.conf").read_text("utf-8")
    assert "proxy_buffering off" in config
    assert "proxy_read_timeout 1h" in config
    assert 'location = /index.html' in config
    assert 'location = /api/v1/events' in config
    assert "listen 9090" in config
    assert config.count("proxy_pass http://api:9000") == 2
    assert "proxy_set_header Authorization" not in config


def test_lan_defaults_use_the_reviewed_server_ip_and_ports() -> None:
    environment = Path("deploy/env.example").read_text("utf-8")
    compose = Path("deploy/compose.yaml").read_text("utf-8")
    web_dockerfile = Path("deploy/web/Dockerfile").read_text("utf-8")
    frontend_main = Path("frontend/src/main.tsx").read_text("utf-8")

    assert 'MANYSELVES_ALLOWED_ORIGINS=["http://192.168.8.28:9090"]' in environment
    assert "MANYSELVES_HTTP_BIND=0.0.0.0" in environment
    assert "MANYSELVES_HTTP_PORT=9090" in environment
    assert "MANYSELVES_ADMIN_USERNAME=admin" in environment
    assert "MANYSELVES_ADMIN_PASSWORD=yuanxi@2026" in environment
    assert "MANYSELVES_ACCESS_TOKEN" not in environment
    assert '["http://192.168.8.28:9090"]' in compose
    assert "EXPOSE 9090" in web_dockerfile
    assert 'defaultServerUrl: "http://192.168.8.28:9090"' in frontend_main


def test_deploy_env_example_uses_parseable_json_origins(monkeypatch) -> None:
    environment = Path("deploy/env.example").read_text("utf-8")
    origin_line = next(
        line for line in environment.splitlines()
        if line.startswith("MANYSELVES_ALLOWED_ORIGINS=")
    )

    monkeypatch.setenv("MANYSELVES_ALLOWED_ORIGINS", origin_line.split("=", 1)[1])

    assert WebSettings().allowed_origins == ["http://192.168.8.28:9090"]


def test_compose_maps_prefixed_provider_keys_to_runtime_environment() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text("utf-8"))
    environment = compose["services"]["api"]["environment"]

    assert environment["OPENAI_API_KEY"] == "${MANYSELVES_OPENAI_API_KEY:-}"
    assert environment["ANTHROPIC_API_KEY"] == "${MANYSELVES_ANTHROPIC_API_KEY:-}"
    assert environment["DEEPSEEK_API_KEY"] == "${MANYSELVES_DEEPSEEK_API_KEY:-}"
    assert environment["OPENROUTER_API_KEY"] == "${MANYSELVES_OPENROUTER_API_KEY:-}"


def test_cent_os_docs_cover_selinux_volume_labeling() -> None:
    docs = Path("docs/deployment/linux-compose.md").read_text("utf-8")

    assert "CentOS" in docs
    assert "SELinux" in docs
    assert ":Z" in docs
    assert "MANYSELVES_DATA_DIR=/srv/manyselves/data" in docs


def test_linux_deployment_docs_prepare_one_non_root_engine_context() -> None:
    docs = Path("docs/deployment/linux-compose.md").read_text("utf-8")
    rootless_docker = docs.split("If that privilege is not acceptable", 1)[1].split(
        "Keep DOCKER_HOST", 1
    )[0]

    assert "Choose exactly one engine path" in docs
    assert "sudo usermod -aG docker manyselves" in docs
    assert "docker group is equivalent to root-level control" in docs
    assert "dockerd-rootless-setuptool.sh install" in docs
    assert "loginctl enable-linger manyselves" in docs
    assert "XDG_RUNTIME_DIR=/run/user/$(id -u)" in docs
    assert "podman info" in docs
    assert "Run every extraction and Compose command below from that configured service-account session." in docs
    assert rootless_docker.index("export XDG_RUNTIME_DIR=/run/user/$(id -u)") < rootless_docker.index(
        'test -d "$XDG_RUNTIME_DIR"'
    )
    assert rootless_docker.index('test -d "$XDG_RUNTIME_DIR"') < rootless_docker.index(
        "systemctl --user is-active default.target"
    )
    assert rootless_docker.index(
        "systemctl --user is-active default.target"
    ) < rootless_docker.index("dockerd-rootless-setuptool.sh install")
    assert "docker context create manyselves-rootless" in rootless_docker
    assert "|| true" not in rootless_docker
