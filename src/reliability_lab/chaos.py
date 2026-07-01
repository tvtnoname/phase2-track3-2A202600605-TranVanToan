from __future__ import annotations

import json
import random
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from reliability_lab.cache import ResponseCache, SharedRedisCache
from reliability_lab.circuit_breaker import CircuitBreaker
from reliability_lab.config import LabConfig, ScenarioConfig
from reliability_lab.gateway import GatewayResponse, ReliabilityGateway
from reliability_lab.metrics import RunMetrics
from reliability_lab.providers import FakeLLMProvider


def load_queries(path: str | Path = "data/sample_queries.jsonl") -> list[str]:
    queries: list[str] = []
    for line in Path(path).read_text().splitlines():
        if not line.strip():
            continue
        queries.append(json.loads(line)["query"])
    return queries


def build_gateway(config: LabConfig, provider_overrides: dict[str, float] | None = None) -> ReliabilityGateway:
    providers = []
    for p in config.providers:
        fail_rate = provider_overrides.get(p.name, p.fail_rate) if provider_overrides else p.fail_rate
        providers.append(FakeLLMProvider(p.name, fail_rate, p.base_latency_ms, p.cost_per_1k_tokens))

    # BONUS: Redis graceful degradation — if backend=redis but Redis is unreachable,
    # fall back to the in-memory cache instead of failing the whole run. The same
    # Redis client (when reachable) is also handed to each CircuitBreaker so failure
    # counters are mirrored across instances (BONUS: Redis circuit state).
    cache: ResponseCache | SharedRedisCache | None = None
    redis_client: Any | None = None
    if config.cache.enabled:
        if config.cache.backend == "redis":
            candidate = SharedRedisCache(
                config.cache.redis_url,
                config.cache.ttl_seconds,
                config.cache.similarity_threshold,
            )
            if candidate.ping():
                cache = candidate
                redis_client = candidate._redis
            else:
                candidate.close()
                print(
                    f"[reliability_lab] Redis at {config.cache.redis_url} unreachable; "
                    "degrading to in-memory cache."
                )
                cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)
        else:
            cache = ResponseCache(config.cache.ttl_seconds, config.cache.similarity_threshold)

    breakers = {
        p.name: CircuitBreaker(
            name=p.name,
            failure_threshold=config.circuit_breaker.failure_threshold,
            reset_timeout_seconds=config.circuit_breaker.reset_timeout_seconds,
            success_threshold=config.circuit_breaker.success_threshold,
            redis_client=redis_client,
        )
        for p in config.providers
    }

    # BONUS: cost-aware routing — skip expensive providers at warning_pct budget
    # utilization, cache-only/static fallback once the budget is exhausted.
    return ReliabilityGateway(
        providers,
        breakers,
        cache,
        cost_budget=config.budget.total,
        cost_budget_warning_pct=config.budget.warning_pct,
    )


def calculate_recovery_time_ms(gateway: ReliabilityGateway) -> float | None:
    recovery_times: list[float] = []
    for breaker in gateway.breakers.values():
        open_ts: float | None = None
        for entry in breaker.transition_log:
            if entry.get("to") == "open":
                open_ts = float(entry["ts"])
            elif entry.get("to") == "closed" and open_ts is not None:
                close_ts = float(entry["ts"])
                delta_ms = (close_ts - open_ts) * 1000
                recovery_times.append(delta_ms)
                open_ts = None
    if not recovery_times:
        return None
    return sum(recovery_times) / len(recovery_times)


def run_scenario(config: LabConfig, queries: list[str], scenario: ScenarioConfig) -> RunMetrics:
    gateway = build_gateway(config, scenario.provider_overrides or None)
    metrics = RunMetrics()

    def do_request(_: int) -> GatewayResponse:
        query = random.choice(queries)
        return gateway.complete(query)

    # BONUS: concurrency — when load_test.concurrency > 1, fire requests through a
    # thread pool against the *same* gateway/breakers/cache to exercise the locks
    # added to CircuitBreaker and ResponseCache. Results are collected into a plain
    # list first and aggregated into `metrics` single-threaded below, so the metrics
    # accumulation itself never needs its own lock.
    concurrency = max(1, config.load_test.concurrency)
    if concurrency > 1:
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            results = list(executor.map(do_request, range(config.load_test.requests)))
    else:
        results = [do_request(i) for i in range(config.load_test.requests)]

    for result in results:
        metrics.total_requests += 1
        metrics.estimated_cost += result.estimated_cost
        
        if result.cache_hit:
            metrics.cache_hits += 1
            metrics.estimated_cost_saved += 0.001
            
        if result.route == "fallback":
            metrics.fallback_successes += 1
            metrics.successful_requests += 1
        elif result.route == "static_fallback":
            metrics.static_fallbacks += 1
            metrics.failed_requests += 1
        else:
            metrics.successful_requests += 1
            
        if result.latency_ms > 0:
            metrics.latencies_ms.append(result.latency_ms)
            
    open_count = 0
    for breaker in gateway.breakers.values():
        for entry in breaker.transition_log:
            if entry.get("to") == "open":
                open_count += 1
    metrics.circuit_open_count = open_count
    metrics.recovery_time_ms = calculate_recovery_time_ms(gateway)
    
    if isinstance(gateway.cache, SharedRedisCache):
        gateway.cache.close()
        
    return metrics


def run_simulation(config: LabConfig, queries: list[str]) -> RunMetrics:
    """Run all named scenarios from config, or a default run if none defined.

    TODO(student): Add a cache vs no-cache comparison scenario.
    Extend with your own custom scenarios (e.g., cost cap near limit).
    """
    if not config.scenarios:
        default_scenario = ScenarioConfig(name="default", description="baseline run")
        metrics = run_scenario(config, queries, default_scenario)
        metrics.scenarios = {"default": "pass" if metrics.successful_requests > 0 else "fail"}
        return metrics

    combined = RunMetrics()
    for scenario in config.scenarios:
        result = run_scenario(config, queries, scenario)

        # TODO(student): Define pass/fail criteria per scenario.
        # Example: primary_timeout_100 passes if fallback_success_rate > 0.9
        passed = result.successful_requests > 0
        combined.scenarios[scenario.name] = "pass" if passed else "fail"

        combined.total_requests += result.total_requests
        combined.successful_requests += result.successful_requests
        combined.failed_requests += result.failed_requests
        combined.fallback_successes += result.fallback_successes
        combined.static_fallbacks += result.static_fallbacks
        combined.cache_hits += result.cache_hits
        combined.circuit_open_count += result.circuit_open_count
        combined.estimated_cost += result.estimated_cost
        combined.estimated_cost_saved += result.estimated_cost_saved
        combined.latencies_ms.extend(result.latencies_ms)
        if result.recovery_time_ms is not None:
            if combined.recovery_time_ms is None:
                combined.recovery_time_ms = result.recovery_time_ms
            else:
                combined.recovery_time_ms = (combined.recovery_time_ms + result.recovery_time_ms) / 2

    return combined
