"""G4-24: discovery resilience to intermittent Gamma timeouts.

A read-timeout (Gamma alive but slow) must not, on its own, open the breaker; a
connection-error / 5xx (Gamma dead) trips it on the second. Discovery is bounded
by a wall-clock budget so one slow page can't hang the scheduler, and every cycle
is attributed via last_fetch_status.

G4-25 additions: (connect, read) tuple timeouts; 4→2 price retries;
exponential breaker cooldown (10→20→40→60 min, cap 60); _open_count resets
on success only (not on expiry).
"""
import time
import requests
import pytest

from src.market.gamma_client import (
    GammaClient,
    _HARD_FAILURE_THRESHOLD,
    _TIMEOUT_THRESHOLD,
    _PRICES_CONNECT_TIMEOUT_S,
    _PRICES_READ_TIMEOUT_S,
    _PRICES_MAX_ATTEMPTS,
    _DISCOVERY_CONNECT_TIMEOUT_S,
    _DISCOVERY_TIMEOUT_S,
    _COOLDOWN_MINUTES_BASE,
    _COOLDOWN_MINUTES_MAX,
)


def _client():
    return GammaClient()


# ---------------------------------------------------------------------------
# _classify
# ---------------------------------------------------------------------------

def test_classify_read_timeout_is_soft():
    assert GammaClient._classify(requests.exceptions.ReadTimeout()) == "timeout"


def test_classify_connect_error_is_hard():
    assert GammaClient._classify(requests.exceptions.ConnectionError()) == "hard"
    assert GammaClient._classify(requests.exceptions.ConnectTimeout()) == "hard"


def test_classify_http_codes():
    def http(code):
        r = requests.Response()
        r.status_code = code
        return requests.HTTPError(response=r)
    assert GammaClient._classify(http(503)) == "hard"
    assert GammaClient._classify(http(502)) == "hard"
    assert GammaClient._classify(http(429)) == "timeout"
    assert GammaClient._classify(http(404)) == "other"
    assert GammaClient._classify(http(403)) == "other"


# ---------------------------------------------------------------------------
# Breaker thresholds — soft vs hard
# ---------------------------------------------------------------------------

def test_single_read_timeout_does_not_open_breaker():
    c = _client()
    c._record_failure("timeout")
    assert c._is_circuit_open() is False
    assert c._consec_timeout == 1


def test_timeout_threshold_opens_breaker():
    c = _client()
    for _ in range(_TIMEOUT_THRESHOLD):
        c._record_failure("timeout")
    assert c._is_circuit_open() is True


def test_two_hard_failures_open_breaker():
    c = _client()
    assert _HARD_FAILURE_THRESHOLD == 2
    c._record_failure("hard")
    assert c._is_circuit_open() is False   # first hard failure: still closed
    c._record_failure("hard")
    assert c._is_circuit_open() is True     # second: open


def test_success_resets_both_counters():
    c = _client()
    c._record_failure("timeout")
    c._record_failure("hard")
    c._record_success()
    assert c._consec_timeout == 0 and c._consec_hard == 0
    # …and a subsequent single hard failure still doesn't open (counter reset)
    c._record_failure("hard")
    assert c._is_circuit_open() is False


def test_other_4xx_never_opens_breaker():
    c = _client()
    for _ in range(10):
        c._record_failure("other")
    assert c._is_circuit_open() is False


# ---------------------------------------------------------------------------
# Pagination budget + single-failure-per-page + recovery
# ---------------------------------------------------------------------------

def test_single_slow_page_counts_as_one_failure(monkeypatch):
    """A page that read-times-out through all retries = ONE timeout increment."""
    c = _client()
    monkeypatch.setattr(time, "sleep", lambda *_: None)  # no real backoff waits

    def always_timeout(*a, **k):
        raise requests.exceptions.ReadTimeout("slow")
    monkeypatch.setattr(c, "_get_once", always_timeout)

    events = c._paginate_events(deadline=time.monotonic() + 5, q="temperature")
    assert events == []
    assert c._consec_timeout == 1          # not 3 (retries don't each count)
    assert c._is_circuit_open() is False   # one slow cycle never trips
    assert c._last_fetch_status == "timeout_budget"


def test_single_timeout_recovers_next_cycle(monkeypatch):
    """Acceptance: after a single read-timeout, the next cycle fetches normally."""
    c = _client()
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    # Cycle 1: everything times out → soft failure, breaker stays closed.
    monkeypatch.setattr(c, "_get_once",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ReadTimeout()))
    c._paginate_events(deadline=time.monotonic() + 5, q="temperature")
    assert c._is_circuit_open() is False

    # Cycle 2: Gamma responds → not skipped, success resets the soft counter.
    one_page = {"data": [{"id": "e1"}]}
    monkeypatch.setattr(c, "_get_once", lambda *a, **k: one_page if k.get("params", {}).get("offset", 0) == 0 else {"data": []})
    out = c._paginate_events(deadline=time.monotonic() + 5, q="temperature")
    assert [e["id"] for e in out] == ["e1"]
    assert c._consec_timeout == 0
    assert c._last_fetch_status == "ok"


def test_budget_exhausted_stops_without_breaker(monkeypatch):
    """A past deadline stops paging immediately and attributes timeout_budget."""
    c = _client()
    out = c._paginate_events(deadline=time.monotonic() - 1, q="temperature")
    assert out == []
    assert c._last_fetch_status == "timeout_budget"
    assert c._is_circuit_open() is False


def test_breaker_open_skips_and_attributes(monkeypatch):
    c = _client()
    for _ in range(_HARD_FAILURE_THRESHOLD):
        c._record_failure("hard")
    assert c._is_circuit_open() is True
    out = c._paginate_events(deadline=time.monotonic() + 5, q="temperature")
    assert out == []
    assert c._last_fetch_status == "breaker_open"


def test_hard_failure_during_paging_attributes_endpoint_error(monkeypatch):
    c = _client()
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    monkeypatch.setattr(c, "_get_once",
                        lambda *a, **k: (_ for _ in ()).throw(requests.exceptions.ConnectionError()))
    out = c._paginate_events(deadline=time.monotonic() + 5, q="temperature")
    assert out == []
    assert c._last_fetch_status == "endpoint_error"
    assert c._consec_hard == 1


# ---------------------------------------------------------------------------
# G4-25: timeout constants (connect, read) and retry count
# ---------------------------------------------------------------------------

def test_prices_timeout_constants():
    assert _PRICES_CONNECT_TIMEOUT_S == 5
    assert _PRICES_READ_TIMEOUT_S == 30
    assert _PRICES_MAX_ATTEMPTS == 2


def test_discovery_timeout_constants():
    assert _DISCOVERY_CONNECT_TIMEOUT_S == 3
    assert _DISCOVERY_TIMEOUT_S == 20.0


def test_get_uses_tuple_timeout(monkeypatch):
    """_get must pass (connect, read) tuple — not a bare float."""
    c = _client()
    captured: list = []

    def fake_get(url, params=None, timeout=None):
        captured.append(timeout)
        r = requests.Response()
        r.status_code = 200
        r._content = b"[]"
        return r

    monkeypatch.setattr(c._session, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    c._get("/events")
    assert captured[0] == (_PRICES_CONNECT_TIMEOUT_S, _PRICES_READ_TIMEOUT_S)


def test_get_once_uses_tuple_timeout(monkeypatch):
    """_get_once must pass (connect_timeout, read_timeout) as tuple."""
    c = _client()
    captured: list = []

    def fake_get(url, params=None, timeout=None):
        captured.append(timeout)
        r = requests.Response()
        r.status_code = 200
        r._content = b"[]"
        return r

    monkeypatch.setattr(c._session, "get", fake_get)
    monkeypatch.setattr(time, "sleep", lambda *_: None)
    c._get_once("/events")
    assert captured[0] == (_DISCOVERY_CONNECT_TIMEOUT_S, _DISCOVERY_TIMEOUT_S)


# ---------------------------------------------------------------------------
# G4-25: exponential breaker cooldown
# ---------------------------------------------------------------------------

def test_first_open_cooldown_is_base():
    c = _client()
    c._record_failure("hard")
    c._record_failure("hard")   # 2nd → open
    assert c._open_count == 1
    delta = c._circuit_open_until - __import__("datetime").datetime.now()
    minutes = delta.total_seconds() / 60
    assert abs(minutes - _COOLDOWN_MINUTES_BASE) < 0.1


def test_second_open_doubles_cooldown():
    c = _client()
    # First open
    c._record_failure("hard"); c._record_failure("hard")
    assert c._open_count == 1
    # Force-expire the breaker without a success (simulates timeout passing)
    c._circuit_open_until = None
    c._consec_hard = 0
    # Second open
    c._record_failure("hard"); c._record_failure("hard")
    assert c._open_count == 2
    delta = c._circuit_open_until - __import__("datetime").datetime.now()
    minutes = delta.total_seconds() / 60
    assert abs(minutes - _COOLDOWN_MINUTES_BASE * 2) < 0.1


def test_cooldown_caps_at_maximum():
    c = _client()
    # Simulate many opens to force the cap
    c._open_count = 10
    c._open_circuit("test cap")
    delta = c._circuit_open_until - __import__("datetime").datetime.now()
    minutes = delta.total_seconds() / 60
    assert minutes <= _COOLDOWN_MINUTES_MAX + 0.1


def test_success_resets_open_count():
    c = _client()
    c._record_failure("hard"); c._record_failure("hard")
    assert c._open_count == 1
    c._open_count = 3  # simulate multiple prior opens
    c._record_success()
    assert c._open_count == 0


def test_expiry_does_not_reset_open_count():
    """Breaker expiry alone must NOT reset _open_count — only success does."""
    c = _client()
    c._record_failure("hard"); c._record_failure("hard")
    assert c._open_count == 1
    # Simulate expiry: wind back the timer
    from datetime import datetime, timedelta
    c._circuit_open_until = datetime.now() - timedelta(seconds=1)
    c._is_circuit_open()   # should clear _circuit_open_until but keep _open_count
    assert c._open_count == 1   # NOT reset
    assert c._circuit_open_until is None


def test_exponential_sequence_10_20_40_60():
    """Verify the full 10→20→40→60 sequence with cap enforcement."""
    from datetime import datetime
    c = _client()
    expected = [10, 20, 40, 60, 60]
    for i, exp_min in enumerate(expected):
        c._consec_hard = 0
        c._circuit_open_until = None
        c._record_failure("hard"); c._record_failure("hard")
        delta = c._circuit_open_until - datetime.now()
        got_min = delta.total_seconds() / 60
        assert abs(got_min - exp_min) < 0.1, f"open #{i+1}: expected {exp_min}min, got {got_min:.1f}min"
