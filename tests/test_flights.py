from datetime import datetime, timezone

from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore


def make_event(**overrides) -> FlightEvent:
    when = datetime(2026, 7, 26, 22, 45, tzinfo=timezone.utc)
    defaults = dict(
        id=FlightEvent.make_id("N265AK", EventType.ARRIVAL, when, "SFO"),
        tail="N265AK",
        livery="More to Love",
        type=EventType.ARRIVAL,
        target_airport="SFO",
        scheduled_time=when,
        route_origin="SEA",
        route_destination="SFO",
        flight_number="AS1234",
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


def test_event_roundtrip():
    ev = make_event(status=EventState.LIVE, status_note="Callsign: ASA1234")
    ev.last_telemetry = {"lat": 37.0, "lon": -122.0, "alt": 12400, "gs": 310.0, "dist_nm": 48.2}
    restored = FlightEvent.from_dict(ev.to_dict())
    assert restored == ev


def test_remove_where():
    store = FlightStore()
    store.upsert(make_event())
    other = make_event(id="N711HK-DEP-x-SFO", tail="N711HK", type=EventType.DEPARTURE)
    store.upsert(other)

    removed = store.remove_where(lambda ev: ev.tail == "N265AK")
    assert [ev.tail for ev in removed] == ["N265AK"]
    assert list(FlightStore().events) == ["N711HK-DEP-x-SFO"]


def test_store_persistence_and_rehydration():
    store = FlightStore()
    ev = make_event(status=EventState.WAITING_2H)
    store.upsert(ev)

    fresh = FlightStore()  # simulates a process restart
    assert fresh.get(ev.id) is not None
    assert fresh.get(ev.id).status == EventState.WAITING_2H
    assert len(fresh.active()) == 1

    ev.status = EventState.LANDED
    fresh.upsert(ev)
    assert FlightStore().active() == []


def test_terminal_states():
    assert EventState.LANDED.terminal
    assert EventState.DEPARTED.terminal
    assert EventState.LOST.terminal
    assert not EventState.WAITING_2H.terminal
    assert not EventState.LIVE.terminal


def test_config_roundtrip_and_code_matching():
    config = Config()
    config.target_airports["SFO"] = {
        "icao": "KSFO",
        "name": "San Francisco International Airport",
        "lat": 37.6213,
        "lon": -122.379,
    }
    config.watchlist["N265AK"] = {"airline": "Alaska", "model": "B739", "livery": "More to Love"}
    config.save()

    loaded = Config.load()
    assert loaded.target_airports == config.target_airports
    assert loaded.watchlist == config.watchlist
    assert loaded.airport_codes() == {"SFO", "KSFO"}
    assert loaded.airport_for_code("ksfo")[0] == "SFO"
    assert loaded.airport_for_code("SFO")[0] == "SFO"
    assert loaded.airport_for_code("LAX") is None
