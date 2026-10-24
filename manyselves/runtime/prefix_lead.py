"""Tell a Map when its lead item's first model response has started.

A Map with a lead starts the first item alone so the others can reuse the
prompt prefix it sends.  The provider can only reuse a prefix it has already
read, and it reads the whole prompt before the first token comes back.  So the
moment the lead's first token arrives is the earliest point at which the
prefix is known to be read.  The provider does not report when its cache entry
is written, so there is no stronger signal to wait for.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_ON_FIRST_RESPONSE: ContextVar[Callable[[], None] | None] = ContextVar(
    "prefix_lead_on_first_response", default=None
)


def notify_first_response() -> None:
    """Called by the agent loop when a model response starts arriving."""

    callback = _ON_FIRST_RESPONSE.get()
    if callback is not None:
        callback()


@contextmanager
def watching_first_response(callback: Callable[[], None]) -> Iterator[None]:
    """Run the block with ``callback`` called on its first model response.

    An enclosing watcher (an outer Map's lead) is still told as well.
    """

    outer = _ON_FIRST_RESPONSE.get()

    def both() -> None:
        callback()
        if outer is not None:
            outer()

    token = _ON_FIRST_RESPONSE.set(both)
    try:
        yield
    finally:
        _ON_FIRST_RESPONSE.reset(token)
