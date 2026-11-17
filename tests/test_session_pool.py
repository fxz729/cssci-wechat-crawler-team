import pytest

from config.session_credentials import SessionCredential
from crawl.session_pool import SessionOutcome, SessionPool, SessionPoolExhausted, SessionState


def _credential(session_id: str) -> SessionCredential:
    return SessionCredential(
        session_id=session_id,
        backend_token="backend-token",
        fingerprint="fingerprint",
        cookie="private-cookie",
        user_agent="test-agent",
        cookie_names=frozenset(),
    )


def _pool(*identifiers: str) -> SessionPool:
    return SessionPool(tuple(_credential(identifier) for identifier in identifiers), "target-biz")


def test_leases_sessions_in_file_order_and_never_leases_one_twice():
    pool = _pool("first", "second")

    first = pool.lease()
    second = pool.lease()

    assert first.credential.session_id == "first"
    assert second.credential.session_id == "second"
    with pytest.raises(SessionPoolExhausted):
        pool.lease()


def test_releases_usable_session_for_later_reuse():
    pool = _pool("first")
    lease = pool.lease()

    pool.release(lease, SessionOutcome.USABLE)

    assert pool.lease().credential.session_id == "first"


@pytest.mark.parametrize(
    ("outcome", "state"),
    [
        (SessionOutcome.EXPIRED, SessionState.EXPIRED),
        (SessionOutcome.DEGRADED, SessionState.DEGRADED),
        (SessionOutcome.UNAUTHORIZED_FOR_TARGET, SessionState.UNAUTHORIZED_FOR_TARGET),
        (SessionOutcome.NETWORK_FAILED, SessionState.NETWORK_FAILED),
    ],
)
def test_failed_session_is_skipped_and_next_session_takes_over(outcome, state):
    pool = _pool("first", "second")
    first = pool.lease()

    pool.release(first, outcome)

    assert pool.statuses()["first"] == state
    assert pool.lease().credential.session_id == "second"


def test_cooldown_is_temporarily_unavailable_until_reenabled():
    pool = _pool("first", "second")
    first = pool.lease()

    pool.release(first, SessionOutcome.COOLDOWN)

    second = pool.lease()
    assert second.credential.session_id == "second"
    pool.release(second, SessionOutcome.USABLE)
    pool.mark_usable("first")
    assert pool.lease().credential.session_id == "first"


def test_all_permanently_unavailable_sessions_raise_sanitized_error():
    pool = _pool("first")
    lease = pool.lease()
    pool.release(lease, SessionOutcome.EXPIRED)

    with pytest.raises(SessionPoolExhausted) as exc_info:
        pool.lease()

    assert "first" not in str(exc_info.value)
    assert "private-cookie" not in str(exc_info.value)
