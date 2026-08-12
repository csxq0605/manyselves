from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.distributed_runtime import LocalEventStore
from manyselves.core.reporting.headless_runtime import (
    HeadlessReportingRuntime,
    RuntimePaths,
)
from manyselves.core.reporting.web_runtime import (
    LocalAuthorizationService,
    ReportingApi,
    ReportingASGIApp,
)


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("render_existing must not call the Provider")


def _runtime(tmp_path: Path) -> HeadlessReportingRuntime:
    return HeadlessReportingRuntime(
        RuntimePaths(
            project_storage_root=tmp_path / "project",
            service_state_root=tmp_path / "state",
            config_root=tmp_path / "config",
        ),
        llm_provider=NoCallProvider(),
    )


async def _asgi_request(
    app: ReportingASGIApp,
    method: str,
    path: str,
    *,
    token: str | None = None,
    payload: dict | None = None,
) -> tuple[int, bytes]:
    body = json.dumps(payload or {}).encode("utf-8")
    headers = []
    if token is not None:
        headers.append((b"authorization", f"Bearer {token}".encode("ascii")))
    sent: list[dict] = []
    received = False

    async def receive() -> dict:
        nonlocal received
        if received:
            return {"type": "http.disconnect"}
        received = True
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message: dict) -> None:
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers,
        },
        receive,
        send,
    )
    status = next(message["status"] for message in sent if message["type"] == "http.response.start")
    response_body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return status, response_body


@pytest.mark.asyncio
async def test_asgi_routes_expose_health_shell_and_durable_run_submission(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    auth = LocalAuthorizationService(runtime.paths.service_state_root)
    token = "asgi-owner-token-000000000"
    project_id = runtime.jobs.project_id
    auth.bootstrap_member(
        user_id="asgi-owner",
        token=token,
        project_id=project_id,
        role="owner",
    )
    app = ReportingASGIApp(ReportingApi(runtime, auth))

    health_status, health = await _asgi_request(app, "GET", "/healthz")
    index_status, index = await _asgi_request(app, "GET", "/")
    denied_status, _denied = await _asgi_request(
        app, "GET", f"/v1/projects/{project_id}/files"
    )
    upload_status, _uploaded = await _asgi_request(
        app,
        "POST",
        f"/v1/projects/{project_id}/files",
        token=token,
        payload={
            "ref": "Inputs/asgi.md",
            "content_base64": base64.b64encode(b"# ASGI report\n").decode("ascii"),
        },
    )
    create_status, created_body = await _asgi_request(
        app,
        "POST",
        f"/v1/projects/{project_id}/runs",
        token=token,
        payload={
            "operation": "render_existing",
            "instruction": "ASGI submission",
            "source_markdown_ref": "Inputs/asgi.md",
        },
    )
    created = json.loads(created_body)

    assert health_status == 200 and json.loads(health) == {"status": "ok"}
    assert index_status == 200
    assert all(pane in index for pane in (b"Files", b"Progress", b"Main conversation"))
    assert denied_status == 403
    assert upload_status == 201
    assert create_status == 202 and created["status"] == "queued"
    assert runtime.jobs.get_run(created["run_id"]).status == "queued"


@pytest.mark.asyncio
async def test_authorized_web_surface_survives_api_runtime_reconstruction(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    auth = LocalAuthorizationService(runtime.paths.service_state_root)
    owner_token = "owner-token-000000000000"
    viewer_token = "viewer-token-00000000000"
    other_token = "other-owner-00000000000"
    auth.bootstrap_member(
        user_id="owner",
        token=owner_token,
        project_id=runtime.jobs.project_id,
        role="owner",
    )
    auth.bootstrap_member(
        user_id="viewer",
        token=viewer_token,
        project_id=runtime.jobs.project_id,
        role="viewer",
    )
    auth.bootstrap_member(
        user_id="other",
        token=other_token,
        project_id=runtime.jobs.project_id,
        role="owner",
    )
    api = ReportingApi(runtime, auth)
    project_id = runtime.jobs.project_id
    markdown = (
        "# 配电安全专家咨询报告\n\n"
        "## 1. 配电评估概述\n\nWeb 队列正文。\n"
    ).encode("utf-8")

    uploaded = api.upload_file(
        owner_token,
        project_id,
        "Inputs/approved.md",
        markdown,
    )
    created = api.create_run(
        owner_token,
        project_id,
        {
            "operation": "render_existing",
            "instruction": "web render",
            "source_markdown_ref": "Inputs/approved.md",
            "output_filename": "web.docx",
        },
    )
    queued_events = api.events(
        owner_token, project_id, created["run_id"], cursor=0
    )

    assert uploaded["size"] == len(markdown)
    assert created["status"] == "queued"
    assert queued_events["events"][0]["type"] == "RunQueued"
    assert all("task_id" not in event for event in queued_events["events"])
    with pytest.raises(PermissionError, match="read-only"):
        api.cancel_run(viewer_token, project_id, created["run_id"])
    with pytest.raises(PermissionError, match="internal"):
        api.read_file(
            owner_token,
            project_id,
            f"Work/runs/{created['run_id']}/request.json",
        )

    worker_runtime = _runtime(tmp_path)
    result = await worker_runtime.work_once("web-worker")
    reconstructed_api = ReportingApi(
        _runtime(tmp_path),
        LocalAuthorizationService(runtime.paths.service_state_root),
    )
    progress = reconstructed_api.progress(
        owner_token, project_id, created["run_id"]
    )
    replay = reconstructed_api.events(
        owner_token,
        project_id,
        created["run_id"],
        cursor=queued_events["cursor"],
    )
    output, media_type = reconstructed_api.read_file(
        owner_token, project_id, "Outputs/Reports/web.docx"
    )

    assert result is not None and result.status == "completed"
    assert progress["status"] == "completed"
    assert [item["module_id"] for item in progress["modules"]] == [
        "2.1",
        "2.2",
        "2.3",
        "2.4",
        "2.5",
    ]
    assert replay["events"][-1]["type"] == "RunCompleted"
    assert output.startswith(b"PK")
    assert media_type.endswith("wordprocessingml.document")

    conversation = reconstructed_api.create_conversation(
        owner_token, project_id
    )
    reconstructed_api.append_conversation(
        owner_token,
        project_id,
        conversation["session_id"],
        "继续解释报告。",
    )
    stored = reconstructed_api.read_conversation(
        owner_token, project_id, conversation["session_id"]
    )
    assert stored["owner_user_id"] == "owner"
    assert stored["messages"][0]["content"] == "继续解释报告。"
    with pytest.raises(PermissionError, match="another user"):
        reconstructed_api.read_conversation(
            other_token, project_id, conversation["session_id"]
        )


def test_module_task_completion_does_not_promote_module_without_review(
    tmp_path: Path,
) -> None:
    runtime = _runtime(tmp_path)
    auth = LocalAuthorizationService(runtime.paths.service_state_root)
    token = "progress-owner-token-000000"
    project_id = runtime.jobs.project_id
    auth.bootstrap_member(
        user_id="progress-owner",
        token=token,
        project_id=project_id,
        role="owner",
    )
    api = ReportingApi(runtime, auth)
    created = api.create_run(
        token,
        project_id,
        {
            "operation": "module_report",
            "instruction": "生成 2.1",
            "target_modules": ["2.1"],
        },
    )
    run_id = created["run_id"]
    LocalEventStore(runtime.paths.project_storage_root, run_id).append(
        "TypedResultAccepted",
        stage_id="module-authoring",
        task_id="module-2.1-author",
        payload={"module_id": "2.1"},
    )

    projected = api.progress(token, project_id, run_id)
    module = next(item for item in projected["modules"] if item["module_id"] == "2.1")
    assert module["status"] == "running"
    assert module["review_status"] == "not_started"
    public = api.events(token, project_id, run_id)
    assert public["events"][-1]["type"] == "module_status"
    assert public["events"][-1]["module_id"] == "2.1"

    review_ref = (
        f"Work/runs/{run_id}/reviews/module/initial/2.1/completion-r0.json"
    )
    runtime.service.store.write_json(
        review_ref,
        {
            "lifecycle": "module",
            "run_id": run_id,
        },
    )
    runtime.service.store.write_json(
        f"Work/runs/{run_id}/workflow-state.json",
        {
            "module_review_completion_refs": {"2.1": review_ref},
        },
    )
    completed = api.progress(token, project_id, run_id)
    module = next(item for item in completed["modules"] if item["module_id"] == "2.1")
    assert module["status"] == "completed"
    assert module["review_status"] == "completed"
