"""BONUS: property-based tests — fuzz the circuit breaker state machine with
hypothesis instead of hand-picked sequences, and assert invariants that must
hold no matter what order successes/failures arrive in.
"""
from hypothesis import given, settings
from hypothesis import strategies as st

from reliability_lab.circuit_breaker import CircuitBreaker, CircuitState

outcomes = st.lists(st.booleans(), min_size=0, max_size=50)


def _apply(cb: CircuitBreaker, outcomes_seq: list[bool]) -> None:
    """Apply a sequence of True=success / False=failure directly to the state
    machine, bypassing allow_request()'s wall-clock gating so the test is
    driven purely by the seeded hypothesis sequence, not real time."""
    for ok in outcomes_seq:
        if ok:
            cb.record_success()
        else:
            cb.record_failure()


@given(outcomes, st.integers(min_value=1, max_value=10), st.integers(min_value=1, max_value=5))
@settings(max_examples=200)
def test_state_is_always_valid_enum_member(
    outcomes_seq: list[bool], failure_threshold: int, success_threshold: int
) -> None:
    cb = CircuitBreaker(
        "fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1, success_threshold=success_threshold
    )
    _apply(cb, outcomes_seq)
    assert cb.state in {CircuitState.CLOSED, CircuitState.OPEN, CircuitState.HALF_OPEN}


@given(outcomes, st.integers(min_value=1, max_value=10))
@settings(max_examples=200)
def test_failure_count_never_negative_and_resets_on_success(
    outcomes_seq: list[bool], failure_threshold: int
) -> None:
    cb = CircuitBreaker("fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1)
    for ok in outcomes_seq:
        if ok:
            cb.record_success()
            assert cb.failure_count == 0
        else:
            cb.record_failure()
        assert cb.failure_count >= 0
        assert cb.success_count >= 0


@given(outcomes, st.integers(min_value=1, max_value=10))
@settings(max_examples=200)
def test_closed_state_implies_failure_count_below_threshold(
    outcomes_seq: list[bool], failure_threshold: int
) -> None:
    """A CLOSED breaker (that has never been OPEN) can never be holding
    failure_count >= failure_threshold — record_failure() must have opened it."""
    cb = CircuitBreaker("fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1)
    _apply(cb, outcomes_seq)
    if cb.state == CircuitState.CLOSED and cb.opened_at is None:
        assert cb.failure_count < failure_threshold


@given(outcomes, st.integers(min_value=1, max_value=10))
@settings(max_examples=200)
def test_open_state_implies_opened_at_is_set(outcomes_seq: list[bool], failure_threshold: int) -> None:
    cb = CircuitBreaker("fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1)
    _apply(cb, outcomes_seq)
    if cb.state == CircuitState.OPEN:
        assert cb.opened_at is not None


@given(outcomes, st.integers(min_value=1, max_value=10))
@settings(max_examples=200)
def test_transition_log_is_internally_consistent(outcomes_seq: list[bool], failure_threshold: int) -> None:
    """Each transition's `from` must equal the previous transition's `to`
    (or CLOSED, the initial state, for the first transition)."""
    cb = CircuitBreaker("fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1)
    _apply(cb, outcomes_seq)
    expected_from = CircuitState.CLOSED.value
    for entry in cb.transition_log:
        assert entry["from"] == expected_from
        expected_from = entry["to"]
    assert expected_from == cb.state.value


@given(st.integers(min_value=1, max_value=10))
@settings(max_examples=50)
def test_half_open_failure_always_reopens_never_stays_half_open(failure_threshold: int) -> None:
    cb = CircuitBreaker("fuzz", failure_threshold=failure_threshold, reset_timeout_seconds=1)
    for _ in range(failure_threshold):
        cb.record_failure()
    assert cb.state == CircuitState.OPEN
    cb.state = CircuitState.HALF_OPEN  # simulate reset_timeout having elapsed
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.transition_log[-1]["reason"] == "probe_failure"
