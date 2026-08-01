"""The hourly mirror sync: pending legs always match the source.

This replaced the per-leg T-2h refresh and the swap cascade. Every pending
leg of every tail is reconciled each hour, so a stale sibling cannot outlive
the next pass, a leg the source no longer lists is withdrawn, and a failed
fetch marks legs "unverified" instead of dropping them.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


class FakeDigest:
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def make_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    config.watchlist = {"N265AK": {"airline": "Alaska", "livery": "More to Love"}}
    return config


def pending_leg(**overrides) -> FlightEvent:
    defaults = dict(
        id="arr", tail="N265AK", livery="More to Love", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=NOW + timedelta(hours=5),
        route_origin="SEA", route_destination="SFO", flight_number="AS1052",
        status=EventState.WAITING_2H,
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


def run_sync(app, monkeypatch, *, rows, refresh=None, telemetry=None):
    monkeypatch.setattr(
        tracker.schedule_provider, "fetch_flight_list", lambda q, fetch_by="reg": rows
    )
    monkeypatch.setattr(tracker.schedule_provider, "cache_rows", lambda reg, r: None)
    if refresh is not None:
        monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", refresh)
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: telemetry)
    monkeypatch.setattr(tracker.airport_db, "nearest", lambda lat, lon: None)
    return asyncio.run(tracker.run_schedule_sync(app))


def app_with(*events) -> tuple[FakeApp, FlightStore]:
    store = FlightStore()
    for ev in events:
        store.upsert(ev)
    return FakeApp(store, make_config()), store


# -- mirroring the source ------------------------------------------------------

def test_sync_adopts_the_sources_time_and_delay_figure(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)
    new_time = leg.scheduled_time + timedelta(minutes=30)

    counts = run_sync(
        app, monkeypatch, rows=[],
        refresh=lambda reg, ev: LegRefresh(new_time, delay_minutes=30),
    )

    survivor = store.get("arr")
    assert survivor.scheduled_time == new_time
    assert survivor.status_note == "delayed 30m"
    assert survivor.status == EventState.WAITING_2H
    assert counts == {"tails": 1, "updated": 1, "withdrawn": 0, "cancelled": 0,
                      "unverified": 0, "discovered": 0, "conflicts": 0}


def test_sync_withdraws_a_leg_the_source_no_longer_lists(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)

    counts = run_sync(app, monkeypatch, rows=[], refresh=lambda reg, ev: LegRefresh(None))

    survivor = store.get("arr")
    assert survivor.status == EventState.SWAPPED
    assert survivor.status_note == tracker.WITHDRAWN_NOTE
    assert survivor.scheduled_time == leg.scheduled_time, "never borrow a time"
    assert counts["withdrawn"] == 1


def test_sync_labels_a_confirmed_swap(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(None, swapped=True))

    assert store.get("arr").status == EventState.SWAPPED
    assert store.get("arr").status_note == "now flown by another aircraft"


def test_sync_cancels_what_the_source_cancels(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(ev.scheduled_time, cancelled=True))

    assert store.get("arr").status == EventState.CANCELLED


def test_whole_rotation_reconciled_in_one_pass(monkeypatch):
    """What the old swap cascade did, the sync now does by construction."""
    arr = pending_leg()
    dep = pending_leg(
        id="dep", type=EventType.DEPARTURE, scheduled_time=NOW + timedelta(hours=8),
        route_origin="SFO", route_destination="SEA", flight_number="AS1053",
    )
    app, store = app_with(arr, dep)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(None, swapped=True))

    assert store.get("arr").status == EventState.SWAPPED
    assert store.get("dep").status == EventState.SWAPPED
    assert store.active() == []


# -- source failure: inconclusive is not gone ---------------------------------

def test_sync_failure_marks_legs_unverified_and_keeps_them(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)

    counts = run_sync(app, monkeypatch, rows=None)

    survivor = store.get("arr")
    assert survivor.status == EventState.WAITING_2H, "a fetch failure never drops a leg"
    assert survivor.status_note == tracker.UNVERIFIED_NOTE
    assert counts["unverified"] == 1


def test_next_healthy_sync_clears_the_unverified_note(monkeypatch):
    leg = pending_leg(status_note=tracker.UNVERIFIED_NOTE)
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(ev.scheduled_time))

    assert store.get("arr").status_note == ""


# -- live legs belong to ADS-B... once ADS-B has actually seen them -------------

def test_sync_never_touches_a_live_leg_with_telemetry(monkeypatch):
    leg = pending_leg(status=EventState.LIVE)
    leg.last_telemetry = {"lat": 37.0, "lon": -122.0, "alt": 12000}
    app, store = app_with(leg)

    def boom(query, fetch_by="reg"):
        raise AssertionError("sync must not fetch for a tail with only seen live legs")

    monkeypatch.setattr(tracker.schedule_provider, "fetch_flight_list", boom)
    counts = asyncio.run(tracker.run_schedule_sync(app))

    assert counts["tails"] == 0
    assert store.get("arr").status == EventState.LIVE


def test_sync_mirrors_a_late_delay_onto_a_dark_live_leg(monkeypatch):
    """The N24988 case: a big delay published after T-1h, while the aircraft
    sits dark at its gate. The mirror keeps custody until first contact."""
    leg = pending_leg(
        type=EventType.DEPARTURE, status=EventState.LIVE,
        scheduled_time=NOW + timedelta(minutes=30),
        route_origin="SFO", route_destination="ICN",
    )
    app, store = app_with(leg)
    new_time = NOW + timedelta(hours=4)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(new_time, delay_minutes=240))

    survivor = store.get("arr")
    assert survivor.scheduled_time == new_time
    assert survivor.status == EventState.WAITING_LIVE, \
        "pushed out of its live window: back to the mirror until T-1h again"
    assert "delayed 240m" in survivor.status_note


def test_sync_keeps_a_dark_live_leg_live_for_a_small_delay(monkeypatch):
    leg = pending_leg(
        type=EventType.DEPARTURE, status=EventState.LIVE,
        scheduled_time=NOW + timedelta(minutes=10),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)
    new_time = NOW + timedelta(minutes=40)  # still inside the T-1h window

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(new_time, delay_minutes=30))

    survivor = store.get("arr")
    assert survivor.status == EventState.LIVE, "still due soon — keep polling"
    assert survivor.scheduled_time == new_time


def test_sync_concludes_a_dark_live_leg_the_source_records_as_flown(monkeypatch):
    leg = pending_leg(
        type=EventType.DEPARTURE, status=EventState.LIVE,
        scheduled_time=NOW - timedelta(minutes=20),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(
                 ev.scheduled_time, completed=True,
                 real_time=NOW - timedelta(minutes=5),
             ))

    survivor = store.get("arr")
    assert survivor.status == EventState.DEPARTED
    assert "per source" in survivor.status_note


def test_sync_does_not_withdraw_a_dark_live_leg_at_departure_time(monkeypatch):
    """Sources briefly unlist flights around pushback; absence only counts
    once the leg is well overdue."""
    leg = pending_leg(
        type=EventType.DEPARTURE, status=EventState.LIVE,
        scheduled_time=NOW - timedelta(minutes=10),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(None, swapped=True))

    assert store.get("arr").status == EventState.LIVE, "10 minutes late proves nothing"


# -- free discovery from the same rows ----------------------------------------

def test_sync_discovers_new_legs_from_the_fetched_rows(monkeypatch):
    leg = pending_leg()
    app, store = app_with(leg)
    when = NOW + timedelta(hours=9)
    new_row = {
        "identification": {"number": {"default": "AS1099"}},
        "airport": {
            "origin": {"code": {"iata": "PDX", "icao": "KPDX"}},
            "destination": {"code": {"iata": "SFO", "icao": "KSFO"}},
        },
        "time": {"scheduled": {"arrival": int(when.timestamp()),
                               "departure": int((when - timedelta(hours=2)).timestamp())}},
        "status": {"generic": {"status": {"text": "scheduled"}}},
    }

    counts = run_sync(app, monkeypatch, rows=[new_row],
                      refresh=lambda reg, ev: LegRefresh(ev.scheduled_time))

    assert counts["discovered"] == 1
    new_ids = [i for i in store.events if i != "arr"]
    assert len(new_ids) == 1
    assert store.get(new_ids[0]).flight_number == "AS1099"


# -- position sanity: the DFW-instead-of-SFO problem --------------------------

def on_ground_at(lat, lon) -> Telemetry:
    return Telemetry(lat=lat, lon=lon, alt_ft=0, on_ground=True, gs_kts=0.0,
                     baro_rate=None, callsign="", source="test")


def test_sync_holds_a_departure_whose_aircraft_is_visibly_elsewhere(monkeypatch):
    leg = pending_leg(
        type=EventType.DEPARTURE, scheduled_time=NOW + timedelta(minutes=90),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(ev.scheduled_time),
             telemetry=on_ground_at(32.8998, -97.0403))  # parked at DFW

    survivor = store.get("arr")
    assert survivor.status == EventState.TURNAROUND_DELAY
    assert "awaiting schedule update" in survivor.status_note


def test_aircraft_on_the_ground_at_its_own_airport_is_no_conflict(monkeypatch):
    leg = pending_leg(
        type=EventType.DEPARTURE, scheduled_time=NOW + timedelta(minutes=90),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(ev.scheduled_time),
             telemetry=on_ground_at(37.6213, -122.3790))  # at its SFO gate

    assert store.get("arr").status == EventState.WAITING_2H


def test_a_dark_transponder_is_not_positive_evidence(monkeypatch):
    leg = pending_leg(
        type=EventType.DEPARTURE, scheduled_time=NOW + timedelta(minutes=90),
        route_origin="SFO", route_destination="SEA",
    )
    app, store = app_with(leg)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(ev.scheduled_time), telemetry=None)

    assert store.get("arr").status == EventState.WAITING_2H


# -- the 15-minute hot lane ----------------------------------------------------

def test_hot_sync_only_touches_tails_with_an_imminent_leg(monkeypatch):
    soon = pending_leg(scheduled_time=NOW + timedelta(minutes=70))
    later = pending_leg(
        id="later", tail="N985AK", flight_number="AS656",
        scheduled_time=NOW + timedelta(hours=6),
    )
    app, store = app_with(soon, later)
    fetched: list[str] = []

    def spy_fetch(query, fetch_by="reg"):
        fetched.append(query)
        return []

    monkeypatch.setattr(tracker.schedule_provider, "fetch_flight_list", spy_fetch)
    monkeypatch.setattr(tracker.schedule_provider, "cache_rows", lambda reg, r: None)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, ev: LegRefresh(ev.scheduled_time),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)
    monkeypatch.setattr(tracker.airport_db, "nearest", lambda lat, lon: None)

    counts = asyncio.run(tracker.run_schedule_sync(app, hot_only=True))
    assert fetched == ["N265AK"], "only the tail with a leg due within 2h"
    assert counts["tails"] == 1

    fetched.clear()
    asyncio.run(tracker.run_schedule_sync(app))
    assert sorted(fetched) == ["N265AK", "N985AK"], "the full pass covers everyone"


# -- the turnaround guard survives the mirror ---------------------------------

def test_sync_never_withdraws_a_leg_held_by_the_turnaround_guard(monkeypatch):
    """During a known-faulty source window (N475UA: landing recorded after the
    outbound ETD), an absent row is FR24 being confused, not a real swap."""
    inbound = pending_leg(
        id="inbound", status=EventState.LANDED,
        scheduled_time=NOW - timedelta(minutes=90),
        last_telemetry={"seen_at": NOW.isoformat(), "lat": 37.62, "lon": -122.37},
    )
    outbound = pending_leg(
        id="outbound", type=EventType.DEPARTURE,
        scheduled_time=NOW - timedelta(minutes=30),
        route_origin="SFO", route_destination="SEA", flight_number="AS1053",
        status=EventState.TURNAROUND_DELAY,
        status_note=tracker.TURNAROUND_CONFLICT_NOTE,
    )
    app, store = app_with(inbound, outbound)

    run_sync(app, monkeypatch, rows=[],
             refresh=lambda reg, ev: LegRefresh(None, swapped=True))

    assert store.get("outbound").status == EventState.TURNAROUND_DELAY
