from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from manyselves.core.reporting.provider_admission import (
    ProviderAdmissionController,
    provider_model_key,
)
from manyselves.core.reporting.service import ReportingService
from manyselves.core.loops.bus import MessageBus
from manyselves.core.providers.base import LLMProvider
from manyselves.core.tools.task_board import TaskBoard


class _NeverCalledProvider(LLMProvider):
    def __init__(self) -> None:
        super().__init__("test", model="never-called")

    async def chat(self, messages, tools=None, temperature=0.1, max_tokens=8192):
        raise AssertionError("provider must not be called")


def test_provider_model_key_is_stable() -> None:
    assert provider_model_key("openai", "gpt-test") == "openai:gpt-test"
    assert provider_model_key("openai", None) == "openai:default"


def test_reporting_service_shares_one_default_controller_across_workflows(
    tmp_path: Path,
) -> None:
    service = ReportingService(
        tmp_path,
        bus=MessageBus(),
        task_board=TaskBoard(),
        llm_provider=_NeverCalledProvider(),
    )

    first = service._agent_runner_for("workflow-a")
    second = service._agent_runner_for("workflow-b")

    assert first.provider_admission is service.provider_admission
    assert second.provider_admission is service.provider_admission
    assert service.provider_admission.global_concurrency == 8
    assert service.provider_admission.provider_model_concurrency == 8


def test_global_and_provider_model_concurrency_are_enforced() -> None:
    async def scenario() -> tuple[int, int, list[str]]:
        controller = ProviderAdmissionController(
            global_concurrency=2,
            provider_model_concurrency={"fake:model": 1},
        )
        active = 0
        max_active = 0
        order: list[str] = []

        async def worker(name: str):
            nonlocal active, max_active
            async with controller.request("fake", "model"):
                order.append(name)
                active += 1
                max_active = max(max_active, active)
                await asyncio.sleep(0.01)
                active -= 1

        await asyncio.gather(*(worker(str(i)) for i in range(3)))
        return active, max_active, order

    active, max_active, order = asyncio.run(scenario())
    assert active == 0
    assert max_active == 1
    assert order == ["0", "1", "2"]


def test_rpm_tpm_are_reserved_before_request_and_released_after_window() -> None:
    async def scenario() -> None:
        now = [100.0]
        controller = ProviderAdmissionController(
            rpm=1,
            tpm=10,
            now=lambda: now[0],
        )
        first = await controller.acquire("fake", "model", estimated_tokens=10)
        await first.release(actual_tokens=8)
        blocked = asyncio.create_task(
            controller.acquire("fake", "model", estimated_tokens=1)
        )
        await asyncio.sleep(0)
        assert not blocked.done()
        now[0] += 61
        second = await asyncio.wait_for(blocked, timeout=1)
        await second.release()

    asyncio.run(scenario())


def test_429_cooldown_is_shared_by_fair_queue() -> None:
    async def scenario() -> list[str]:
        now = [10.0]
        controller = ProviderAdmissionController(
            provider_model_concurrency={"fake:model": 1},
            now=lambda: now[0],
        )
        first = await controller.acquire("fake", "model")
        await first.mark_rate_limited(5.0)
        await first.release()
        order: list[str] = []

        async def worker(name: str):
            lease = await controller.acquire("fake", "model")
            order.append(name)
            await lease.release()

        tasks = [asyncio.create_task(worker(str(i))) for i in range(3)]
        await asyncio.sleep(0)
        assert order == []
        now[0] += 5.1
        async with controller._condition:
            controller._condition.notify_all()
        await asyncio.wait_for(asyncio.gather(*tasks), timeout=1)
        return order

    assert asyncio.run(scenario()) == ["0", "1", "2"]


def test_429_error_on_request_context_publishes_shared_cooldown() -> None:
    class RateLimitError(RuntimeError):
        status_code = 429
        retry_after = 0.02

    async def scenario() -> float:
        controller = ProviderAdmissionController()
        with pytest.raises(RateLimitError):
            async with controller.request("fake", "model"):
                raise RateLimitError("slow down")
        return controller.cooldown_remaining("fake", "model")

    assert asyncio.run(scenario()) > 0


def test_typed_task_admission_is_separate_from_provider_capacity() -> None:
    async def scenario() -> list[str]:
        controller = ProviderAdmissionController(global_concurrency=1)
        order: list[str] = []

        async def worker(name: str):
            async with await controller.task_acquire("identity", name):
                order.append(name)
                await asyncio.sleep(0.01)

        await asyncio.gather(worker("task-a"), worker("task-b"))
        return order

    assert asyncio.run(scenario()) == ["task-a", "task-b"]
