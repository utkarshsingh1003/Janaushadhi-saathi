"""Per-process rate limiter behaviour."""

from __future__ import annotations

from core.ratelimit import RateLimiter


def test_rate_limiter_allows_up_to_limit_then_blocks():
    limiter = RateLimiter()
    for _ in range(3):
        allowed, retry = limiter.allow("client", 3, 60)
        assert allowed is True
        assert retry == 0
    allowed, retry = limiter.allow("client", 3, 60)
    assert allowed is False
    assert retry >= 1
    allowed, _ = limiter.allow("another-client", 3, 60)
    assert allowed is True
