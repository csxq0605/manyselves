from __future__ import annotations

from contextlib import nullcontext
from dataclasses import dataclass, field
from typing import Any

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

    def request(self, method: str, path: str, **kwargs: Any) -> FakeResponse:
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


def test_deployment_verifier_checks_runtime_persistence_and_cleanup() -> None:
    deployment = FakeDeployment()
    result = verify_deployment("https://pilot.example.internal", "token", client=deployment)
    assert result.checks == {
        "live": True, "ready": True, "single_runtime": True, "project": True,
        "conversation": True, "file_round_trip": True, "sse": True, "artifact_download": True,
    }
    assert result.passed
    assert deployment.deleted
    assert deployment.released
