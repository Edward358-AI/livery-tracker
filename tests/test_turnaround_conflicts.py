"""Regression coverage for delayed turnarounds with contradictory schedules."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh, rows_to_events


NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


class FakeDigest:
    async def refresh(self):
        pass


class FakeApp:
    def __init__(self, store: FlightStore, config: Config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def sfo_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    return config


def landed_inbound() -> FlightEvent:
    return FlightEvent(
        id="arrival", tail="N475UA", livery="Retro", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=NOW - timedelta(minutes=90),
        route_origin="SEA", route_destination="SFO", flight_number="UA2164",
        status=EventState.LANDED,
        last_telemetry={"seen_at": NOW.isoformat(), "lat": 37.62, "lon": -122.37},
    )


def impossible_outbound(status: EventState = EventState.WAITING_LIVE) -> FlightEvent:
    return FlightEvent(
        id="departure", tail="N475UA", livery="Retro", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=NOW - timedelta(minutes=30),
        route_origin="SFO", route_destination="SEA", flight_number="UA1007", status=status,
    )


def context_for(app: FakeApp, event: FlightEvent):
    return SimpleNamespace(
        application=app,
        job=SimpleNamespace(data=event.id, schedule_removal=MagicMock()),
    )


def test_live_start_waits_when_outbound_estimate_predates_recorded_arrival(monkeypatch):
    """A stale 9 PM ETD cannot supersede an arrival recorded at 9:53 PM."""
    store, config = FlightStore(), sfo_config()
    inbound, outbound = landed_inbound(), impossible_outbound()
    store.upsert(inbound)
    store.upsert(outbound)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time", lambda reg, event: LegRefresh(event.scheduled_time)
    )

    asyncio.run(tracker.job_live_start(context_for(app, outbound)))

    current = store.get(outbound.id)
    assert current.status.value == "TURNAROUND_DELAY"
    assert current.status_note == "Awaiting turnaround / source conflict"


def test_live_start_waits_for_an_active_inbound_with_a_later_eta(monkeypatch):
    """The conflict starts before touchdown, so it cannot become a false swap."""
    store, config = FlightStore(), sfo_config()
    inbound = landed_inbound()
    inbound.status = EventState.LIVE
    inbound.scheduled_time = NOW + timedelta(minutes=40)
    inbound.last_telemetry = {}
    outbound = impossible_outbound()
    store.upsert(inbound)
    store.upsert(outbound)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time", lambda reg, event: LegRefresh(event.scheduled_time)
    )

    asyncio.run(tracker.job_live_start(context_for(app, outbound)))

    assert store.get(outbound.id).status.value == "TURNAROUND_DELAY"


def test_turnaround_wait_ignores_inbound_callsign_until_own_flight_takes_off(monkeypatch):
    """The still-inbound UAL2164 must not swap UA1007 off the watched tail."""
    store, config = FlightStore(), sfo_config()
    inbound = landed_inbound()
    outbound = impossible_outbound(EventState.LIVE)
    store.upsert(inbound)
    store.upsert(outbound)
    app = FakeApp(store, config)
    ctx = context_for(app, outbound)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time", lambda reg, event: LegRefresh(event.scheduled_time)
    )

    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(
            lat=37.62, lon=-122.37, alt_ft=0, on_ground=True, gs_kts=0,
            baro_rate=None, callsign="UAL2164", source="test",
        ),
    )
    asyncio.run(tracker.job_poll(ctx))
    assert store.get(outbound.id).status.value == "TURNAROUND_DELAY"

    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(
            lat=37.88, lon=-122.19, alt_ft=12_000, on_ground=False, gs_kts=340,
            baro_rate=1_200, callsign="UAL1007", source="test",
        ),
    )
    asyncio.run(tracker.job_poll(ctx))
    assert store.get(outbound.id).status == EventState.DEPARTED


def test_rebuild_harvest_retains_recently_overdue_estimated_departure():
    """A stale but explicitly estimated departure stays discoverable for six hours."""
    now = NOW
    stamp = lambda when: int(when.timestamp())
    row = {
        "identification": {"number": {"default": "UA1007"}},
        "airport": {
            "origin": {"code": {"iata": "SFO", "icao": "KSFO"}},
            "destination": {"code": {"iata": "SEA", "icao": "KSEA"}},
        },
        "time": {
            "scheduled": {"departure": stamp(now - timedelta(hours=2))},
            "estimated": {"departure": stamp(now - timedelta(minutes=90))},
            "real": {"departure": None},
        },
        "status": {"generic": {"status": {"text": "estimated", "type": "departure"}}},
    }

    events = rows_to_events("N475UA", "Retro", [row], sfo_config(), now=now)

    assert [(event.flight_number, event.type) for event in events] == [("UA1007", EventType.DEPARTURE)]
