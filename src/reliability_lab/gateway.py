from __future__ import annotations

from dataclasses import dataclass

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker, CircuitOpenError
from reliability_lab.providers import FakeLLMProvider, ProviderError


@dataclass(slots=True)
class GatewayResponse:
    text: str
    route: str
    provider: str | None
    cache_hit: bool
    latency_ms: float
    estimated_cost: float
    error: str | None = None


class ReliabilityGateway:
    """Routes requests through cache, circuit breakers, and fallback providers.

    BONUS: Cost-aware routing — tracks cumulative cost and adjusts provider
    selection based on budget utilization:
      - Below 80%: normal routing (primary → fallback chain)
      - 80–100%:   skip expensive providers, only use cheapest ones
      - Above 100%: cache-only or static fallback, no API calls
    """

    def __init__(
        self,
        providers: list[FakeLLMProvider],
        breakers: dict[str, CircuitBreaker],
        cache: ResponseCache | SharedRedisCache | None = None,
        cost_budget: float | None = None,
        cost_budget_warning_pct: float = 0.8,
    ):
        self.providers = providers
        self.breakers = breakers
        self.cache = cache
        self.cost_budget = cost_budget
        self.cost_budget_warning_pct = cost_budget_warning_pct
        self.cumulative_cost: float = 0.0

    def _budget_utilization(self) -> float | None:
        """Return current budget utilization as a fraction (0.0–1.0+), or None if no budget."""
        if self.cost_budget is None or self.cost_budget <= 0:
            return None
        return self.cumulative_cost / self.cost_budget

    def _min_cost_per_1k(self) -> float:
        """Return the lowest cost_per_1k_tokens across all providers."""
        return min(p.cost_per_1k_tokens for p in self.providers)

    def complete(self, prompt: str) -> GatewayResponse:
        # --- CACHE CHECK (always attempted first) ---
        if self.cache is not None:
            cached_text, score = self.cache.get(prompt)
            if cached_text is not None:
                return GatewayResponse(
                    text=cached_text,
                    route=f"cache_hit:{score:.2f}",
                    provider=None,
                    cache_hit=True,
                    latency_ms=0.0,
                    estimated_cost=0.0
                )

        # --- BONUS: Budget exhausted → cache-only / static fallback ---
        utilization = self._budget_utilization()
        if utilization is not None and utilization >= 1.0:
            return GatewayResponse(
                text="The service is temporarily degraded. Please try again soon.",
                route="static_fallback",
                provider=None,
                cache_hit=False,
                latency_ms=0.0,
                estimated_cost=0.0,
                error="cost_budget_exhausted",
            )

        # --- PROVIDER FALLBACK CHAIN ---
        last_error = None
        min_cost = self._min_cost_per_1k()

        for i, provider in enumerate(self.providers):
            # BONUS: When budget >= 80%, skip expensive providers
            if utilization is not None and utilization >= self.cost_budget_warning_pct:
                if provider.cost_per_1k_tokens > min_cost:
                    last_error = f"skipped {provider.name}: cost_budget_warning"
                    continue

            breaker = self.breakers[provider.name]
            try:
                response = breaker.call(provider.complete, prompt)

                # Track cumulative cost
                self.cumulative_cost += response.estimated_cost

                if self.cache is not None:
                    self.cache.set(prompt, response.text, {"provider": provider.name})

                route = "primary" if i == 0 else "fallback"
                return GatewayResponse(
                    text=response.text,
                    route=route,
                    provider=provider.name,
                    cache_hit=False,
                    latency_ms=response.latency_ms,
                    estimated_cost=response.estimated_cost
                )
            except (ProviderError, CircuitOpenError) as e:
                last_error = str(e)
                continue
            except Exception as e:
                last_error = str(e)
                continue

        return GatewayResponse(
            text="The service is temporarily degraded. Please try again soon.",
            route="static_fallback",
            provider=None,
            cache_hit=False,
            latency_ms=0.0,
            estimated_cost=0.0,
            error=last_error
        )
