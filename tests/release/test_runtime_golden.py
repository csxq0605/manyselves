from pathlib import Path

import pytest

from tests.release.runtime_harness import run_legacy_flow, run_service_flow


@pytest.mark.asyncio
async def test_message_interrupt_and_checkpoint_flow_matches_legacy_and_service(tmp_path: Path) -> None:
    legacy = await run_legacy_flow(tmp_path / "legacy")
    service = await run_service_flow(tmp_path / "service")
    assert service.event == legacy.event == ("inspect", "main", "message-1", "user")
    assert service.interrupted == legacy.interrupted == ("main",)
    assert service.rollback == legacy.rollback
    assert service.workspace_manifest == legacy.workspace_manifest
