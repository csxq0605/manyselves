from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest
from docx import Document

from manyselves import headless_service
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.execution_runtime import (
    ProviderRouter,
    TaskExecutionProfile,
)
from manyselves.core.reporting.headless_runtime import (
    HeadlessReportingRuntime,
    RuntimePaths,
)
from manyselves.core.reporting.models import ReportRequest
from manyselves.core.reporting.service import ReportingRunResult


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("render_existing must not call the Provider")


def _paths(tmp_path: Path) -> RuntimePaths:
    return RuntimePaths(
        project_storage_root=tmp_path / "project",
        service_state_root=tmp_path / "state",
        config_root=tmp_path / "config",
    )


def test_runtime_paths_require_explicit_deployment_roots(tmp_path: Path) -> None:
    paths = RuntimePaths.from_environment(
        {
            "MANYSELVES_PROJECT_STORAGE_ROOT": str(tmp_path / "project"),
            "MANYSELVES_SERVICE_STATE_ROOT": str(tmp_path / "state"),
            "MANYSELVES_CONFIG_ROOT": str(tmp_path / "config"),
        }
    )
    assert paths.project_storage_root == (tmp_path / "project").resolve()
    with pytest.raises(ValueError, match="not configured"):
        RuntimePaths.from_environment({})


def test_headless_import_graph_does_not_load_qt() -> None:
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; "
                "import manyselves.core.reporting.headless_runtime; "
                "import manyselves.headless_service; "
                "assert not any(name.startswith('PyQt6') for name in sys.modules)"
            ),
        ],
        cwd=Path(__file__).resolve().parents[2],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def test_api_process_builds_without_provider_secret_or_qt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    paths = _paths(tmp_path)
    monkeypatch.setenv(
        "MANYSELVES_PROJECT_STORAGE_ROOT", str(paths.project_storage_root)
    )
    monkeypatch.setenv(
        "MANYSELVES_SERVICE_STATE_ROOT", str(paths.service_state_root)
    )
    monkeypatch.setenv("MANYSELVES_CONFIG_ROOT", str(paths.config_root))
    monkeypatch.delenv("MANYSELVES_PROVIDER_API_KEY", raising=False)

    app = headless_service.create_api_app()

    assert app.api.runtime.service.llm_provider.model == "api-process-does-not-call-provider"


def test_headless_runtime_injects_deployment_profiles_into_every_runner(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    paths.config_root.mkdir(parents=True)
    profile = TaskExecutionProfile(
        profile_id="configured-review",
        task_kind="module_review",
        effort="high",
        working_memory_tokens=64_000,
        max_output_tokens=32_768,
        max_tool_rounds=20,
        max_tool_calls_per_round=8,
        max_tool_result_chars=32_000,
    )
    (paths.config_root / "execution-profiles.json").write_text(
        '{"profiles":{"task_kind:module_review":'
        + profile.model_dump_json()
        + "}}",
        encoding="utf-8",
    )

    runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    runner = runtime.service._agent_runner_for("workflow-config-test")

    assert isinstance(runtime.service.provider_router, ProviderRouter)
    assert runner.provider_router is runtime.service.provider_router
    assert (
        runner.provider_router.profiles["task_kind:module_review"].profile_id
        == "configured-review"
    )


@pytest.mark.asyncio
async def test_headless_submit_returns_run_before_independent_worker_executes(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = paths.project_storage_root / "Work/drafts/approved.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "# 配电安全专家咨询报告\n\n## 1. 配电评估概述\n\n无界面运行正文。\n",
        encoding="utf-8",
    )
    api_runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    job = api_runtime.submit(
        ReportRequest(
            operation="render_existing",
            instruction="render without Qt",
            source_markdown_ref=Path("Work/drafts/approved.md"),
            output_filename="headless.docx",
        )
    )
    source.write_text(
        "# 配电安全专家咨询报告\n\n## 1. 配电评估概述\n\nQUEUED-SOURCE-CHANGED。\n",
        encoding="utf-8",
    )

    assert job.status == "queued"
    assert api_runtime.projection(job.run_id).run_status == "queued"
    # Reconstructing the runtime proves the worker does not depend on the API
    # object's memory or task lifecycle.
    worker_runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    result = await worker_runtime.work_once("worker-one")

    assert result is not None and result.status == "completed"
    assert result.run_id == job.run_id
    assert worker_runtime.jobs.get(job.job_id).status == "completed"
    assert worker_runtime.projection(job.run_id).run_status == "completed"
    assert (paths.project_storage_root / "Outputs/Reports/headless.docx").is_file()
    rendered_text = "\n".join(
        paragraph.text
        for paragraph in Document(
            paths.project_storage_root / "Outputs/Reports/headless.docx"
        ).paragraphs
    )
    assert "无界面运行正文" in rendered_text
    assert "QUEUED-SOURCE-CHANGED" not in rendered_text


@pytest.mark.asyncio
async def test_cancel_is_durable_before_worker_claim(tmp_path: Path) -> None:
    paths = _paths(tmp_path)
    source = paths.project_storage_root / "Work/drafts/approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报告\n\n正文。\n", encoding="utf-8")
    runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    job = runtime.submit(
        ReportRequest(
            operation="render_existing",
            instruction="cancel",
            source_markdown_ref=Path("Work/drafts/approved.md"),
        )
    )

    runtime.jobs.request_cancel(job.run_id)
    result = await HeadlessReportingRuntime(
        paths, llm_provider=NoCallProvider()
    ).work_once("worker-cancel")

    assert result is not None and result.status == "cancelled"
    assert runtime.jobs.get(job.job_id).status == "cancelled"


def test_crashed_running_job_is_reclaimed_with_new_fencing_epoch(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    source = paths.project_storage_root / "Work/drafts/approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报告\n\n正文。\n", encoding="utf-8")
    runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    job = runtime.submit(
        ReportRequest(
            operation="render_existing",
            instruction="reclaim",
            source_markdown_ref=Path("Work/drafts/approved.md"),
        )
    )
    stale = runtime.jobs.claim_next("worker-stale")
    assert stale is not None
    first_epoch = stale.lease.lease_epoch
    stale.release()

    reclaimed = runtime.jobs.claim_next("worker-reclaimed")
    assert reclaimed is not None
    try:
        assert reclaimed.job.job_id == job.job_id
        assert reclaimed.job.attempt == 2
        assert reclaimed.lease.lease_epoch == first_epoch + 1
    finally:
        runtime.jobs.finish(reclaimed, status="failed", error="test cleanup")


def test_headless_completed_job_with_missing_output_can_be_requeued(
    tmp_path: Path,
) -> None:
    paths = _paths(tmp_path)
    runtime = HeadlessReportingRuntime(paths, llm_provider=NoCallProvider())
    source = paths.project_storage_root / "Work/drafts/approved.md"
    source.parent.mkdir(parents=True)
    source.write_text("# 报告\n", encoding="utf-8")
    job = runtime.submit(
        ReportRequest(
            operation="render_existing",
            instruction="repair incomplete completion",
            source_markdown_ref=Path("Work/drafts/approved.md"),
        )
    )
    claim = runtime.jobs.claim_next("worker-false-completion")
    assert claim is not None
    runtime.service._save_run(
        ReportingRunResult(
            run_id=job.run_id,
            status="completed",
            output_paths=[
                paths.project_storage_root / "Outputs/Reports/missing.docx"
            ],
        )
    )
    runtime.jobs.finish(claim, status="completed")

    resumed = runtime.resume(job.run_id)

    assert resumed.status == "queued"
    assert resumed.resume_requested is True
