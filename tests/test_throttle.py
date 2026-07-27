"""Rate-limiting primitives and the memoisation wired onto the API clients."""

import time

import livery_tracker.adsb as adsb
import livery_tracker.schedule_provider as sp
from livery_tracker.throttle import MISS, Cooldown, MinInterval, TTLCache


# -- primitives ----------------------------------------------------------------

def test_ttl_cache_hits_then_expires():
    cache = TTLCache(ttl_seconds=0.15)
    assert cache.get("k") is MISS
    cache.set("k", [1, 2])
    assert cache.get("k") == [1, 2]
    time.sleep(0.2)
    assert cache.get("k") is MISS


def test_ttl_cache_caches_falsy_values():
    """[] and None are real answers ('no flights'), not cache misses."""
    cache = TTLCache(ttl_seconds=5)
    cache.set("empty", [])
    cache.set("none", None)
    assert cache.get("empty") == []
    assert cache.get("none") is None


def test_ttl_cache_evicts_when_full():
    cache = TTLCache(ttl_seconds=60, max_entries=2)
    cache.set("a", 1)
    time.sleep(0.01)
    cache.set("b", 2)
    time.sleep(0.01)
    cache.set("c", 3)
    assert cache.get("a") is MISS      # oldest evicted
    assert cache.get("c") == 3


def test_min_interval_spaces_calls():
    gate = MinInterval(seconds=0.2)
    gate.wait()                        # first call is free
    start = time.monotonic()
    delayed = gate.wait()
    assert delayed > 0
    assert time.monotonic() - start >= 0.15


def test_cooldown_blocks_then_allows():
    cooldown = Cooldown(seconds=0.2)
    assert cooldown.remaining("chat1") == 0      # first use allowed
    assert cooldown.remaining("chat1") > 0       # immediate repeat blocked
    assert cooldown.remaining("chat2") == 0      # independent per key
    time.sleep(0.25)
    assert cooldown.remaining("chat1") == 0


# -- wiring --------------------------------------------------------------------

def test_flight_list_is_memoised_and_spaced(monkeypatch):
    sp._LIST_MEMO.clear()
    calls: list[str] = []

    class Response:
        status_code = 200

        @staticmethod
        def json():
            return {"result": {"response": {"data": [{"x": 1}]}}}

    def fake_get(url, **kwargs):
        calls.append(kwargs["params"]["query"])
        return Response()

    monkeypatch.setattr(sp.curl_requests, "get", fake_get)
    monkeypatch.setattr(sp, "_LIST_SPACING", MinInterval(seconds=0))

    assert sp.fetch_flight_list("N265AK") == [{"x": 1}]
    assert sp.fetch_flight_list("n265ak") == [{"x": 1}]   # case-insensitive hit
    assert sp.fetch_flight_list("N265AK", fetch_by="flight") == [{"x": 1}]
    assert calls == ["N265AK", "N265AK"]  # 2 fetches: the repeat was memoised
    sp._LIST_MEMO.clear()


def test_failed_flight_list_is_not_memoised(monkeypatch):
    """A transient outage must not be cached as 'no data' for five minutes."""
    sp._LIST_MEMO.clear()
    calls: list[str] = []

    def fake_get(url, **kwargs):
        calls.append("x")
        raise OSError("network down")

    monkeypatch.setattr(sp.curl_requests, "get", fake_get)
    monkeypatch.setattr(sp, "_LIST_SPACING", MinInterval(seconds=0))
    monkeypatch.setattr(sp.time, "sleep", lambda s: None)

    assert sp.fetch_flight_list("N265AK") is None
    assert sp.fetch_flight_list("N265AK") is None
    assert len(calls) == 2 * len(sp.IMPERSONATE_PROFILES)  # retried, not cached
    sp._LIST_MEMO.clear()


def test_telemetry_is_memoised(monkeypatch):
    adsb._TELEMETRY_MEMO.clear()
    calls: list[str] = []

    def fake_get_json(url, **kwargs):
        calls.append(url)
        return {"ac": [{"lat": 37.0, "lon": -122.0, "alt_baro": 30000,
                        "gs": 400, "flight": "ASA1 "}]}

    monkeypatch.setattr(adsb, "get_json", fake_get_json)
    first = adsb.fetch_telemetry("N265AK")
    second = adsb.fetch_telemetry("n265ak")

    assert first is second
    assert len(calls) == 1
    adsb._TELEMETRY_MEMO.clear()


def test_callsign_route_is_memoised(monkeypatch):
    adsb._ROUTE_MEMO.clear()
    calls: list[str] = []

    def fake_get_json(url, **kwargs):
        calls.append(url)
        return {"response": {"flightroute": {
            "callsign_iata": "AS237",
            "origin": {"iata_code": "SEA"},
            "destination": {"iata_code": "LIH"},
        }}}

    monkeypatch.setattr(adsb, "get_json", fake_get_json)
    assert adsb.resolve_callsign_route("ASA237") == ("SEA", "LIH", "AS237")
    assert adsb.resolve_callsign_route("ASA237") == ("SEA", "LIH", "AS237")
    assert len(calls) == 1
    adsb._ROUTE_MEMO.clear()
