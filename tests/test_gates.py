"""Gate/terminal display: mirrored from the source, omitted when silent.

Parsed from the airport.info block the rows always carried — zero extra
requests. Display-only: no tracking logic may depend on a gate.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import livery_tracker.schedule_provider as sp
import livery_tracker.tracker as tracker
from livery_tracker.config import Config
from livery_tracker.digest import format_leg
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh
from livery_tracker.tracker import _apply_schedule

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def sfo_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    config.watchlist = {"N265AK": {"airline": "Alaska", "livery": ""}}
    return config


def row_with_info(origin="SFO", dest="SEA", number="AS656"):
    when = NOW + timedelta(hours=5)
    return {
        "identification": {"number": {"default": number}},
        "airport": {
            "origin": {
                "code": {"iata": origin, "icao": ""},
                "info": {"terminal": "2", "gate": "D15", "baggage": None},
            },
            "destination": {
                "code": {"iata": dest, "icao": ""},
                "info": {"terminal": "B", "gate": "6"},
            },
        },
        "time": {
            "scheduled": {
                "departure": int(when.timestamp()),
                "arrival": int((when + timedelta(hours=2)).timestamp()),
            },
            "estimated": {},
        },
        "status": {"generic": {"status": {"text": "scheduled"}}},
    }


def make_leg(**overrides) -> FlightEvent:
    defaults = dict(
        id="leg", tail="N265AK", livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=NOW + timedelta(hours=5),
        route_origin="SFO", route_destination="SEA", flight_number="AS656",
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


# -- extraction picks the watched side -----------------------------------------

def test_discovery_takes_the_watched_airports_gate():
    departure_events = sp.rows_to_events("N265AK", "", [row_with_info()], sfo_config(), now=NOW)
    assert len(departure_events) == 1
    assert (departure_events[0].terminal, departure_events[0].gate) == ("2", "D15")

    arrival_events = sp.rows_to_events(
        "N265AK", "", [row_with_info(origin="SEA", dest="SFO")], sfo_config(), now=NOW
    )
    assert len(arrival_events) == 1
    assert (arrival_events[0].terminal, arrival_events[0].gate) == ("B", "6")


def test_refresh_carries_the_gate(monkeypatch):
    monkeypatch.setattr(sp, "fetch_flight_list", lambda q, fetch_by="reg": [row_with_info()])
    result = sp.refresh_leg_time("N265AK", make_leg())
    assert (result.terminal, result.gate) == ("2", "D15")


# -- mirror semantics: adopt, update, clear -------------------------------------

def test_apply_schedule_adopts_and_clears_gates():
    leg = make_leg(terminal="2", gate="D15")
    _apply_schedule(leg, LegRefresh(leg.scheduled_time, terminal="2", gate="C3"))
    assert leg.gate == "C3", "a new gate from the source replaces ours"

    _apply_schedule(leg, LegRefresh(leg.scheduled_time))
    assert leg.gate == "" and leg.terminal == "", "source silent -> gate omitted"


def test_sync_updates_on_a_gate_only_change(monkeypatch):
    store = FlightStore()
    leg = make_leg(terminal="2", gate="D15")
    store.upsert(leg)

    class FakeDigest:
        async def refresh(self):
            pass

    app = MagicMock()
    app.bot_data = {"store": store, "config": sfo_config(), "digest": FakeDigest()}
    app.job_queue.get_jobs_by_name.return_value = []
    monkeypatch.setattr(
        tracker.schedule_provider, "fetch_flight_list", lambda q, fetch_by="reg": []
    )
    monkeypatch.setattr(tracker.schedule_provider, "cache_rows", lambda reg, r: None)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, ev: LegRefresh(ev.scheduled_time, terminal="2", gate="C3"),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)
    monkeypatch.setattr(tracker.airport_db, "nearest", lambda lat, lon: None)

    counts = asyncio.run(tracker.run_schedule_sync(app))

    assert store.get("leg").gate == "C3"
    assert counts["updated"] == 1, "a gate change alone counts as an update"


# -- display: present when known, gone when not, gone at conclusion -------------

def test_digest_shows_gate_and_omits_when_absent():
    with_gate = make_leg(terminal="2", gate="D15")
    assert "ETD" in format_leg(with_gate)
    assert "· T2 D15" in format_leg(with_gate)

    alpha_terminal = make_leg(terminal="I", gate="A7")
    assert "· I A7" in format_leg(alpha_terminal)

    bare = make_leg()
    line = format_leg(bare)
    assert "T2" not in line and "·" not in line.split(",")[1]


def test_concluded_legs_do_not_show_a_gate():
    done = make_leg(terminal="2", gate="D15", status=EventState.DEPARTED)
    done.status_note = "3:43 PM PDT"
    assert "D15" not in format_leg(done)
