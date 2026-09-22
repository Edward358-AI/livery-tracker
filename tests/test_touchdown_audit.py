"""Touchdown ground truth: FR24's seconds-precise wheels-down rides along in
history beside the interpolated stamp, so the estimator can be audited."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry, _trail_touchdown
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

NOW_TS = 1_660_000_000.0


def pt(ts, alt):
    return {"ts": ts, "alt": alt, "lat": 37.6, "lng": -122.4}


# -- reading the trail ----------------------------------------------------------

def test_trail_touchdown_finds_the_ground_transition():
    # Newest first: three ground fixes, then the approach. Touchdown is the
    # earliest fix of the leading ground run.
    trail = [pt(NOW_TS - 60, 0), pt(NOW_TS - 90, 0), pt(NOW_TS - 120, 0),
             pt(NOW_TS - 150, 300), pt(NOW_TS - 180, 800)]
    when = _trail_touchdown(trail, now_ts=NOW_TS)
    assert when == datetime.fromtimestamp(NOW_TS - 120, tz=timezone.utc)


def test_trail_still_airborne_yields_nothing():
    trail = [pt(NOW_TS - 10, 500), pt(NOW_TS - 40, 900)]
    assert _trail_touchdown(trail, now_ts=NOW_TS) is None


def test_trail_parked_all_along_proves_nothing():
    # No airborne point at all: could be yesterday's parking, not a landing.
    trail = [pt(NOW_TS - 60, 0), pt(NOW_TS - 120, 0)]
    assert _trail_touchdown(trail, now_ts=NOW_TS) is None


def test_trail_stale_ground_run_is_ignored():
    old = NOW_TS - 2 * 3600
    trail = [pt(old - 60, 0), pt(old - 120, 300)]
    assert _trail_touchdown(trail, now_ts=NOW_TS) is None


# -- wiring: the landed poll records both stamps --------------------------------

class FakeDigest:
    async def refresh(self):
        pass


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def test_landing_records_fr24_touchdown_for_the_audit_log(monkeypatch):
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    now = datetime.now(timezone.utc)
    event = FlightEvent(
        id="N265AK-ARR-SFO", tail="N265AK", livery="", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=now, route_origin="SEA",
        route_destination="SFO", flight_number="AS1234", status=EventState.LIVE,
    )
    store = FlightStore()
    store.upsert(event)
    app = FakeApp(store, config)

    truth = now - timedelta(seconds=95)
    monkeypatch.setattr(tracker.adsb, "fr24_touchdown_time", lambda reg: truth)
    at_the_field = Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                             gs_kts=15.0, baro_rate=None, callsign="ASA1234", source="t")
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: at_the_field)

    ctx = SimpleNamespace(
        application=app, job=SimpleNamespace(data=event.id, schedule_removal=lambda: None)
    )
    asyncio.run(tracker.job_poll(ctx))

    ev = store.get(event.id)
    assert ev.status == EventState.LANDED
    assert ev.last_telemetry["touchdown_fr24_at"] == truth.isoformat()
    assert "touchdown_at" in ev.last_telemetry  # the interpolated stamp stays
