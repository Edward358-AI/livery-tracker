"""Completed-awareness: the poll reads whether the source says a leg already ran.

The WN3982 case: the arrival landed 10:51, a rebuild re-created the leg, and
the aircraft at its gate was already squawking the next flight's callsign.
"Still listed by the source" must not be read as "still coming" when the row
itself says landed. Rebuilds also stop rewriting the past: directly observed
conclusions survive, derived verdicts are re-derived.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.schedule_provider as sp
import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


class FakeDigest:
    async def refresh(self):
        pass


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def sfo_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    return config


def context_for(app, event):
    return SimpleNamespace(
        application=app,
        job=SimpleNamespace(data=event.id, schedule_removal=MagicMock()),
    )


def make_leg(ev_type: EventType, status: EventState, when=None, number="WN3982") -> FlightEvent:
    origin, dest = ("STL", "SFO") if ev_type == EventType.ARRIVAL else ("SFO", "SAN")
    return FlightEvent(
        id="leg", tail="N8619F", livery="Illinois One", type=ev_type,
        target_airport="SFO", scheduled_time=when or (NOW - timedelta(minutes=90)),
        route_origin=origin, route_destination=dest, flight_number=number,
        status=status,
    )


# -- the provider reports completion from the row itself ------------------------

def landed_row(real_minutes_ago=60) -> dict:
    sched = NOW - timedelta(minutes=90)
    real = NOW - timedelta(minutes=real_minutes_ago)
    return {
        "identification": {"number": {"default": "WN3982"}},
        "airport": {
            "origin": {"code": {"iata": "STL"}},
            "destination": {"code": {"iata": "SFO"}},
        },
        "time": {
            "scheduled": {"arrival": int(sched.timestamp())},
            "estimated": {},
            "real": {"arrival": int(real.timestamp())},
        },
        "status": {"live": False, "generic": {"status": {"text": "landed"}}},
    }


def test_refresh_reports_completion_and_the_real_time(monkeypatch):
    monkeypatch.setattr(sp, "fetch_flight_list", lambda q, fetch_by="reg": [landed_row()])
    result = sp.refresh_leg_time("N8619F", make_leg(EventType.ARRIVAL, EventState.LIVE))
    assert result.completed is True
    assert result.real_time == NOW - timedelta(minutes=60)


def test_a_pending_row_is_not_completed(monkeypatch):
    row = landed_row()
    row["time"]["real"] = {}
    row["status"] = {"live": False, "generic": {"status": {"text": "estimated"}}}
    monkeypatch.setattr(sp, "fetch_flight_list", lambda q, fetch_by="reg": [row])
    result = sp.refresh_leg_time("N8619F", make_leg(EventType.ARRIVAL, EventState.LIVE))
    assert result.completed is False


def test_a_live_row_completes_a_departure_but_not_an_arrival():
    row = landed_row()
    row["time"]["real"] = {}
    row["status"] = {"live": True, "generic": {"status": {"text": "estimated"}}}
    done_dep, _ = sp._row_completed(row, "departure")
    done_arr, _ = sp._row_completed(row, "arrival")
    assert done_dep is True, "airborne means it certainly left its origin"
    assert done_arr is False, "airborne means it certainly has not arrived"


# -- the WN3982 replay: mismatch + landed row -----------------------------------

def test_mismatched_callsign_with_a_landed_row_concludes_landed(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = make_leg(EventType.ARRIVAL, EventState.LIVE)
    store.upsert(leg)
    app = FakeApp(store, config)
    landed_at = NOW - timedelta(minutes=60)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(
            event.scheduled_time, completed=True, real_time=landed_at
        ),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                              gs_kts=0.0, baro_rate=None, callsign="SWA3496", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.LANDED
    assert "per source" in survivor.status_note


# -- a held leg concludes once the source records completion --------------------

def test_held_leg_concludes_when_the_source_records_completion(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = make_leg(EventType.DEPARTURE, EventState.TURNAROUND_DELAY, number="WN3496")
    leg.status_note = tracker.TURNAROUND_CONFLICT_NOTE
    store.upsert(leg)
    app = FakeApp(store, config)
    departed_at = NOW - timedelta(minutes=10)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(
            event.scheduled_time, completed=True, real_time=departed_at
        ),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.DEPARTED
    assert "per source" in survivor.status_note


# -- a never-seen leg adopts the source's record instead of LOST ----------------

def test_never_seen_leg_adopts_the_sources_record_instead_of_lost(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = make_leg(
        EventType.DEPARTURE, EventState.LIVE,
        when=NOW - timedelta(minutes=40), number="WN3496",
    )
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(
            event.scheduled_time, completed=True,
            real_time=NOW - timedelta(minutes=25),
        ),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.DEPARTED, "a coverage gap is not LOST"
    assert "per source" in survivor.status_note


# -- rebuild keeps the observed past, re-derives the rest -----------------------

def test_rebuild_keeps_observed_conclusions_and_clears_derived_ones(monkeypatch):
    store = FlightStore()
    states = {
        "landed": EventState.LANDED,
        "diverted": EventState.DIVERTED,
        "swapped": EventState.SWAPPED,
        "lost": EventState.LOST,
        "waiting": EventState.WAITING_2H,
    }
    for name, status in states.items():
        leg = make_leg(EventType.ARRIVAL, status)
        leg.id = name
        store.upsert(leg)
    app = FakeApp(store, sfo_config())

    async def fake_harvest(application):
        return tracker.HarvestResult()

    monkeypatch.setattr(tracker, "_run_harvest_locked", fake_harvest)
    monkeypatch.setattr(tracker.schedule_provider, "clear_caches", lambda: 0)
    monkeypatch.setattr(tracker.adsb, "clear_caches", lambda: None)

    result = asyncio.run(tracker.rebuild_schedule(app))

    assert set(store.events) == {"landed", "diverted"}
    assert result.discarded_legs == 3
