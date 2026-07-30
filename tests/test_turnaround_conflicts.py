"""Regression coverage for delayed turnarounds with contradictory schedules."""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.airports import Airport
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


def test_live_start_ignores_a_next_day_return_as_a_preceding_turnaround(monkeypatch):
    """Tomorrow's return to OAK cannot delay tonight's OAK departure."""
    store, config = FlightStore(), sfo_config()
    outbound = FlightEvent(
        id="y47791", tail="XA-VUS", livery="", type=EventType.DEPARTURE,
        target_airport="OAK", scheduled_time=NOW,
        route_origin="OAK", route_destination="MLM", flight_number="Y47791",
        status=EventState.WAITING_LIVE,
    )
    next_day_return = FlightEvent(
        id="y47790", tail="XA-VUS", livery="", type=EventType.ARRIVAL,
        target_airport="OAK", scheduled_time=NOW + timedelta(hours=22),
        route_origin="MLM", route_destination="OAK", flight_number="Y47790",
        status=EventState.WAITING_2H,
    )
    store.upsert(outbound)
    store.upsert(next_day_return)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time", lambda reg, event: LegRefresh(event.scheduled_time)
    )

    asyncio.run(tracker.job_live_start(context_for(app, outbound)))

    assert store.get(outbound.id).status == EventState.LIVE


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


AIRPORT_COORDS = {"SFO": (37.6198, -122.3748), "PHX": (33.4342, -112.0117)}


def fake_lookup(code):
    if code in AIRPORT_COORDS:
        lat, lon = AIRPORT_COORDS[code]
        return Airport(icao="K" + code, iata=code, name=code, lat=lat, lon=lon, rank=4)
    return None


def out_and_back(now=None):
    """The observed N27255 false positive: SFO->PHX held by its own return."""
    now = now or NOW
    outbound = FlightEvent(
        id="ua2374", tail="N27255", livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=now + timedelta(hours=1),
        route_origin="SFO", route_destination="PHX", flight_number="UA2374",
        status=EventState.WAITING_LIVE,
    )
    dependent_return = FlightEvent(
        id="ua2619", tail="N27255", livery="", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=now + timedelta(hours=5),
        route_origin="PHX", route_destination="SFO", flight_number="UA2619",
        status=EventState.WAITING_2H,
    )
    return outbound, dependent_return


def test_a_same_day_return_leg_is_not_a_turnaround_conflict(monkeypatch):
    """The return arrives after the ETD by definition — that's the rotation
    working. Only a gap too short for a round trip marks a real conflict."""
    monkeypatch.setattr(tracker.airport_db, "lookup", fake_lookup)
    store = FlightStore()
    outbound, ret = out_and_back()
    store.upsert(outbound)
    store.upsert(ret)

    assert tracker._has_turnaround_conflict(store, outbound) is False

    app = FakeApp(store, sfo_config())
    asyncio.run(tracker.job_live_start(context_for(app, outbound)))
    assert store.get(outbound.id).status == EventState.LIVE


def test_a_gap_too_short_for_a_round_trip_still_conflicts(monkeypatch):
    """SFO->PHX with the 'return' due 1h after the ETD: the aircraft cannot
    have flown out and back — the inbound must be a delayed prerequisite."""
    monkeypatch.setattr(tracker.airport_db, "lookup", fake_lookup)
    store = FlightStore()
    outbound, ret = out_and_back()
    ret.scheduled_time = outbound.scheduled_time + timedelta(hours=1)
    store.upsert(outbound)
    store.upsert(ret)

    assert tracker._has_turnaround_conflict(store, outbound) is True


def test_poll_releases_a_held_departure_taxiing_at_its_own_airport(monkeypatch):
    """N27255 as observed live: held as a source conflict while sitting at SFO
    broadcasting UAL2374 — positive proof there is no conflict."""
    store, config = FlightStore(), sfo_config()
    outbound, ret = out_and_back()
    outbound.status = EventState.TURNAROUND_DELAY
    outbound.status_note = tracker.TURNAROUND_CONFLICT_NOTE
    outbound.scheduled_time = NOW - timedelta(minutes=30)
    store.upsert(outbound)
    store.upsert(ret)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(
            lat=37.6109, lon=-122.3613, alt_ft=0, on_ground=True, gs_kts=9.0,
            baro_rate=None, callsign="UAL2374", source="test",
        ),
    )

    asyncio.run(tracker.job_poll(context_for(app, outbound)))

    survivor = store.get(outbound.id)
    assert survivor.status == EventState.LIVE
    assert "still on the ground" in survivor.status_note


def test_poll_returns_a_stale_hold_to_normal_waiting(monkeypatch):
    """A hold whose conflict has evaporated (and no contrary position data)
    must drop back to normal waiting, not stick forever."""
    store, config = FlightStore(), sfo_config()
    outbound, _ = out_and_back()
    outbound.status = EventState.TURNAROUND_DELAY
    outbound.status_note = tracker.TURNAROUND_CONFLICT_NOTE
    store.upsert(outbound)  # no inbound in the store: nothing conflicts
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, outbound)))

    assert store.get(outbound.id).status == EventState.WAITING_LIVE


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
