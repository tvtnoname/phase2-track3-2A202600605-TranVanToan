"""BONUS: concurrent load test — hammer a shared gateway/breaker/cache from
multiple threads and assert internal state stays consistent (no lost updates,
no corrupted counters), proving the locks in CircuitBreaker/ResponseCache work.
"""
from concurrent.futures import ThreadPoolExecutor

from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def test_concurrent_requests_do_not_corrupt_breaker_state() -> None:
    provider = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breaker = CircuitBreaker("primary", failure_threshold=1000, reset_timeout_seconds=10)
    gateway = ReliabilityGateway([provider], {"primary": breaker}, ResponseCache(60, 0.9))

    n = 200
    with ThreadPoolExecutor(max_workers=16) as executor:
        results = list(executor.map(lambda i: gateway.complete(f"query {i}"), range(n)))

    assert len(results) == n
    assert all(r.text for r in results)
    # Every non-cache-hit response is a successful call, so success_count in a
    # never-failing, always-CLOSED breaker should never exceed n and never go negative.
    assert 0 <= breaker.success_count <= n
    assert breaker.failure_count == 0
    assert breaker.state == CircuitState.CLOSED


def test_concurrent_cache_writes_do_not_lose_entries() -> None:
    cache = ResponseCache(ttl_seconds=60, similarity_threshold=0.99)
    with ThreadPoolExecutor(max_workers=16) as executor:
        list(executor.map(lambda i: cache.set(f"unique query {i}", f"answer {i}"), range(100)))
    # All 100 distinct entries must be present — no lost updates under concurrent set().
    assert len(cache._entries) == 100
