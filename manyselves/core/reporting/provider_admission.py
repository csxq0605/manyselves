"""Provider-side admission, reservations, and shared backpressure.

The reporting scheduler decides *which typed task* is ready.  This module is
the separate physical-request gate: it limits concurrent requests for the
whole process and for one provider/model, reserves RPM/TPM capacity before a
request is sent, and shares a 429 cooldown across all lanes.  A lease covers
one real request only; callers release it before sleeping for retry backoff.

The implementation is intentionally dependency-free so fake providers can
exercise it offline.  ``asyncio.Condition`` gives waiting callers FIFO order;
capacity release and cooldown updates notify every waiter, which then checks
its ticket again without bypassing the queue head.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Mapping


def provider_model_key(provider: str, model: str | None = None) -> str:
    """Return one stable admission key for a provider/model pair."""

    provider_text = str(provider or "provider").strip() or "provider"
    model_text = str(model or "default").strip() or "default"
    return f"{provider_text}:{model_text}"


@dataclass(slots=True)
class _Reservation:
    ticket: int
    key: str
    granted_at: float
    tpm_tokens: int


class ProviderAdmissionError(RuntimeError):
    """Base error for a request that could not be admitted."""


class ProviderRequestLease:
    """One physical Provider request reservation.

    The lease is an async context manager and is idempotently releasable.  A
    retry caller should leave this context before applying its backoff; the
    controller therefore never counts sleeping retries against concurrency.
    """

    def __init__(
        self,
        controller: "ProviderAdmissionController",
        reservation: _Reservation,
    ) -> None:
        self.controller = controller
        self.reservation = reservation
        self._released = False

    @property
    def provider_model(self) -> str:
        return self.reservation.key

    @property
    def reserved_tokens(self) -> int:
        return self.reservation.tpm_tokens

    @property
    def acquired_at(self) -> float:
        return self.reservation.granted_at

    async def release(
        self,
        *,
        actual_tokens: int | None = None,
        error: BaseException | None = None,
    ) -> None:
        if self._released:
            return
        self._released = True
        await self.controller._release(
            self.reservation,
            actual_tokens=actual_tokens,
            error=error,
        )

    async def mark_rate_limited(self, retry_after: float | None = None) -> None:
        """Publish a shared 429 cooldown while retaining no request lease."""

        self.controller.mark_rate_limited(self.provider_model, retry_after)

    async def __aenter__(self) -> "ProviderRequestLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release(error=exc)


@dataclass(slots=True)
class TaskAdmissionLease:
    """Lease for one typed task under a durable professional identity."""

    controller: "ProviderAdmissionController"
    identity_key: str
    task_id: str
    _released: bool = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self.controller._release_task(self.identity_key, self.task_id)

    async def __aenter__(self) -> "TaskAdmissionLease":
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.release()


@dataclass(slots=True)
class _Waiter:
    ticket: int
    key: str
    estimated_tokens: int
    enqueued_at: float


class ProviderAdmissionController:
    """Global/provider-model request admission with fair backpressure.

    Parameters are optional and unlimited when omitted.  ``global_concurrency``
    applies across all providers; ``provider_model_concurrency`` may be either
    a mapping keyed by ``"provider:model"`` or a single integer default for
    every key.  RPM and TPM are reservations over a rolling 60-second window,
    not post-hoc telemetry and not a business-task cap.
    """

    def __init__(
        self,
        *,
        global_concurrency: int | None = None,
        provider_model_concurrency: int | Mapping[str, int] | None = None,
        rpm: int | None = None,
        tpm: int | None = None,
        provider_model_rpm: Mapping[str, int] | None = None,
        provider_model_tpm: Mapping[str, int] | None = None,
        cooldown_jitter_seconds: float = 0.0,
        now: Callable[[], float] | None = None,
        sleep: Callable[[float], Awaitable[Any]] | None = None,
        random_source: random.Random | None = None,
        **aliases: Any,
    ) -> None:
        # Keep the public constructor tolerant of common config spellings;
        # these aliases do not alter the semantics of typed-task admission.
        if global_concurrency is None:
            global_concurrency = aliases.pop("max_concurrency", None)
        if provider_model_concurrency is None:
            provider_model_concurrency = aliases.pop("provider_model_limits", None)
        if rpm is None:
            rpm = aliases.pop("rpm_limit", None)
        if tpm is None:
            tpm = aliases.pop("tpm_limit", None)
        if aliases:
            unknown = ", ".join(sorted(aliases))
            raise TypeError(f"unknown ProviderAdmissionController options: {unknown}")
        self.global_concurrency = self._limit(global_concurrency)
        if isinstance(provider_model_concurrency, Mapping):
            self.provider_model_concurrency: int | dict[str, int] | None = {
                str(key): self._limit(value) or 0
                for key, value in provider_model_concurrency.items()
            }
        else:
            self.provider_model_concurrency = self._limit(provider_model_concurrency)
        self.rpm = self._limit(rpm)
        self.tpm = self._limit(tpm)
        self.provider_model_rpm = {
            str(key): self._limit(value) or 0
            for key, value in (provider_model_rpm or {}).items()
        }
        self.provider_model_tpm = {
            str(key): self._limit(value) or 0
            for key, value in (provider_model_tpm or {}).items()
        }
        self.cooldown_jitter_seconds = max(0.0, float(cooldown_jitter_seconds))
        self._now = now or time.monotonic
        self._sleep = sleep or asyncio.sleep
        self._random = random_source or random.Random()
        self._condition = asyncio.Condition()
        self._next_ticket = 0
        self._waiters: deque[_Waiter] = deque()
        self._active_global = 0
        self._active_by_key: dict[str, int] = {}
        # Rolling reservation rows: (timestamp, provider/model, requests,
        # tokens).  Retaining reservations until expiry prevents a burst from
        # racing past an RPM/TPM limit before provider telemetry arrives.
        self._reservations: deque[_Reservation] = deque()
        self._cooldown_until: dict[str, float] = {}
        self._task_waiters: deque[tuple[int, str, str]] = deque()
        self._active_tasks: dict[str, str] = {}
        self._task_ticket = 0

    @staticmethod
    def _limit(value: int | None) -> int | None:
        if value is None:
            return None
        parsed = int(value)
        if parsed <= 0:
            raise ValueError("admission limits must be positive")
        return parsed

    @staticmethod
    def _error_status(error: BaseException | None) -> int | None:
        if error is None:
            return None
        original = getattr(error, "original", error)
        status = getattr(original, "status_code", None)
        if status is None:
            response = getattr(original, "response", None)
            status = getattr(response, "status_code", None)
        return int(status) if isinstance(status, int) else None

    def _limit_for_key(self, value: int | Mapping[str, int] | None, key: str) -> int | None:
        if isinstance(value, Mapping):
            configured = value.get(key)
            return int(configured) if configured is not None else None
        return value

    def _rpm_limit(self, key: str) -> int | None:
        return self.provider_model_rpm.get(key, self.rpm)

    def _tpm_limit(self, key: str) -> int | None:
        return self.provider_model_tpm.get(key, self.tpm)

    def _provider_model_limit(self, key: str) -> int | None:
        return self._limit_for_key(self.provider_model_concurrency, key)

    def _prune(self, now: float) -> None:
        cutoff = now - 60.0
        while self._reservations and self._reservations[0].granted_at <= cutoff:
            self._reservations.popleft()

    def _reserved_counts(self, key: str, now: float) -> tuple[int, int]:
        self._prune(now)
        requests = 0
        tokens = 0
        for row in self._reservations:
            if row.key == key:
                requests += 1
                tokens += row.tpm_tokens
        return requests, tokens

    def _head_ready(self, waiter: _Waiter, now: float) -> tuple[bool, float]:
        if not self._waiters or self._waiters[0] is not waiter:
            return False, 0.05
        cooldown = max(0.0, self._cooldown_until.get(waiter.key, 0.0) - now)
        if cooldown:
            return False, cooldown
        provider_limit = self._provider_model_limit(waiter.key)
        if provider_limit is not None and self._active_by_key.get(waiter.key, 0) >= provider_limit:
            return False, 0.05
        if self.global_concurrency is not None and self._active_global >= self.global_concurrency:
            return False, 0.05
        rpm_limit = self._rpm_limit(waiter.key)
        tpm_limit = self._tpm_limit(waiter.key)
        requests, tokens = self._reserved_counts(waiter.key, now)
        if rpm_limit is not None and requests >= rpm_limit:
            return False, 0.05
        if tpm_limit is not None and tokens + waiter.estimated_tokens > tpm_limit:
            return False, 0.05
        return True, 0.0

    async def acquire(
        self,
        provider: str | None = None,
        model: str | None = None,
        *,
        provider_model: str | None = None,
        estimated_tokens: int = 0,
        request_id: str | None = None,
        task_id: str | None = None,
        identity_key: str | None = None,
    ) -> ProviderRequestLease:
        """Wait fairly for one physical request reservation."""

        del request_id, task_id, identity_key  # durable identity is a separate lease
        if provider_model is not None:
            key = str(provider_model)
        else:
            key = provider_model_key(provider or "provider", model)
        estimated = max(0, int(estimated_tokens or 0))
        async with self._condition:
            self._next_ticket += 1
            waiter = _Waiter(self._next_ticket, key, estimated, self._now())
            self._waiters.append(waiter)
            while True:
                now = self._now()
                ready, delay = self._head_ready(waiter, now)
                if ready:
                    self._waiters.popleft()
                    reservation = _Reservation(
                        ticket=waiter.ticket,
                        key=key,
                        granted_at=now,
                        tpm_tokens=estimated,
                    )
                    self._reservations.append(reservation)
                    self._active_global += 1
                    self._active_by_key[key] = self._active_by_key.get(key, 0) + 1
                    return ProviderRequestLease(self, reservation)
                # Wake on release/429 notification, or periodically at the
                # earliest capacity/cooldown check.  No network or backoff is
                # held while waiting here.
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=max(0.001, delay))
                except asyncio.TimeoutError:
                    continue

    async def _release(
        self,
        reservation: _Reservation,
        *,
        actual_tokens: int | None,
        error: BaseException | None,
    ) -> None:
        status = self._error_status(error)
        async with self._condition:
            self._active_global = max(0, self._active_global - 1)
            current = self._active_by_key.get(reservation.key, 0)
            if current <= 1:
                self._active_by_key.pop(reservation.key, None)
            else:
                self._active_by_key[reservation.key] = current - 1
            if actual_tokens is not None:
                reservation.tpm_tokens = max(0, int(actual_tokens))
            if status == 429:
                # A 429 is a shared signal.  Keep the longest known cooldown so
                # a late response cannot shorten another lane's backoff.
                retry_after = getattr(error, "retry_after", None)
                self._set_cooldown_locked(
                    reservation.key,
                    float(retry_after) if retry_after is not None else 1.0,
                )
            self._condition.notify_all()

    def mark_rate_limited(self, provider_or_key: str, retry_after: float | None = None, model: str | None = None) -> None:
        """Publish a provider/model cooldown from a 429 response."""

        key = (
            provider_model_key(provider_or_key, model)
            if model is not None
            else str(provider_or_key)
        )
        # This method is intentionally synchronous so adapters/tests can call
        # it from an exception handler before releasing an async lease.
        self._set_cooldown_locked(key, retry_after)

    def _set_cooldown_locked(self, key: str, retry_after: float | None) -> None:
        delay = max(0.0, float(retry_after if retry_after is not None else 1.0))
        if self.cooldown_jitter_seconds:
            delay += self._random.uniform(0.0, self.cooldown_jitter_seconds)
        until = self._now() + delay
        # No condition lock is required when called by a single-threaded test;
        # async callers still wake at their periodic timeout.  ``notify_all``
        # is safe whenever the lock is held (the normal lease-release path).
        self._cooldown_until[key] = max(self._cooldown_until.get(key, 0.0), until)

    def cooldown_remaining(self, provider: str, model: str | None = None) -> float:
        return max(0.0, self._cooldown_until.get(provider_model_key(provider, model), 0.0) - self._now())

    # Names used by lightweight adapters/tests; all refer to the same shared
    # cooldown state rather than maintaining per-lane retry timers.
    notify_rate_limit = mark_rate_limited
    on_429 = mark_rate_limited

    async def task_acquire(self, identity_key: str, task_id: str) -> TaskAdmissionLease:
        """Admit one typed task per durable identity, independently of RPM/TPM."""

        identity = str(identity_key)
        task = str(task_id)
        async with self._condition:
            self._task_ticket += 1
            ticket = self._task_ticket
            self._task_waiters.append((ticket, identity, task))
            while True:
                head = self._task_waiters[0]
                if head[0] == ticket and identity not in self._active_tasks:
                    self._task_waiters.popleft()
                    self._active_tasks[identity] = task
                    return TaskAdmissionLease(self, identity, task)
                try:
                    await self._condition.wait()
                except asyncio.CancelledError:
                    self._task_waiters = deque(
                        row for row in self._task_waiters if row[0] != ticket
                    )
                    self._condition.notify_all()
                    raise

    async def _release_task(self, identity_key: str, task_id: str) -> None:
        async with self._condition:
            if self._active_tasks.get(identity_key) == task_id:
                self._active_tasks.pop(identity_key, None)
            self._condition.notify_all()

    # Friendly aliases used by callers that distinguish task and request
    # admission explicitly.
    acquire_task = task_acquire
    reserve = acquire

    @asynccontextmanager
    async def request(
        self,
        provider: str | None = None,
        model: str | None = None,
        *,
        provider_model: str | None = None,
        estimated_tokens: int = 0,
        request_id: str | None = None,
        task_id: str | None = None,
        identity_key: str | None = None,
    ):
        """Async context manager for one admitted physical request."""

        lease = await self.acquire(
            provider,
            model,
            provider_model=provider_model,
            estimated_tokens=estimated_tokens,
            request_id=request_id,
            task_id=task_id,
            identity_key=identity_key,
        )
        try:
            yield lease
        except BaseException as exc:
            await lease.release(error=exc)
            raise
        finally:
            await lease.release()


# Some integrations prefer a noun matching the persisted ledger terminology.
ProviderAdmission = ProviderAdmissionController
