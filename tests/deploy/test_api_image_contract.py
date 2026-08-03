from pathlib import Path


def test_entrypoint_enforces_one_worker() -> None:
    script = Path("deploy/api/entrypoint.sh").read_text("utf-8")
    assert "--workers 1" in script
    assert "uvicorn_worker.UvicornWorker" in script
    assert "WORKERS" not in script
    assert "--no-control-socket" in script


def test_dockerfile_uses_non_root_user_and_healthcheck() -> None:
    dockerfile = Path("deploy/api/Dockerfile").read_text("utf-8")
    assert "USER manyselves" in dockerfile
    assert "HEALTHCHECK" in dockerfile
    assert "python:3.12-slim" in dockerfile


def test_initial_config_contains_no_provider_secret() -> None:
    script = Path("deploy/api/init_config.py").read_text("utf-8")
    assert '"apiKey": None' in script
    assert "OPENAI_API_KEY" not in script
    assert "if not config_path.exists()" in script
