"""Regressions for three digest entries that did not match reality.

  XA-VUS  shown "swapped off" Y47790 while FR24 still had it on that flight
  N475UA  shown "diverted" while simply sitting at its origin, pre-departure
  N579UW  genuinely swapped, but its stale downstream legs lingered
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
from livery_tracker.tracker import _callsign_matches_flight, _flight_digits

# Relative to the real clock: job_poll gives up on a leg LIVE_MAX_OVERRUN past
# its scheduled time, which a hard-coded date would trip immediately.
NOW = datetime.now(timezone.utc) + timedelta(minutes=20)


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
    config.target_airports = {
        "OAK": {"icao": "KOAK", "name": "Oakland", "lat": 37.7213, "lon": -122.2211},
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    return config


# -- 1. XA-VUS: airline codes that contain a digit ------------------------------

def test_flight_digits_strips_airline_code_including_numeric_ones():
    assert _flight_digits("Y47790") == "7790"      # Volaris: airline code Y4
    assert _flight_digits("VOI7790") == "7790"     # ...and its ICAO callsign
    assert _flight_digits("B61234") == "1234"      # JetBlue
    assert _flight_digits("AA3054") == "3054"
    assert _flight_digits("AAL3054") == "3054"
    assert _flight_digits("UAL0012") == "12"       # leading zeros ignored
    assert _flight_digits("") == ""
    assert _flight_digits(None) == ""


def test_volaris_callsign_is_not_treated_as_a_swap():
    """The reported XA-VUS case: VOI7790 really is Y47790."""
    assert _callsign_matches_flight("VOI7790", "Y47790")
    assert _callsign_matches_flight("VOI7791", "Y47791")
    assert _callsign_matches_flight("JBU1234", "B61234")


def test_genuinely_different_flights_still_mismatch():
    assert not _callsign_matches_flight("SWA3043", "WN4244")
    assert not _callsign_matches_flight("VOI7790", "Y43061")


# -- 2. N475UA: "diverted" while still at its origin ----------------------------

def arrival_leg() -> FlightEvent:
    return FlightEvent(
        id="N475UA-ARR-SFO", tail="N475UA", livery="Retro", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=NOW, route_origin="SEA",
        route_destination="SFO", flight_number="UA2164", status=EventState.LIVE,
    )


def poll_with(app, event, telemetry, times=1):
    async def scenario():
        ctx = SimpleNamespace(
            application=app, job=SimpleNamespace(data=event.id, schedule_removal=lambda: None)
        )
        for _ in range(times):
            await tracker.job_poll(ctx)

    original = tracker.fetch_telemetry
    tracker.fetch_telemetry = lambda reg: telemetry
    try:
        asyncio.run(scenario())
    finally:
        tracker.fetch_telemetry = original


def test_aircraft_waiting_at_its_origin_is_not_a_diversion():
    """SEA is this flight's origin: on the ground there is pre-departure."""
    store, config = FlightStore(), make_config()
    event = arrival_leg()
    store.upsert(event)
    at_seattle = Telemetry(lat=47.4489, lon=-122.3094, alt_ft=0, on_ground=True,
                           gs_kts=0.0, baro_rate=None, callsign="UAL2164", source="t")

    poll_with(FakeApp(store, config), event, at_seattle, times=3)

    assert store.get(event.id).status == EventState.LIVE, "must keep waiting, not divert"


def test_diversion_still_detected_once_the_aircraft_has_flown():
    """Airborne first, then on the ground far away — a real diversion."""
    store, config = FlightStore(), make_config()
    event = arrival_leg()
    store.upsert(event)
    app = FakeApp(store, config)

    airborne = Telemetry(lat=40.0, lon=-120.0, alt_ft=35000, on_ground=False,
                         gs_kts=450.0, baro_rate=0, callsign="UAL2164", source="t")
    poll_with(app, event, airborne)
    assert store.get(event.id).last_telemetry["was_airborne"] is True

    grounded_elsewhere = Telemetry(lat=38.6954, lon=-121.5908, alt_ft=0, on_ground=True,
                                   gs_kts=5.0, baro_rate=None, callsign="UAL2164", source="t")
    poll_with(app, event, grounded_elsewhere, times=tracker.DIVERT_CONFIRM_POLLS)

    assert store.get(event.id).status == EventState.DIVERTED


# -- 3. N579UW: the rest of a swapped rotation ---------------------------------

def rotation_legs() -> list[FlightEvent]:
    base = dict(tail="N579UW", livery="Allegheny Retro", target_airport="SFO")
    return [
        FlightEvent(id="arr", type=EventType.ARRIVAL, scheduled_time=NOW,
                    route_origin="ORD", route_destination="SFO",
                    flight_number="AA3054", status=EventState.WAITING_2H, **base),
        FlightEvent(id="dep", type=EventType.DEPARTURE, scheduled_time=NOW + timedelta(hours=6),
                    route_origin="SFO", route_destination="DFW",
                    flight_number="AA2305", status=EventState.WAITING_2H, **base),
    ]


def test_swap_drops_the_rest_of_the_rotation(monkeypatch):
    store, config = FlightStore(), make_config()
    arr, dep = rotation_legs()
    store.upsert(arr)
    store.upsert(dep)
    app = FakeApp(store, config)

    # FR24 no longer lists either flight for this tail; both still operate.
    monkeypatch.setattr(sp, "refresh_leg_time", lambda reg, ev: LegRefresh(None, swapped=True))
    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time",
                        lambda reg, ev: LegRefresh(None, swapped=True))

    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=arr.id))
    asyncio.run(tracker.job_refresh(ctx))

    assert store.get("arr").status == EventState.SWAPPED
    assert store.get("dep").status == EventState.SWAPPED, "downstream leg should go too"
    assert store.active() == []


def test_rotation_recheck_keeps_legs_the_aircraft_still_operates(monkeypatch):
    """Only legs the source confirms as swapped are dropped."""
    store, config = FlightStore(), make_config()
    arr, dep = rotation_legs()
    store.upsert(arr)
    store.upsert(dep)
    app = FakeApp(store, config)

    def selective(reg, ev):
        if ev.flight_number == "AA3054":
            return LegRefresh(None, swapped=True)
        return LegRefresh(ev.scheduled_time)      # still ours

    monkeypatch.setattr(tracker.schedule_provider, "refresh_leg_time", selective)

    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=arr.id))
    asyncio.run(tracker.job_refresh(ctx))

    assert store.get("arr").status == EventState.SWAPPED
    assert store.get("dep").status == EventState.WAITING_2H, "must not drop a valid leg"
