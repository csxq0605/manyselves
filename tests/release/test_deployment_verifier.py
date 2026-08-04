from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

import httpx
import pytest

from scripts.verify_deployment import verify_deployment


@dataclass
class FakeResponse:
    status_code: int = 200
    body: Any = field(default_factory=dict)
    headers: dict[str, str] = field(default_factory=dict)
    content: bytes = b""

    def json(self) -> Any:
        return self.body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeDeployment:
    def __init__(self) -> None:
        self.temporary = b""
        self.deleted = False
        self.released = False
        self.logged_out = False
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
        self.requests.append((method, path, kwargs))
        if path.endswith("/auth/login"):
            return FakeResponse(204)
        if path.endswith("/auth/logout"):
            self.logged_out = True
            return FakeResponse(204)
        if path.endswith("/health/live"):
            return FakeResponse(body={"status": "live"})
        if path.endswith("/health/ready"):
            return FakeResponse(body={"status": "ready"})
        if path.endswith("/bootstrap"):
            return FakeResponse(body={
                "project": {"id": "project-1"}, "runtime": {"ready": True}, "streamId": "stream-1",
            })
        if path.endswith("/projects"):
            return FakeResponse(body={"projects": [{"id": "project-1", "active": True}]})
        if path.endswith("/conversations"):
            return FakeResponse(body={"activeSessionId": None, "conversations": []})
        if path.endswith("/control/lease") and method == "POST":
            return FakeResponse(201, {"leaseToken": "lease-1"})
        if path.endswith("/control/lease") and method == "DELETE":
            self.released = True
            return FakeResponse(204)
        if path.endswith("/files/entries") and method == "POST":
            self.temporary = kwargs["json"]["content"].encode()
            return FakeResponse(201, {"revision": "revision-1"})
        if path.endswith("/files/content"):
            return FakeResponse(body={"content": self.temporary.decode()})
        if path.endswith("/files/download"):
            return FakeResponse(content=self.temporary)
        if path.endswith("/files/entries") and method == "DELETE":
            self.deleted = True
            return FakeResponse(204)
        raise AssertionError((method, path, kwargs))

    def stream(self, method: str, path: str, **kwargs: Any):
        assert (method, path) == ("GET", "/api/v1/events")
        return nullcontext(FakeResponse(headers={"content-type": "text/event-stream"}))

    def close(self) -> None:
        raise AssertionError("injected clients are not owned by the verifier")


def test_deployment_verifier_logs_in_before_protected_checks() -> None:
    deployment = FakeDeployment()
    result = verify_deployment(
        "https://pilot.example.internal",
        "admin",
        "yuanxi@2026",
        client=deployment,
    )
    assert result.checks == {
        "live": True, "ready": True, "single_runtime": True, "project": True,
        "conversation": True, "file_round_trip": True, "sse": True, "artifact_download": True,
    }
    assert result.passed
    assert deployment.deleted
    assert deployment.released
    assert deployment.logged_out
    assert deployment.requests[0] == (
        "POST",
        "/api/v1/auth/login",
        {"json": {"username": "admin", "password": "yuanxi@2026"}},
    )
    assert deployment.requests[1][1] == "/api/v1/health/live"
    assert "Authorization" not in deployment.requests[1][2].get("headers", {})


def test_deployment_verifier_reuses_login_cookie_for_protected_checks() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        method = request.method
        path = request.url.path
        if path == "/api/v1/auth/login":
            return httpx.Response(204, headers={"set-cookie": "manyselves_session=test; Path=/; HttpOnly"})
        if path == "/api/v1/auth/logout":
            return httpx.Response(204)
        if path in {"/api/v1/health/live", "/api/v1/health/ready"}:
            return httpx.Response(200, json={"status": "live" if path.endswith("live") else "ready"})
        assert request.headers.get("cookie") == "manyselves_session=test"
        if path == "/api/v1/bootstrap":
            return httpx.Response(200, json={
                "project": {"id": "project-1"}, "runtime": {"ready": True}, "streamId": "stream-1",
            })
        if path == "/api/v1/projects" and method == "GET":
            return httpx.Response(200, json={"projects": [{"id": "project-1", "active": True}]})
        if path == "/api/v1/conversations":
            return httpx.Response(200, json={"activeSessionId": None, "conversations": []})
        if path == "/api/v1/control/lease" and method == "POST":
            return httpx.Response(201, json={"leaseToken": "lease-1"})
        if path == "/api/v1/projects/project-1/files/entries" and method == "POST":
            return httpx.Response(201, json={"revision": "revision-1"})
        if path == "/api/v1/projects/project-1/files/content":
            return httpx.Response(200, json={"content": "manyselves phase1 deployment verifier\n"})
        if path == "/api/v1/projects/project-1/files/download":
            return httpx.Response(200, content=b"manyselves phase1 deployment verifier\n")
        if path == "/api/v1/events":
            return httpx.Response(200, headers={"content-type": "text/event-stream"})
        if path == "/api/v1/projects/project-1/files/entries" and method == "DELETE":
            return httpx.Response(204)
        if path == "/api/v1/control/lease" and method == "DELETE":
            return httpx.Response(204)
        raise AssertionError((method, path))

    client = httpx.Client(base_url="https://pilot.example.internal", transport=httpx.MockTransport(handler))
    try:
        assert verify_deployment("https://pilot.example.internal", "admin", "password", client=client).passed
    finally:
        client.close()

    protected = [request for request in requests if request.url.path == "/api/v1/bootstrap"]
    assert len(protected) == 1


def test_deployment_verifier_cleans_temp_file_lease_and_session_after_check_failure() -> None:
    class FailingDeployment(FakeDeployment):
        def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
            if path.endswith("/files/content"):
                self.requests.append((method, path, kwargs))
                return FakeResponse(status_code=500)
            return super().request(method, path, **kwargs)

    deployment = FailingDeployment()
    with pytest.raises(RuntimeError, match="HTTP 500"):
        verify_deployment("https://pilot.example.internal", "admin", "password", client=deployment)

    assert deployment.deleted
    assert deployment.released
    assert deployment.logged_out
    assert [(method, path) for method, path, _ in deployment.requests[-3:]] == [
        ("DELETE", "/api/v1/projects/project-1/files/entries"),
        ("DELETE", "/api/v1/control/lease"),
        ("POST", "/api/v1/auth/logout"),
    ]
    temporary_cleanup = deployment.requests[-3][2]
    assert temporary_cleanup["params"]["path"].startswith("Work/.phase1-deployment-verifier-")
    assert temporary_cleanup["headers"] == {
        "X-Control-Lease-Token": "lease-1", "If-Match": '"revision-1"',
    }
    assert deployment.requests[-2][2] == {"json": {"leaseToken": "lease-1"}}


def test_deployment_verifier_does_not_logout_when_login_fails() -> None:
    class RejectingLogin:
        def __init__(self) -> None:
            self.requests: list[tuple[str, str, dict[str, Any]]] = []

        def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
            self.requests.append((method, path, kwargs))
            return FakeResponse(status_code=401)

        def stream(self, method: str, path: str, **kwargs: Any):
            raise AssertionError("the verifier must stop after a failed login")

        def close(self) -> None:
            raise AssertionError("injected clients are not owned by the verifier")

    deployment = RejectingLogin()
    with pytest.raises(RuntimeError, match="HTTP 401"):
        verify_deployment("https://pilot.example.internal", "admin", "wrong", client=deployment)

    assert deployment.requests == [
        ("POST", "/api/v1/auth/login", {"json": {"username": "admin", "password": "wrong"}}),
    ]


def test_posix_backup_encodes_credentials_without_putting_password_in_curl_arguments() -> None:
    script = Path("deploy/backup/backup.sh").read_text("utf-8")

    assert "--data-binary @-" in script
    assert "json.dumps" in script
    assert '"password":"$ADMIN_PASSWORD"' not in script


def test_posix_backup_login_json_preserves_special_character_credentials() -> None:
    script = Path("deploy/backup/backup.sh").read_text("utf-8")
    match = re.search(r"login_json\(\).*?python -c '([^']+)'", script, re.DOTALL)
    assert match is not None

    environment = os.environ | {
        "ADMIN_USERNAME": 'admin"\\\noperator',
        "ADMIN_PASSWORD": 'quoted"\\\npassword',
    }
    output = subprocess.run(
        [sys.executable, "-c", match.group(1)],
        check=True,
        capture_output=True,
        env=environment,
        text=True,
    ).stdout

    assert json.loads(output) == {
        "username": 'admin"\\\noperator',
        "password": 'quoted"\\\npassword',
    }


def test_backup_clients_preserve_session_maintenance_lease_and_logout_contract() -> None:
    shell = Path("deploy/backup/backup.sh").read_text("utf-8")
    powershell = Path("deploy/backup/backup.ps1").read_text("utf-8")

    shell_login_flow = shell.split('if [ -n "$API_URL" ]; then', 1)[1]
    assert [shell_login_flow.index(endpoint) for endpoint in (
        "/api/v1/auth/login",
        "/api/v1/control/lease",
        "/api/v1/maintenance/quiesce",
    )] == sorted(shell_login_flow.index(endpoint) for endpoint in (
        "/api/v1/auth/login",
        "/api/v1/control/lease",
        "/api/v1/maintenance/quiesce",
    ))
    shell_cleanup = shell.split("cleanup() {", 1)[1].split("}\ntrap cleanup", 1)[0]
    assert [shell_cleanup.index(endpoint) for endpoint in (
        "/api/v1/maintenance/release",
        "/api/v1/control/lease",
        "/api/v1/auth/logout",
    )] == sorted(shell_cleanup.index(endpoint) for endpoint in (
        "/api/v1/maintenance/release",
        "/api/v1/control/lease",
        "/api/v1/auth/logout",
    ))

    powershell_login_flow = powershell.split("try {", 1)[1].split("} finally {", 1)[0]
    assert [powershell_login_flow.index(endpoint) for endpoint in (
        "/api/v1/auth/login",
        "/api/v1/control/lease",
        "/api/v1/maintenance/quiesce",
    )] == sorted(powershell_login_flow.index(endpoint) for endpoint in (
        "/api/v1/auth/login",
        "/api/v1/control/lease",
        "/api/v1/maintenance/quiesce",
    ))
    powershell_cleanup = powershell.split("} finally {", 1)[1]
    assert [powershell_cleanup.index(endpoint) for endpoint in (
        "/api/v1/maintenance/release",
        "/api/v1/control/lease",
        "/api/v1/auth/logout",
    )] == sorted(powershell_cleanup.index(endpoint) for endpoint in (
        "/api/v1/maintenance/release",
        "/api/v1/control/lease",
        "/api/v1/auth/logout",
    ))

    assert "--cookie-jar \"$COOKIE_JAR\"" in shell
    assert "--cookie \"$COOKIE_JAR\"" in shell
    assert "-WebSession $session" in powershell


def test_linux_compose_docs_create_privileged_paths_then_switch_to_service_account() -> None:
    docs = Path("docs/deployment/linux-compose.md").read_text("utf-8")

    assert "sudo install -d -m 0750 -o manyselves -g manyselves /opt/manyselves" in docs
    assert "sudo install -d -m 0700 -o manyselves -g manyselves /srv/manyselves/data /srv/manyselves/backups" in docs
    assert "sudo -iu manyselves" in docs
    assert "Run every extraction and Compose command below from that configured service-account session." in docs
