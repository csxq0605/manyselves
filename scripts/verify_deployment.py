"""Verify a running single-runtime Phase 1 deployment through public HTTP APIs."""

from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict, dataclass
from types import TracebackType
from typing import Any, Protocol
from uuid import uuid4

import httpx


class Response(Protocol):
    status_code: int
    headers: Any
    content: bytes

    def json(self) -> Any: ...
    def raise_for_status(self) -> None: ...


class StreamContext(Protocol):
    def __enter__(self) -> Response: ...
    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> bool | None: ...


class Client(Protocol):
    def request(self, method: str, path: str, **kwargs: Any) -> Response: ...
    def stream(self, method: str, path: str, **kwargs: Any) -> StreamContext: ...
    def close(self) -> None: ...


@dataclass(frozen=True)
class VerificationResult:
    checks: dict[str, bool]
    details: dict[str, str]

    @property
    def passed(self) -> bool:
        return all(self.checks.values())


def _checked(response: Response, expected: int = 200) -> Response:
    if response.status_code != expected:
        response.raise_for_status()
        raise RuntimeError(f"Expected HTTP {expected}, received {response.status_code}")
    return response


def verify_deployment(
    url: str,
    username: str,
    password: str,
    *,
    client: Client | None = None,
) -> VerificationResult:
    """Run a reversible public-API smoke drill and return every named Gate E check."""
    if not username or not password:
        raise ValueError("Deployment administrator username and password are required")
    owned_client = client is None
    if client is None:
        client = httpx.Client(
            base_url=url.rstrip("/"),
            timeout=httpx.Timeout(10, read=10),
        )
    checks = {name: False for name in (
        "live", "ready", "single_runtime", "project", "conversation",
        "file_round_trip", "sse", "artifact_download",
    )}
    details: dict[str, str] = {}
    lease_token: str | None = None
    project_id: str | None = None
    path: str | None = None
    revision: str | None = None
    logged_in = False
    try:
        _checked(client.request(
            "POST",
            "/api/v1/auth/login",
            json={"username": username, "password": password},
        ), 204)
        logged_in = True
        live = _checked(client.request("GET", "/api/v1/health/live")).json()
        checks["live"] = live.get("status") == "live"
        ready = _checked(client.request("GET", "/api/v1/health/ready")).json()
        checks["ready"] = ready.get("status") == "ready"

        bootstrap = _checked(client.request("GET", "/api/v1/bootstrap")).json()
        runtime = bootstrap.get("runtime", {})
        checks["single_runtime"] = bool(runtime.get("ready") and bootstrap.get("streamId"))
        project_id = bootstrap.get("project", {}).get("id")

        projects = _checked(client.request("GET", "/api/v1/projects")).json().get("projects", [])
        checks["project"] = bool(project_id) and any(item.get("id") == project_id for item in projects)
        conversations = _checked(client.request("GET", "/api/v1/conversations")).json()
        checks["conversation"] = "conversations" in conversations and "activeSessionId" in conversations

        lease = _checked(client.request(
            "POST", "/api/v1/control/lease", json={"clientId": "phase1-deployment-verifier"},
        ), 201).json()
        lease_token = lease["leaseToken"]
        mutation_headers = {"X-Control-Lease-Token": lease_token}
        path = f"Inputs/.phase1-deployment-verifier-{uuid4().hex}.txt"
        payload = b"manyselves phase1 deployment verifier\n"
        created = _checked(client.request(
            "POST",
            f"/api/v1/projects/{project_id}/files/entries",
            headers=mutation_headers,
            json={"path": path, "kind": "file", "content": payload.decode()},
        ), 201).json()
        revision = created["revision"]
        content = _checked(client.request(
            "GET", f"/api/v1/projects/{project_id}/files/content", params={"path": path},
        )).json()
        checks["file_round_trip"] = content.get("content", "").encode() == payload
        artifact = _checked(client.request(
            "GET", f"/api/v1/projects/{project_id}/files/download", params={"path": path},
        ))
        checks["artifact_download"] = artifact.content == payload

        with client.stream("GET", "/api/v1/events") as stream:
            checks["sse"] = stream.status_code == 200 and (
                "text/event-stream" in stream.headers.get("content-type", "")
            )
        details["projectId"] = str(project_id)
        details["streamId"] = str(bootstrap.get("streamId", ""))
    finally:
        if path and revision and project_id and lease_token:
            try:
                response = client.request(
                    "DELETE",
                    f"/api/v1/projects/{project_id}/files/entries",
                    params={"path": path},
                    headers={"X-Control-Lease-Token": lease_token, "If-Match": f'"{revision}"'},
                )
                if response.status_code != 204:
                    details["cleanup"] = f"temporary verifier file cleanup returned {response.status_code}"
            except Exception as error:
                details["cleanup"] = f"temporary verifier file cleanup failed: {error}"
        if lease_token:
            try:
                response = client.request(
                    "DELETE", "/api/v1/control/lease", json={"leaseToken": lease_token},
                )
                if response.status_code != 204:
                    details["leaseRelease"] = f"lease release returned {response.status_code}"
            except Exception as error:
                details["leaseRelease"] = f"lease release failed: {error}"
        if logged_in:
            try:
                response = client.request("POST", "/api/v1/auth/logout")
                if response.status_code != 204:
                    details["logout"] = f"logout returned {response.status_code}"
            except Exception as error:
                details["logout"] = f"logout failed: {error}"
        if owned_client:
            client.close()
    return VerificationResult(checks=checks, details=details)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--username", default=os.environ.get("MANYSELVES_ADMIN_USERNAME", "admin"))
    parser.add_argument("--password", help="administrator password; prefer --password-env")
    parser.add_argument("--password-env", default="MANYSELVES_ADMIN_PASSWORD")
    args = parser.parse_args()
    password = args.password or os.environ.get(args.password_env, "")
    result = verify_deployment(args.url, args.username, password)
    print(json.dumps({**asdict(result), "passed": result.passed}, indent=2, sort_keys=True))
    return 0 if result.passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
