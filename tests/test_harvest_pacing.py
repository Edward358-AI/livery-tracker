"""The FR24 minimum-interval floor is the single guarantee that paces harvests.

The harvest loop used to sleep 3-5s per tail *as well*, which only doubled the
wait. These tests pin the remaining guarantee so it cannot be removed by
accident.
"""

import time

import livery_tracker.schedule_provider as sp
from livery_tracker.throttle import MinInterval


def test_fetch_flight_list_enforces_minimum_spacing(monkeypatch):
    """Consecutive live lookups are spaced by the process-wide floor.

    Uses a scaled-down interval with generous slack: Windows' sleep
    granularity is ~15ms, so a tight tolerance here makes the test flaky
    without saying anything more about the behaviour.
    """
    interval, tolerance = 0.25, 0.05
    calls: list[float] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"response": {"data": []}}}

    def fake_get(url, **kwargs):
        calls.append(time.monotonic())
        return FakeResponse()

    monkeypatch.setattr(sp.curl_requests, "get", fake_get)
    monkeypatch.setattr(sp, "_LIST_MEMO", sp.TTLCache(ttl_seconds=0))  # defeat memoisation
    monkeypatch.setattr(sp, "_LIST_SPACING", MinInterval(seconds=interval))

    for reg in ("N1", "N2", "N3"):
        sp.fetch_flight_list(reg)

    assert len(calls) == 3
    gaps = [b - a for a, b in zip(calls, calls[1:])]
    assert all(gap >= interval - tolerance for gap in gaps), \
        f"spacing floor not enforced: {gaps}"


def test_repeat_lookups_are_memoised_not_refetched(monkeypatch):
    """A repeated tail costs zero network calls inside the TTL window."""
    calls: list[str] = []

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"response": {"data": [{"x": 1}]}}}

    def fake_get(url, **kwargs):
        calls.append(kwargs.get("params", {}).get("query"))
        return FakeResponse()

    monkeypatch.setattr(sp.curl_requests, "get", fake_get)
    monkeypatch.setattr(sp, "_LIST_MEMO", sp.TTLCache(ttl_seconds=300))
    monkeypatch.setattr(sp, "_LIST_SPACING", MinInterval(seconds=0))

    first = sp.fetch_flight_list("N265AK")
    second = sp.fetch_flight_list("N265AK")

    assert first == second == [{"x": 1}]
    assert calls == ["N265AK"]  # only one real request


def test_failures_are_not_memoised(monkeypatch):
    """A transient outage must not be cached as 'no data' for five minutes."""
    attempts: list[int] = []

    class FailResponse:
        status_code = 503

        @staticmethod
        def json():
            return {}

    def fake_get(url, **kwargs):
        attempts.append(1)
        return FailResponse()

    monkeypatch.setattr(sp.curl_requests, "get", fake_get)
    monkeypatch.setattr(sp, "_LIST_MEMO", sp.TTLCache(ttl_seconds=300))
    monkeypatch.setattr(sp, "_LIST_SPACING", MinInterval(seconds=0))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)  # skip retry backoff

    assert sp.fetch_flight_list("N265AK") is None
    before = len(attempts)
    assert sp.fetch_flight_list("N265AK") is None
    assert len(attempts) > before, "failure was cached — outage would look like no data"


def test_harvest_loop_no_longer_sleeps_per_tail():
    """polite_delay is gone; pacing lives solely in fetch_flight_list."""
    assert not hasattr(sp, "polite_delay")
