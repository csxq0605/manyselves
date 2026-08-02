"""Cancellation-safe ownership for async work that must reach a definite outcome."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Generic, ParamSpec, TypeVar, cast

_P = ParamSpec("_P")
_T = TypeVar("_T")


@dataclass(frozen=True, slots=True)
class OwnedTaskOutcome(Generic[_T]):
    """Completed owned-task state plus caller cancellation observed while waiting."""

    value: _T | None
    error: BaseException | None
    cancellation_requested: bool

    def result(self) -> _T:
        """Return the value or re-raise the owned task's terminal error."""
        if self.error is not None:
            raise self.error
        return cast(_T, self.value)


async def await_owned(awaitable: Awaitable[_T]) -> OwnedTaskOutcome[_T]:
    """Run one owned task to completion despite repeated caller cancellation."""

    async def run() -> _T:
        return await awaitable

    owned = asyncio.create_task(run())
    caller = asyncio.current_task()
    cancellation_requested = False

    while not owned.done():
        try:
            await asyncio.shield(owned)
        except asyncio.CancelledError:
            if caller is None or caller.cancelling() == 0:
                if owned.done():
                    break
                raise
            cancellation_requested = True
            while caller.cancelling():
                caller.uncancel()
        except BaseException:
            if owned.done():
                break
            raise

    try:
        value = owned.result()
    except BaseException as error:
        return OwnedTaskOutcome(
            value=None,
            error=error,
            cancellation_requested=cancellation_requested,
        )
    return OwnedTaskOutcome(
        value=value,
        error=None,
        cancellation_requested=cancellation_requested,
    )


async def to_thread_non_abandoning(
    function: Callable[_P, _T],
    /,
    *args: _P.args,
    **kwargs: _P.kwargs,
) -> _T:
    """Join an owned worker thread before honoring caller cancellation."""
    outcome = await await_owned(asyncio.to_thread(function, *args, **kwargs))
    if outcome.cancellation_requested:
        raise asyncio.CancelledError
    return outcome.result()
