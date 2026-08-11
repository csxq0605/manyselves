from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.models import (
    EvidenceItem,
    ManifestFile,
    ProjectManifest,
    ReportRequest,
    SourceLocation,
)
from manyselves.core.reporting.preparation import FilePreparationResult
from manyselves.core.reporting.service import ReportingService
from manyselves.core.tools.task_board import TaskBoard


class NoCallProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="no-call")

    async def chat(self, *args, **kwargs):
        raise AssertionError("preparation must not call the Provider")


@pytest.mark.asyncio
async def test_per_file_workers_overlap_but_reducer_keeps_manifest_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NoCallProvider(),
    )
    files = [
        ManifestFile(
            id=f"file-{index}",
            path=Path(f"Inputs/{index}.txt"),
            sha256=f"{index}" * 64,
            media_type="text/plain",
        )
        for index in (1, 2, 3)
    ]
    active = 0
    maximum_active = 0
    lock = threading.Lock()

    def fake_prepare(
        _workspace: Path,
        _run_id: str,
        manifest_file: ManifestFile,
        order: int,
    ) -> FilePreparationResult:
        nonlocal active, maximum_active
        with lock:
            active += 1
            maximum_active = max(maximum_active, active)
        time.sleep(0.04 * (3 - order))
        with lock:
            active -= 1
        return FilePreparationResult(
            manifest_order=order,
            file_id=manifest_file.id,
            source_path=manifest_file.path,
            source_sha256=manifest_file.sha256,
            purpose=None,
            status="parsed",
            provisional_evidence=[
                EvidenceItem(
                    id=f"provisional-{manifest_file.id}",
                    subject=manifest_file.id,
                    fact=f"complete fact for {manifest_file.id}",
                    source=SourceLocation(
                        file_id=manifest_file.id,
                        path=manifest_file.path,
                    ),
                )
            ],
        )

    monkeypatch.setattr(
        "manyselves.core.reporting.service.prepare_manifest_file",
        fake_prepare,
    )
    state = {
        "run_id": "run-parallel-preparation",
        "request": ReportRequest(
            operation="module_report",
            instruction="prepare",
            target_modules=["2.1"],
            preparation_mode="deterministic_workers",
            preparation_concurrency=3,
        ),
        "project_manifest": ProjectManifest(files=files),
    }

    await service._parse_artifacts(state)
    await service._normalize_evidence(state)

    assert maximum_active == 3
    assert state["preparation_parallelism"]["reducer_order"] == [
        "file-1",
        "file-2",
        "file-3",
    ]
    assert [item.id for item in state["evidence_items"]] == [
        "E-0001",
        "E-0002",
        "E-0003",
    ]
    assert [item.subject for item in state["evidence_items"]] == [
        "file-1",
        "file-2",
        "file-3",
    ]
