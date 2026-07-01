from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, TypeVar

T = TypeVar("T")


class CircuitState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitOpenError(RuntimeError):
    """Raised when a circuit is open and calls should fail fast."""


@dataclass(slots=True)
class CircuitBreaker:
    """Circuit breaker: CLOSED -> OPEN -> HALF_OPEN -> CLOSED state machine.

    - CLOSED: calls pass through; count failures.
    - OPEN: fail fast until reset timeout elapses.
    - HALF_OPEN: allow a probe; close on success or re-open on failure.

    Thread-safe: state mutations (`allow_request`'s OPEN->HALF_OPEN transition,
    `record_success`, `record_failure`) are guarded by an internal lock so multiple
    threads sharing one breaker (see chaos.py concurrent load test) can't corrupt
    `failure_count`/`state`. The lock is held only around state reads/writes, not
    around the wrapped call itself, so slow provider calls don't serialize each other.

    BONUS: optional `redis_client` mirrors failure counts to Redis (INCR/EXPIRE) so
    multiple gateway instances can observe each other's failure counters, even
    though each instance's local state machine still transitions independently.
    """

    name: str
    failure_threshold: int
    reset_timeout_seconds: float
    success_threshold: int = 1
    state: CircuitState = CircuitState.CLOSED
    failure_count: int = 0
    success_count: int = 0
    opened_at: float | None = None
    transition_log: list[dict[str, str | float]] = field(default_factory=list)
    redis_client: Any | None = field(default=None, repr=False, compare=False)
    redis_key_prefix: str = "rl:breaker:"
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)

    def allow_request(self) -> bool:
        with self._lock:
            if self.state == CircuitState.CLOSED or self.state == CircuitState.HALF_OPEN:
                return True
            if self.state == CircuitState.OPEN:
                if self.opened_at is not None:
                    elapsed = time.monotonic() - self.opened_at
                    if elapsed >= self.reset_timeout_seconds:
                        self._transition(CircuitState.HALF_OPEN, "reset_timeout_elapsed")
                        return True
                return False
            return False

    def call(self, fn: Callable[..., T], *args: object, **kwargs: object) -> T:
        if not self.allow_request():
            raise CircuitOpenError(f"Circuit breaker {self.name} is OPEN")
        try:
            res = fn(*args, **kwargs)
            self.record_success()
            return res
        except Exception as e:
            self.record_failure()
            raise e

    def record_success(self) -> None:
        with self._lock:
            self.failure_count = 0
            self.success_count += 1
            if self.state == CircuitState.HALF_OPEN and self.success_count >= self.success_threshold:
                self._transition(CircuitState.CLOSED, "probe_success")
                self.success_count = 0
        self._redis_reset()

    def record_failure(self) -> None:
        with self._lock:
            self.failure_count += 1
            self.success_count = 0
            if self.state == CircuitState.HALF_OPEN:
                self._transition(CircuitState.OPEN, "probe_failure")
                self.opened_at = time.monotonic()
            elif self.failure_count >= self.failure_threshold:
                self._transition(CircuitState.OPEN, "failure_threshold_reached")
                self.opened_at = time.monotonic()
        self._redis_incr()

    def _transition(self, new_state: CircuitState, reason: str) -> None:
        """Caller must hold self._lock."""
        if self.state == new_state:
            return
        self.transition_log.append(
            {"from": self.state.value, "to": new_state.value, "reason": reason, "ts": time.time()}
        )
        self.state = new_state

    def _redis_incr(self) -> None:
        if self.redis_client is None:
            return
        key = f"{self.redis_key_prefix}{self.name}:failures"
        self.redis_client.incr(key)
        self.redis_client.expire(key, max(int(self.reset_timeout_seconds * 2), 1))

    def _redis_reset(self) -> None:
        if self.redis_client is None:
            return
        self.redis_client.delete(f"{self.redis_key_prefix}{self.name}:failures")

    def shared_failure_count(self) -> int:
        """Read the Redis-mirrored failure count (visible to all instances), or -1 if unavailable."""
        if self.redis_client is None:
            return -1
        value = self.redis_client.get(f"{self.redis_key_prefix}{self.name}:failures")
        return int(value) if value is not None else 0
