"""One policy for a provider that keeps failing: stop asking for a while.

Every poller here used to retry a dead provider on a fixed short timer for as
long as it stayed dead — four days of Yahoo's "401 Invalid Crumb" produced
eight hundred identical failures fifteen seconds apart, each one a warning in
the log and a wasted request. The live feed already had the right shape for
this (`feed.RETRY_MIN` doubling to `RETRY_MAX`); this module is that shape
made shareable.

A `Breaker` counts consecutive failures. At `failures_to_open` it opens: calls
are refused (`allow()` is False) for `open_seconds`, doubling on each
consecutive opening up to `max_open_seconds`. When the window lapses it goes
half-open and lets exactly one probe through; a success closes it and resets
the doubling, a failure re-opens it for longer. The caller keeps serving its
last good values in the meantime and says so in `meta`.

Breakers are named and registered so /api/health can report every one. A
name is a provider, not a call — "yfinance" is shared by the six modules that
talk to Yahoo, because Yahoo being down is one fact, not six.
"""
from __future__ import annotations

import threading
import time

_registry: dict[str, "Breaker"] = {}
_registry_lock = threading.Lock()

# Provider answers that mean "nothing by that name", not "the provider is
# down". Three clicks on unknown symbols must not pause Yahoo for everyone.
_NOT_OUTAGE = ("no results", "not found", "no data found", "delisted",
               "emptydataerror", "no fundamentals", "no timezone found")


def is_outage(error) -> bool:
    """Whether a provider error should count toward opening a breaker."""
    if error is None:
        return True
    text = (f"{type(error).__name__}: {error}" if isinstance(error, BaseException)
            else str(error)).lower()
    return not any(word in text for word in _NOT_OUTAGE)


class Breaker:
    def __init__(self, name: str, *, failures_to_open: int = 3,
                 open_seconds: float = 120.0, max_open_seconds: float = 1800.0,
                 clock=time.monotonic):
        self.name = name
        self.failures_to_open = failures_to_open
        self.open_seconds = open_seconds
        self.max_open_seconds = max_open_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0          # consecutive, since the last success
        self._openings = 0          # consecutive openings, drives the doubling
        self._open_until: float | None = None
        self._probing = False       # half-open: one call is out
        self._last_error: str | None = None
        self._opened_at: float | None = None

    # ---------------------------------------------------------------- state

    @property
    def state(self) -> str:
        with self._lock:
            return self._state()

    def _state(self) -> str:
        if self._open_until is None:
            return "closed"
        if self._clock() < self._open_until:
            return "open"
        return "half-open"

    def _window(self) -> float:
        return min(self.open_seconds * (2 ** max(self._openings - 1, 0)),
                   self.max_open_seconds)

    # ---------------------------------------------------------------- use

    def allow(self) -> bool:
        """Whether a call may go out now. Half-open admits one probe."""
        with self._lock:
            state = self._state()
            if state == "closed":
                return True
            if state == "open":
                return False
            if self._probing:
                return False
            self._probing = True
            return True

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._openings = 0
            self._open_until = None
            self._probing = False
            self._last_error = None

    def record_failure(self, error: Exception | str | None = None) -> None:
        if not is_outage(error):
            return
        with self._lock:
            self._failures += 1
            self._last_error = str(error)[:160] if error else None
            was_probe = self._probing
            self._probing = False
            if was_probe or self._failures >= self.failures_to_open:
                self._openings += 1
                self._opened_at = self._clock()
                self._open_until = self._opened_at + self._window()

    def retry_after(self) -> float:
        """Seconds until the next call is worth making — 0 when closed."""
        with self._lock:
            if self._open_until is None:
                return 0.0
            return max(self._open_until - self._clock(), 0.0)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "state": self._state(),
                "failures": self._failures,
                "openings": self._openings,
                "retry_after": (max(self._open_until - self._clock(), 0.0)
                                if self._open_until is not None else 0.0),
                "last_error": self._last_error,
            }

    def reason(self) -> str:
        """The sentence a poller puts in meta.error while refusing to call."""
        return f"{self.name} breaker {self.state}, retry in {int(self.retry_after())}s"


# ---------------------------------------------------------------- registry

def get(name: str, **kwargs) -> Breaker:
    """The shared breaker for a provider, created on first use."""
    with _registry_lock:
        breaker = _registry.get(name)
        if breaker is None:
            breaker = _registry[name] = Breaker(name, **kwargs)
        return breaker


def snapshot_all() -> dict[str, dict]:
    with _registry_lock:
        return {name: b.snapshot() for name, b in _registry.items()}


def reset_all() -> None:
    """Tests only."""
    with _registry_lock:
        _registry.clear()
