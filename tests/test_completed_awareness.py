"""Completed-awareness: the poll reads whether the source says a leg already ran.

The WN3982 case: the arrival landed 10:51, a rebuild re-created the leg, and
the aircraft at its gate was already squawking the next flight's callsign.
"Still listed by the source" must not be read as "still coming" when the row
itself says landed. Rebuilds also stop rewriting the past: directly observed
conclusions survive, derived verdicts are re-derived.
"""

import asyncio
import re
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


# -- the early no-show source check (ETD+10m, not +30m) -------------------------

def test_dark_leg_mirrors_a_delay_soon_after_its_time(monkeypatch):
    """A leg 15 minutes overdue with no ADS-B contact asks the source right
    away — a late-published delay reaches the digest within minutes."""
    store, config = FlightStore(), sfo_config()
    leg = make_leg(
        EventType.DEPARTURE, EventState.LIVE,
        when=NOW - timedelta(minutes=15), number="UA893",
    )
    store.upsert(leg)
    app = FakeApp(store, config)
    new_time = NOW + timedelta(hours=3)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(new_time, delay_minutes=195),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.WAITING_LIVE
    assert survivor.scheduled_time == new_time
    assert "delayed" in survivor.status_note


def test_dark_leg_absence_is_ignored_before_the_older_deadline(monkeypatch):
    """Sources briefly unlist flights around pushback: a swapped/absent answer
    at 15 minutes late must annotate, not withdraw."""
    store, config = FlightStore(), sfo_config()
    leg = make_leg(
        EventType.DEPARTURE, EventState.LIVE,
        when=NOW - timedelta(minutes=15), number="UA893",
    )
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(None, swapped=True),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.LIVE, "15 minutes late proves nothing"
    assert "likely delayed" in survivor.status_note


# -- a visible, overdue, parked departure re-checks the source ------------------

def parked_at_sfo(callsign="UAL115") -> Telemetry:
    return Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                     gs_kts=1.0, baro_rate=None, callsign=callsign, source="test")


def overdue_departure(store) -> FlightEvent:
    leg = make_leg(
        EventType.DEPARTURE, EventState.LIVE,
        when=NOW - timedelta(minutes=20), number="UA115",
    )
    store.upsert(leg)
    return leg


def test_big_gate_delay_stands_polling_down(monkeypatch):
    """A seen aircraft's leg must not ride a stale clock into a false LOST:
    a delay that leaves the live window hands the leg back to the mirror."""
    store, config = FlightStore(), sfo_config()
    leg = overdue_departure(store)
    app = FakeApp(store, config)
    new_time = NOW + timedelta(hours=2)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(new_time, delay_minutes=140),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: parked_at_sfo())

    ctx = context_for(app, leg)
    asyncio.run(tracker.job_poll(ctx))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.WAITING_LIVE
    assert survivor.scheduled_time == new_time
    assert survivor.status_note == "delayed 140m"
    assert ctx.job.schedule_removal.called, "polling must stand down"


def test_small_gate_delay_keeps_polling_against_the_new_time(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = overdue_departure(store)
    app = FakeApp(store, config)
    new_time = NOW + timedelta(minutes=30)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(new_time, delay_minutes=50),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: parked_at_sfo())

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.LIVE, "still inside the live window"
    assert survivor.scheduled_time == new_time
    assert survivor.status_note == "delayed 50m"


def test_late_cancellation_reaches_a_seen_live_leg(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = overdue_departure(store)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time, cancelled=True),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: parked_at_sfo())

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    assert store.get(leg.id).status == EventState.CANCELLED


def test_no_source_change_keeps_the_honest_lateness_note(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = overdue_departure(store)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: parked_at_sfo())

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.LIVE
    # The minute count rides the real clock (20 or 21 depending on when the
    # suite reaches this test), so match the shape rather than the number.
    assert re.fullmatch(r"running 2\dm late — still on the ground", survivor.status_note)
    assert survivor.scheduled_time == NOW - timedelta(minutes=20), "time untouched"


# -- a late airborne arrival mirrors the source's live ETA ----------------------

def airborne_inbound(callsign="UAL2077") -> Telemetry:
    return Telemetry(lat=42.0, lon=-122.0, alt_ft=39_000, on_ground=False,
                     gs_kts=439.0, baro_rate=0, callsign=callsign, source="test")


def test_late_airborne_arrival_mirrors_the_sources_eta(monkeypatch):
    """The N14219 case: ETA fossilized at go-live while the flight departed
    late — the digest showed 3:35 with the aircraft still 182 NM out."""
    store, config = FlightStore(), sfo_config()
    leg = make_leg(
        EventType.ARRIVAL, EventState.LIVE,
        when=NOW - timedelta(minutes=20), number="UA2077",
    )
    store.upsert(leg)
    app = FakeApp(store, config)
    new_eta = NOW + timedelta(minutes=25)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(new_eta, delay_minutes=37),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: airborne_inbound())

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.LIVE, "still inbound — polling continues"
    assert survivor.scheduled_time == new_eta
    assert survivor.status_note == "delayed 37m"


def test_late_airborne_arrival_with_no_source_news_keeps_the_late_note(monkeypatch):
    store, config = FlightStore(), sfo_config()
    leg = make_leg(
        EventType.ARRIVAL, EventState.LIVE,
        when=NOW - timedelta(minutes=20), number="UA2077",
    )
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: airborne_inbound())

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert re.fullmatch(r"running 2\dm late", survivor.status_note)
    assert survivor.scheduled_time == NOW - timedelta(minutes=20), "time untouched"


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
