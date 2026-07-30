"""A confirmed aircraft swap must end the leg, not let it run to a false outcome.

The UA1116/N475UA incident had two halves. The first (adopting another
flight's time as a delay) is covered in test_leg_matching.py. This covers the
second: the ghost leg surviving detection and then reporting a departure or
landing based on whatever the aircraft actually flew.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.config import Config
from livery_tracker.digest import render_digest
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh

NOW = datetime(2026, 7, 27, 20, 20, tzinfo=timezone.utc)


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
    config.target_airports = {"SFO": {"icao": "KSFO", "name": "SF", "lat": 37.6, "lon": -122.4}}
    config.watchlist = {"N475UA": {"airline": "United", "livery": "Retro"}}
    return config


def ghost_leg() -> FlightEvent:
    return FlightEvent(
        id="N475UA-DEP-202607272042-SFO", tail="N475UA", livery="Retro",
        type=EventType.DEPARTURE, target_airport="SFO", scheduled_time=NOW,
        route_origin="SFO", route_destination="AUS", flight_number="UA1116",
        status=EventState.WAITING_2H,
    )


def run_refresh(app, event, refresh_result):
    """Drive the hourly sync (the T-2h refresh's replacement) with a canned answer."""
    import livery_tracker.schedule_provider as sp

    originals = (
        sp.fetch_flight_list, sp.refresh_leg_time, sp.cache_rows, tracker.fetch_telemetry
    )
    sp.fetch_flight_list = lambda q, fetch_by="reg": []
    sp.refresh_leg_time = lambda reg, ev: refresh_result
    sp.cache_rows = lambda reg, rows: None
    tracker.fetch_telemetry = lambda reg: None
    try:
        asyncio.run(tracker.run_schedule_sync(app))
    finally:
        (sp.fetch_flight_list, sp.refresh_leg_time, sp.cache_rows,
         tracker.fetch_telemetry) = originals


def test_swapped_leg_becomes_terminal_and_stops_tracking():
    store = FlightStore()
    event = ghost_leg()
    store.upsert(event)
    app = FakeApp(store, make_config())

    run_refresh(app, event, LegRefresh(None, swapped=True))

    survivor = store.get(event.id)
    assert survivor.status == EventState.SWAPPED
    assert survivor.status.terminal, "a swapped leg must never reach live polling"
    assert store.active() == []


def test_swapped_leg_keeps_its_original_time():
    """No time is ever borrowed from the flight's new operator."""
    store = FlightStore()
    event = ghost_leg()
    store.upsert(event)
    app = FakeApp(store, make_config())

    run_refresh(app, event, LegRefresh(None, swapped=True))
    assert store.get(event.id).scheduled_time == NOW


def test_normal_delay_still_proceeds_to_live_tracking():
    """The swap path must not swallow ordinary delays.

    The delay figure comes from the source's own scheduled-vs-estimated
    pair, carried on LegRefresh, rather than from drift against whatever we
    happened to store last. The sync mirrors it and the leg stays pending
    for its T-1h live start.
    """
    store = FlightStore()
    event = ghost_leg()
    store.upsert(event)
    app = FakeApp(store, make_config())

    run_refresh(app, event, LegRefresh(NOW + timedelta(minutes=20), delay_minutes=20))

    survivor = store.get(event.id)
    assert not survivor.status.terminal
    assert survivor.scheduled_time == NOW + timedelta(minutes=20)
    assert "delayed 20m" in survivor.status_note


def test_digest_explains_a_swapped_leg():
    store = FlightStore()
    event = ghost_leg()
    event.status = EventState.SWAPPED
    event.status_note = "now flown by another aircraft"
    store.upsert(event)

    text = render_digest(store, make_config())
    assert "🔀" in text
    assert "aircraft swapped off this flight" in text
    assert "delayed" not in text


def test_swap_is_recorded_in_history(tmp_path):
    store = FlightStore()
    event = ghost_leg()
    store.upsert(event)
    app = FakeApp(store, make_config())

    run_refresh(app, event, LegRefresh(None, swapped=True))

    from livery_tracker.config import data_dir
    lines = (data_dir() / "history.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert any('"SWAPPED"' in line for line in lines)
