from pathlib import Path

import pytest
from pydantic import ValidationError

from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.reporting.models import RevisionRequest
from manyselves.core.reporting.service import ReportingService
from manyselves.core.tools.task_board import TaskBoard


def test_revision_request_defaults_to_report_only_and_validates_scope() -> None:
    request = RevisionRequest(
        baseline_version_id="version-001",
        feedback="修订设备状态边界",
        target_module_ids=["2.4"],
        target_submodule_ids=["2.4.1.1"],
    )

    assert request.promote_to_skill is False
    with pytest.raises(ValidationError, match="promote_skill_id"):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="尝试演进",
            target_module_ids=["2.4"],
            promote_to_skill=True,
        )
    with pytest.raises(ValidationError, match="outside selected modules"):
        RevisionRequest(
            baseline_version_id="version-001",
            feedback="错误范围",
            target_module_ids=["2.4"],
            target_submodule_ids=["2.3.1"],
        )


@pytest.mark.asyncio
async def test_revision_of_unknown_baseline_fails_without_provider_call(tmp_path: Path) -> None:
    class NeverCalledProvider(LLMProvider):
        def __init__(self):
            super().__init__("test", model="never-called")

        async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
            raise AssertionError("missing baseline must fail before Agent execution")

    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=NeverCalledProvider(),
    )

    result = await service.revise(
        RevisionRequest(
            baseline_version_id="missing-version",
            feedback="修订设备状态边界",
            target_module_ids=["2.4"],
        )
    )

    assert result.status == "failed"
    assert "unknown report version" in (result.error or "")
