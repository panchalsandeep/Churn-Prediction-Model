"""Phase 1: rate limiting shared across processes (Redis), with graceful fallback."""
from __future__ import annotations

import fakeredis

from churn import security


def test_limit_is_shared_between_two_web_processes():
    server = fakeredis.FakeServer()
    a = security.make_limiter(client=fakeredis.FakeStrictRedis(server=server))
    b = security.make_limiter(client=fakeredis.FakeStrictRedis(server=server))
    assert [a.allow('u1', 3)[0] for _ in range(2)] == [True, True]
    assert b.allow('u1', 3)[0] is True                       # the third request hits a different process...
    ok, retry = a.allow('u1', 3)                              # ...and the fourth is refused on either
    assert ok is False and 1 <= retry <= 60
    assert b.allow('u1', 3)[0] is False
    assert a.allow('u2', 3)[0] is True                        # other keys are unaffected


def test_window_expires():
    r = security.make_limiter(client=fakeredis.FakeStrictRedis())
    assert r.allow('k', 1, window=0.2)[0] is True
    assert r.allow('k', 1, window=0.2)[0] is False
    import time
    time.sleep(0.3)
    assert r.allow('k', 1, window=0.2)[0] is True


def test_redis_outage_falls_back_to_the_local_limiter_instead_of_failing_requests():
    class Down:
        def pipeline(self):
            raise ConnectionError('redis is down')

        def scan_iter(self, *_):
            raise ConnectionError('redis is down')

    lim = security.make_limiter(client=Down())
    assert lim.allow('k', 2)[0] is True and lim.allow('k', 2)[0] is True
    assert lim.allow('k', 2)[0] is False                      # still limited, per process
    lim.reset()


def test_no_redis_url_means_the_in_process_limiter():
    assert isinstance(security.make_limiter(url=''), security.RateLimiter)
