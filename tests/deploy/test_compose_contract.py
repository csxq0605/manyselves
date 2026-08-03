from pathlib import Path

import yaml


def test_compose_has_one_api_replica_and_persistent_data() -> None:
    compose = yaml.safe_load(Path("deploy/compose.yaml").read_text("utf-8"))
    assert set(compose["services"]) == {"api", "web"}
    api = compose["services"]["api"]
    assert api["deploy"]["replicas"] == 1
    assert any("/data/manyselves" in volume for volume in api["volumes"])
    assert api["read_only"] is True
    assert api["environment"]["CONFIG_PATH"] == "/data/manyselves/manyselves.config.yaml"


def test_nginx_disables_sse_buffering_and_caches_safely() -> None:
    config = Path("deploy/nginx/default.conf").read_text("utf-8")
    assert "proxy_buffering off" in config
    assert "proxy_read_timeout 1h" in config
    assert 'location = /index.html' in config
    assert 'location = /api/v1/events' in config
