"""Watch mode synthesis, callsign routing, schedule cache, nearest airport, history."""

import json
from datetime import datetime, timedelta, timezone

import livery_tracker.adsb as adsb
import livery_tracker.airports as airports
from livery_tracker.adsb import Telemetry
from livery_tracker.airports import Airport
from livery_tracker.config import Config, data_dir
from livery_tracker.flights import EventState, EventType, FlightEvent, append_history
from livery_tracker.schedule_provider import cache_rows, load_cached_rows
from livery_tracker.tracker import synthesize_watch_events

NOW = datetime(2026, 7, 26, 22, 0, tzinfo=timezone.utc)


def make_config() -> Config:
    config = Config()
    config.target_airports["SFO"] = {
        "icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748,
    }
    config.target_airports["LAX"] = {
        "icao": "KLAX", "name": "Los Angeles", "lat": 33.9425, "lon": -118.4080,
    }
    return config


def airborne(lat, lon, alt=32000, gs=420.0) -> Telemetry:
    return Telemetry(lat=lat, lon=lon, alt_ft=alt, on_ground=False, gs_kts=gs,
                     baro_rate=0.0, callsign="ASA1052", source="test")


# -- synthesize_watch_events -------------------------------------------------

def test_watch_synthesizes_live_arrival_with_estimated_eta():
    # Cruising near Fresno, inbound SEA->SFO: only the arrival leg matches.
    tele = airborne(36.7, -119.8)
    events = synthesize_watch_events(
        "N265AK", "Livery", tele, "SEA", "SFO", "AS1052", make_config(), NOW
    )
    assert len(events) == 1
    ev = events[0]
    assert ev.type == EventType.ARRIVAL and ev.status == EventState.LIVE
    assert ev.id == "N265AK-ARR-AS1052-20260726-SFO"
    # ~130 NM at 420 kts -> ETA well under an hour but after "now"
    assert NOW < ev.scheduled_time < NOW + timedelta(hours=1)


def test_watch_synthesizes_both_legs_between_watched_airports():
    tele = airborne(35.8, -120.5)  # mid-route SFO->LAX
    events = synthesize_watch_events(
        "N265AK", "", tele, "SFO", "LAX", "AS1052", make_config(), NOW
    )
    types = {e.type for e in events}
    assert types == {EventType.ARRIVAL, EventType.DEPARTURE}
    dep = next(e for e in events if e.type == EventType.DEPARTURE)
    # Already high and far from SFO: the departure is concluded immediately.
    assert dep.status == EventState.DEPARTED
    assert "ADS-B watch" in dep.status_note


def test_watch_departure_still_climbing_stays_live():
    tele = airborne(37.65, -122.40, alt=4000, gs=250.0)  # just off SFO
    events = synthesize_watch_events(
        "N265AK", "", tele, "SFO", "PDX", "AS300", make_config(), NOW
    )
    assert len(events) == 1
    assert events[0].type == EventType.DEPARTURE
    assert events[0].status == EventState.LIVE


def test_watch_ignores_unwatched_routes():
    tele = airborne(40.0, -100.0)
    events = synthesize_watch_events(
        "N265AK", "", tele, "ORD", "DEN", "AS1", make_config(), NOW
    )
    assert events == []


# -- adsbdb callsign routing --------------------------------------------------

def test_resolve_callsign_route_parses_response(monkeypatch):
    monkeypatch.setattr(adsb, "get_json", lambda url: {
        "response": {"flightroute": {
            "callsign_iata": "AS571",
            "origin": {"iata_code": "MCO"},
            "destination": {"iata_code": "SAN"},
        }}
    })
    assert adsb.resolve_callsign_route("ASA571") == ("MCO", "SAN", "AS571")


def test_resolve_callsign_route_handles_unknown_and_failure(monkeypatch):
    monkeypatch.setattr(adsb, "get_json", lambda url: {"response": "unknown callsign"})
    assert adsb.resolve_callsign_route("XXX123") is None
    monkeypatch.setattr(adsb, "get_json", lambda url: None)
    assert adsb.resolve_callsign_route("XXX123") is None


# -- schedule cache ------------------------------------------------------------

def test_schedule_cache_roundtrip_and_expiry():
    rows = [{"identification": {"number": {"default": "AS474"}}}]
    cache_rows("N265AK", rows)
    assert load_cached_rows("N265AK") == rows
    assert load_cached_rows("N999XX") is None

    # Age the entry past the 12h limit.
    path = data_dir() / "schedule_cache.json"
    cache = json.loads(path.read_text(encoding="utf-8"))
    cache["N265AK"]["fetched_at"] = (
        datetime.now(timezone.utc) - timedelta(hours=13)
    ).isoformat()
    path.write_text(json.dumps(cache), encoding="utf-8")
    assert load_cached_rows("N265AK") is None


# -- nearest airport -----------------------------------------------------------

def test_nearest_airport_respects_rank(monkeypatch):
    smf = Airport(icao="KSMF", iata="SMF", name="Sacramento Intl", lat=38.6954,
                  lon=-121.5908, rank=4)
    strip = Airport(icao="XXXX", iata="", name="Tiny Strip", lat=38.70, lon=-121.59, rank=2)
    monkeypatch.setattr(airports, "_index", {"SMF": smf, "KSMF": smf, "XXXX": strip})

    found = airports.nearest(38.70, -121.59)  # right on top of the tiny strip
    assert found is smf  # rank filter excludes the strip


# -- history log ----------------------------------------------------------------

def test_append_history_writes_jsonl():
    ev = FlightEvent(
        id="h1", tail="N265AK", livery="", type=EventType.ARRIVAL, target_airport="SFO",
        scheduled_time=NOW, status=EventState.LANDED,
    )
    append_history(ev)
    append_history(ev)
    lines = (data_dir() / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    entry = json.loads(lines[0])
    assert entry["id"] == "h1" and entry["status"] == "LANDED"
    assert "finalized_at" in entry
