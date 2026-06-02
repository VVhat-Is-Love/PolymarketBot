"""G4-24: discovery resilience to intermittent Gamma timeouts.

A read-timeout (Gamma alive but slow) must not, on its own, open the breaker; a
connection-error / 5xx (Gamma dead) trips it on the second. Discovery is bounded
by a wall-clock budget so one slow page can't hang the scheduler, and every cycle
is attributed via last_fetch_status.
"""
import time
import requests
import pytest

from src.market.gamma_client import (
    GammaClient,
    _HARD_FAILURE_THRESHOLD,
    _TIMEOUT_THRESHOLD,
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
