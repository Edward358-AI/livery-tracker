"""Every swap-detection entry point must cascade to the rest of the rotation.

The reported N579UW case was that a sibling leg (a future departure not yet
at its own T-2h check) kept showing stale data after the aircraft was
confirmed swapped elsewhere. The fix added a cascade from _mark_swapped, but
it only proves itself if every detection path actually reaches that call.
This file drives all three: the T-2h refresh, the pre-live-tracking check,
and a live-poll callsign mismatch.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.adsb as adsb
import livery_tracker.schedule_provider as sp
import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.schedule_provider import LegRefresh

NOW = datetime.now(timezone.utc) + timedelta(hours=1)


class FakeDigest:
    async def refresh(self):
        pass


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def make_config() -> Config:
    config = Config()
    config.target_airports = {"SFO": {"icao": "KSFO", "name": "SF", "lat": 37.6, "lon": -122.4}}
    return config


def rotation(tail="N579UW"):
    """One leg about to be checked, and a future sibling that has not."""
    triggering = FlightEvent(
        id="triggering", tail=tail, livery="", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=NOW, route_origin="ORD",
        route_destination="SFO", flight_number="AA3054", status=EventState.LIVE,
    )
    future_sibling = FlightEvent(
        id="future_sibling", tail=tail, livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=NOW + timedelta(hours=6),
        route_origin="SFO", route_destination="DFW", flight_number="AA2305",
        status=EventState.WAITING_2H,
    )
    return triggering, future_sibling


def always_swapped(reg, ev):
    return LegRefresh(None, swapped=True)


# -- path 1: T-2h schedule refresh (already covered elsewhere, kept for parity) -

def test_schedule_refresh_path_cascades(monkeypatch):
    triggering, sibling = rotation()
    store = FlightStore()
    store.upsert(triggering)
    store.upsert(sibling)
    app = FakeApp(store, make_config())
    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", always_swapped)

    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=triggering.id))
    asyncio.run(tracker.job_refresh(ctx))

    assert store.get("future_sibling").status == EventState.SWAPPED


# -- path 2: the pre-live-tracking check -----------------------------------------

def test_live_start_path_cascades(monkeypatch):
    triggering, sibling = rotation()
    triggering.status = EventState.WAITING_LIVE
    store = FlightStore()
    store.upsert(triggering)
    store.upsert(sibling)
    app = FakeApp(store, make_config())
    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", always_swapped)

    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=triggering.id))
    asyncio.run(tracker.job_live_start(ctx))

    assert store.get("triggering").status == EventState.SWAPPED
    assert store.get("future_sibling").status == EventState.SWAPPED, \
        "a swap caught right before live tracking must still cascade"


# -- path 3: live-poll callsign mismatch -----------------------------------------

def test_live_poll_callsign_mismatch_cascades(monkeypatch):
    """The aircraft is seen flying under a callsign that matches nothing we
    have scheduled for it — direct proof of a swap, discovered mid-flight."""
    triggering, sibling = rotation()
    store = FlightStore()
    store.upsert(triggering)
    store.upsert(sibling)
    app = FakeApp(store, make_config())

    # The rotation re-check (for the sibling) must also confirm the swap.
    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", always_swapped)
    wrong_flight = Telemetry(
        lat=37.6, lon=-122.4, alt_ft=12000, on_ground=False, gs_kts=300.0,
        baro_rate=-500, callsign="AAL9999", source="test",
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: wrong_flight)

    ctx = SimpleNamespace(
        application=app, job=SimpleNamespace(data=triggering.id, schedule_removal=lambda: None)
    )
    asyncio.run(tracker.job_poll(ctx))

    assert store.get("triggering").status == EventState.SWAPPED
    assert store.get("future_sibling").status == EventState.SWAPPED, \
        "a swap caught mid-flight via callsign mismatch must still cascade"


# -- the cascade itself must not re-cascade (bounded) ----------------------------

def test_cascade_is_one_pass_only(monkeypatch):
    """A sibling confirmed swapped during the cascade must not trigger a
    second cascade — otherwise a large rotation could blow up in cost."""
    tail = "N579UW"
    triggering, sibling = rotation(tail)
    third = FlightEvent(
        id="third", tail=tail, livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=NOW + timedelta(hours=10),
        route_origin="SFO", route_destination="LAX", flight_number="AA9001",
        status=EventState.WAITING_2H,
    )
    store = FlightStore()
    for ev in (triggering, sibling, third):
        store.upsert(ev)
    app = FakeApp(store, make_config())

    calls = {"n": 0}

    def counting_swap(reg, ev):
        calls["n"] += 1
        return LegRefresh(None, swapped=True)

    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", counting_swap)
    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=triggering.id))
    asyncio.run(tracker.job_refresh(ctx))

    # 1 call for the triggering leg itself + 1 per sibling (sibling, third) =
    # 3. If the cascade recursed, "third" being marked swapped inside the
    # cascade would trigger its own re-check pass and this would grow.
    assert calls["n"] == 3
    assert store.get("third").status == EventState.SWAPPED
