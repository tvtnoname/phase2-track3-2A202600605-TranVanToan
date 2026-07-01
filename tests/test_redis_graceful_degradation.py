"""BONUS: Redis graceful degradation — build_gateway() must fall back to the
in-memory cache (instead of crashing) when cache.backend=redis but Redis is
unreachable, e.g. wrong port / Redis container stopped.
"""
from reliability_lab.cache import ResponseCache
from reliability_lab.chaos import build_gateway
from reliability_lab.config import CacheConfig, CircuitBreakerConfig, LabConfig, LoadTestConfig, ProviderConfig


def _config(redis_url: str) -> LabConfig:
    return LabConfig(
        providers=[ProviderConfig(name="primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)],
        circuit_breaker=CircuitBreakerConfig(failure_threshold=3, reset_timeout_seconds=1, success_threshold=1),
        cache=CacheConfig(enabled=True, backend="redis", ttl_seconds=60, similarity_threshold=0.9, redis_url=redis_url),
        load_test=LoadTestConfig(requests=1),
    )


def test_unreachable_redis_degrades_to_in_memory_cache() -> None:
    # Port 6390 is not the Redis container's port (6379) — connection must fail fast.
    config = _config("redis://localhost:6390/0")
    gateway = build_gateway(config)
    assert isinstance(gateway.cache, ResponseCache)


def test_gateway_still_works_after_degradation() -> None:
    config = _config("redis://localhost:6390/0")
    gateway = build_gateway(config)
    result = gateway.complete("hello")
    assert result.text
