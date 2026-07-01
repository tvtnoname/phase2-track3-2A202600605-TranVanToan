"""BONUS: cost-aware routing tests for ReliabilityGateway's budget logic."""
from reliability_lab.cache import ResponseCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.gateway import ReliabilityGateway
from reliability_lab.providers import FakeLLMProvider


def _gateway(cost_budget: float | None, warning_pct: float = 0.8) -> ReliabilityGateway:
    primary = FakeLLMProvider("primary", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.01)
    backup = FakeLLMProvider("backup", fail_rate=0.0, base_latency_ms=1, cost_per_1k_tokens=0.001)
    breakers = {
        "primary": CircuitBreaker("primary", failure_threshold=99, reset_timeout_seconds=10),
        "backup": CircuitBreaker("backup", failure_threshold=99, reset_timeout_seconds=10),
    }
    return ReliabilityGateway(
        [primary, backup], breakers, cost_budget=cost_budget, cost_budget_warning_pct=warning_pct
    )


def test_no_budget_always_uses_primary() -> None:
    gateway = _gateway(cost_budget=None)
    for _ in range(5):
        result = gateway.complete("hello")
        assert result.provider == "primary"


def test_below_warning_pct_uses_primary() -> None:
    gateway = _gateway(cost_budget=1.0, warning_pct=0.8)
    result = gateway.complete("hello")
    assert result.provider == "primary"
    assert gateway._budget_utilization() is not None
    assert gateway._budget_utilization() < 0.8


def test_above_warning_pct_skips_expensive_provider() -> None:
    gateway = _gateway(cost_budget=0.001, warning_pct=0.0)
    result = gateway.complete("hello")
    assert result.provider == "backup"
    assert result.route == "fallback"


def test_budget_exhausted_returns_static_fallback_without_provider_call() -> None:
    gateway = _gateway(cost_budget=0.0000001, warning_pct=0.5)
    gateway.cumulative_cost = 1.0  # simulate budget already spent
    result = gateway.complete("hello")
    assert result.route == "static_fallback"
    assert result.error == "cost_budget_exhausted"
    assert result.provider is None
    assert result.estimated_cost == 0.0


def test_cache_hit_bypasses_budget_check() -> None:
    cache = ResponseCache(60, 0.5)
    gateway = _gateway(cost_budget=0.0000001, warning_pct=0.5)
    gateway.cache = cache
    gateway.cache.set("cached query", "cached answer")
    result = gateway.complete("cached query")
    assert result.cache_hit
    assert result.text == "cached answer"
