"""BONUS: Redis-mirrored circuit breaker state — verify failure counters are
visible across independent CircuitBreaker instances sharing one Redis client,
and that Redis graceful degradation falls back cleanly when Redis is down.

Skipped automatically if Redis isn't reachable (see tests/test_redis_cache.py
for the same pattern used elsewhere in this suite).
"""
import pytest

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState

redis = pytest.importorskip("redis")


def _redis_available() -> bool:
    try:
        client = redis.Redis.from_url("redis://localhost:6379/0", socket_connect_timeout=0.5)
        return bool(client.ping())
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _redis_available(), reason="Redis not running (make docker-up)")


@pytest.fixture
def redis_client():  # type: ignore[no-untyped-def]
    client = redis.Redis.from_url("redis://localhost:6379/0", decode_responses=True)
    client.delete("rl:breaker:test-shared:failures")
    yield client
    client.delete("rl:breaker:test-shared:failures")


def test_failure_count_is_visible_across_instances(redis_client) -> None:  # type: ignore[no-untyped-def]
    breaker_a = CircuitBreaker("test-shared", failure_threshold=99, reset_timeout_seconds=10, redis_client=redis_client)
    breaker_b = CircuitBreaker("test-shared", failure_threshold=99, reset_timeout_seconds=10, redis_client=redis_client)

    breaker_a.record_failure()
    breaker_a.record_failure()

    assert breaker_a.failure_count == 2
    assert breaker_b.failure_count == 0  # local state is per-instance
    assert breaker_b.shared_failure_count() == 2  # but visible via Redis

    breaker_b.record_failure()
    assert breaker_a.shared_failure_count() == 3


def test_success_resets_shared_counter(redis_client) -> None:  # type: ignore[no-untyped-def]
    breaker = CircuitBreaker("test-shared", failure_threshold=99, reset_timeout_seconds=10, redis_client=redis_client)
    breaker.record_failure()
    assert breaker.shared_failure_count() == 1
    breaker.record_success()
    assert breaker.shared_failure_count() == 0


def test_local_state_machine_unaffected_by_redis_mirror(redis_client) -> None:  # type: ignore[no-untyped-def]
    breaker = CircuitBreaker("test-shared", failure_threshold=1, reset_timeout_seconds=10, redis_client=redis_client)
    breaker.record_failure()
    assert breaker.state == CircuitState.OPEN


def test_no_redis_client_returns_sentinel() -> None:
    breaker = CircuitBreaker("test-shared", failure_threshold=1, reset_timeout_seconds=10)
    breaker.record_failure()
    assert breaker.shared_failure_count() == -1
