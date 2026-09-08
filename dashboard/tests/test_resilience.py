"""The breaker is the one retry policy. Time is injected so nothing sleeps."""
import resilience


class Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        return self.t


def make(**kw):
    clock = Clock()
    b = resilience.Breaker("test", failures_to_open=3, open_seconds=100,
                           max_open_seconds=400, clock=clock, **kw)
    return b, clock


def test_opens_after_three_consecutive_failures():
    b, _ = make()
    b.record_failure("a"); b.record_failure("b")
    assert b.state == "closed" and b.allow()
    b.record_failure("c")
    assert b.state == "open" and not b.allow()
    assert b.retry_after() == 100
    assert b.snapshot()["last_error"] == "c"


def test_success_resets_the_failure_count():
    b, _ = make()
    b.record_failure(); b.record_failure(); b.record_success()
    b.record_failure(); b.record_failure()
    assert b.state == "closed"


def test_half_open_admits_one_probe_and_closes_on_success():
    b, clock = make()
    for _ in range(3):
        b.record_failure()
    clock.t = 101
    assert b.state == "half-open"
    assert b.allow() is True          # the probe
    assert b.allow() is False         # nobody else while it is out
    b.record_success()
    assert b.state == "closed" and b.allow() and b.retry_after() == 0


def test_open_window_doubles_per_opening_and_caps():
    b, clock = make()
    for _ in range(3):
        b.record_failure()
    assert b.retry_after() == 100
    clock.t = 101; b.allow(); b.record_failure()          # failed probe: 200
    assert b.retry_after() == 200
    clock.t = 302; b.allow(); b.record_failure()          # 400
    assert b.retry_after() == 400
    clock.t = 703; b.allow(); b.record_failure()          # capped at 400
    assert b.retry_after() == 400
    assert b.reason().startswith("test breaker open, retry in 400s")


def test_registry_shares_one_breaker_per_name():
    resilience.reset_all()
    a = resilience.get("yfinance")
    assert resilience.get("yfinance") is a
    a.record_failure("x")
    assert resilience.snapshot_all()["yfinance"]["failures"] == 1
    resilience.reset_all()


def test_nothing_by_that_name_is_not_an_outage():
    b, _ = make()
    for msg in ("EmptyDataError: No results found", "No fundamentals data found for symbol: 3115.HK",
                "HTTP Error 404: not found"):
        b.record_failure(msg)
    assert b.state == "closed"
    assert resilience.is_outage("HTTP Error 401: Invalid Crumb")
    assert not resilience.is_outage(ValueError("possibly delisted; no timezone found"))
