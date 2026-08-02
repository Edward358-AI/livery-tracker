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


def live_departure(number="AS656", dest="SEA", when=None) -> FlightEvent:
    return FlightEvent(
        id=number.lower(), tail="N985AK", livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=when or (NOW + timedelta(minutes=50)),
        route_origin="SFO", route_destination=dest, flight_number=number,
        status=EventState.LIVE,
    )


def test_stale_gate_callsign_is_not_a_swap(monkeypatch):
    """N985AK as observed: parked at its SFO gate still squawking the
    inbound's ASA725 while its own AS656 was live — hold, never swap."""
    store, config = FlightStore(), sfo_config()
    leg = live_departure()
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time, delay_minutes=25),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                              gs_kts=0.0, baro_rate=None, callsign="ASA725", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.TURNAROUND_DELAY
    assert "still operating ASA725" in survivor.status_note


def test_airborne_on_its_late_inbound_is_not_a_swap(monkeypatch):
    """N8619F as observed: WN3496 went live at T-1h while the tail was still
    flying its late inbound WN3982 — hold for the rotation."""
    store, config = FlightStore(), sfo_config()
    leg = live_departure(number="WN3496", dest="SAN", when=NOW - timedelta(minutes=10))
    leg.tail = "N8619F"
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(NOW + timedelta(minutes=40), delay_minutes=50),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=36.9, lon=-121.2, alt_ft=22_000, on_ground=False,
                              gs_kts=410.0, baro_rate=-800, callsign="SWA3982", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.TURNAROUND_DELAY
    assert "still operating SWA3982" in survivor.status_note
    assert survivor.scheduled_time == NOW + timedelta(minutes=40), \
        "the source's delayed time must be mirrored while holding"


def test_mismatch_with_the_flight_truly_gone_is_still_a_swap(monkeypatch):
    """The original poison case: the tail flies something else AND the source
    no longer lists our flight for it — that ends the leg."""
    store, config = FlightStore(), sfo_config()
    leg = live_departure()
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(None, swapped=True),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=36.9, lon=-121.2, alt_ft=22_000, on_ground=False,
                              gs_kts=410.0, baro_rate=0, callsign="ASA9999", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    assert store.get(leg.id).status == EventState.SWAPPED


def test_held_leg_withdraws_once_the_source_drops_it(monkeypatch):
    """A foreign-callsign hold is not the known-faulty-source window: if the
    source stops listing the leg, trust it and withdraw."""
    store, config = FlightStore(), sfo_config()
    leg = live_departure()
    leg.status = EventState.TURNAROUND_DELAY
    leg.status_note = "aircraft still operating ASA725 — awaiting rotation"
    store.upsert(leg)  # no inbound in the store: no genuine conflict
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(None, swapped=True),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    survivor = store.get(leg.id)
    assert survivor.status == EventState.SWAPPED
    assert survivor.status_note == tracker.WITHDRAWN_NOTE


def test_foreign_callsign_keeps_the_hold_from_flapping(monkeypatch):
    """While the old callsign persists, the hold must stay a hold — not fall
    back to waiting and re-trip on the next poll."""
    store, config = FlightStore(), sfo_config()
    leg = live_departure()
    leg.status = EventState.TURNAROUND_DELAY
    leg.status_note = "aircraft still operating ASA725 — awaiting rotation"
    store.upsert(leg)
    app = FakeApp(store, config)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                              gs_kts=0.0, baro_rate=None, callsign="ASA725", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    assert store.get(leg.id).status == EventState.TURNAROUND_DELAY


def test_discovery_carries_a_rows_cancellation():
    """A flight already cancelled at discovery time builds directly as ❌ —
    and therefore can never revive a previously cancelled leg."""
    stamp = lambda when: int(when.timestamp())
    row = {
        "identification": {"number": {"default": "UA1007"}},
        "airport": {
            "origin": {"code": {"iata": "SFO", "icao": "KSFO"}},
            "destination": {"code": {"iata": "SEA", "icao": "KSEA"}},
        },
        "time": {"scheduled": {"departure": stamp(NOW + timedelta(hours=4))}},
        "status": {"generic": {"status": {"text": "Canceled"}}},
    }

    events = rows_to_events("N475UA", "Retro", [row], sfo_config(), now=NOW)

    assert len(events) == 1
    assert events[0].status == EventState.CANCELLED


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
