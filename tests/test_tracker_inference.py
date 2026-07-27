"""Signal-loss inference and cancellation detection (pure logic, no network)."""

from datetime import datetime, timedelta, timezone

from livery_tracker.flights import EventState, EventType, FlightEvent
from livery_tracker.schedule_provider import LegRefresh, row_is_cancelled
from livery_tracker.tracker import (
    LIVE_MAX_OVERRUN,
    _apply_delay_pushback,
    _conclude_dark_leg,
)


def make_live_event(ev_type: EventType, scheduled: datetime, telemetry: dict) -> FlightEvent:
    ev = FlightEvent(
        id="x",
        tail="N265AK",
        livery="",
        type=ev_type,
        target_airport="SFO",
        scheduled_time=scheduled,
        route_origin="SEA",
        route_destination="SFO",
        status=EventState.LIVE,
    )
    ev.last_telemetry = telemetry
    return ev


NOW = datetime(2026, 7, 26, 23, 0, tzinfo=timezone.utc)
NEVER_SEEN = {"lat": None, "lon": None, "alt": None, "gs": None, "dist_nm": None}


def seen(minutes_ago: int, **fields) -> dict:
    base = {
        "lat": 37.5,
        "lon": -122.3,
        "alt": 2000,
        "gs": 150.0,
        "dist_nm": 8.0,
        "on_ground": False,
        "baro_rate": -600,
        "seen_at": (NOW - timedelta(minutes=minutes_ago)).isoformat(),
    }
    base.update(fields)
    return base


def test_never_seen_before_deadline_keeps_polling():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=10), NEVER_SEEN)
    assert _conclude_dark_leg(ev, NOW) is None


def test_never_seen_past_deadline_is_lost():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=45), NEVER_SEEN)
    state, _ = _conclude_dark_leg(ev, NOW)
    assert state == EventState.LOST


def test_dark_on_approach_becomes_landed():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=5),
                         seen(8, alt=1400, dist_nm=4.0))
    state, note = _conclude_dark_leg(ev, NOW)
    assert state == EventState.LANDED
    assert "signal lost on approach" in note


def test_dark_while_climbing_near_airport_is_not_landed():
    # Real case observed at SFO: 1,250 ft / 1.7 NM but +3,392 fpm — a
    # go-around or departure, not a landing. Must keep polling.
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=5),
                         seen(8, alt=1250, dist_nm=1.7, baro_rate=3392))
    assert _conclude_dark_leg(ev, NOW) is None


def test_dark_at_cruise_keeps_polling_until_cap():
    tele = seen(8, alt=34000, dist_nm=200.0)
    ev = make_live_event(EventType.ARRIVAL, NOW + timedelta(minutes=25), tele)
    assert _conclude_dark_leg(ev, NOW) is None  # not near, not past cap: wait

    ev_late = make_live_event(EventType.ARRIVAL, NOW - LIVE_MAX_OVERRUN - timedelta(minutes=1), tele)
    state, _ = _conclude_dark_leg(ev_late, NOW)
    assert state == EventState.LOST


def test_short_silence_keeps_polling():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=5),
                         seen(2, alt=1400, dist_nm=4.0))
    assert _conclude_dark_leg(ev, NOW) is None


def test_departure_dark_after_takeoff_becomes_departed():
    ev = make_live_event(EventType.DEPARTURE, NOW - timedelta(minutes=10),
                         seen(8, alt=5200, dist_nm=6.0))
    state, note = _conclude_dark_leg(ev, NOW)
    assert state == EventState.DEPARTED
    assert "signal lost after takeoff" in note


def test_departure_dark_still_on_ground_keeps_polling():
    ev = make_live_event(EventType.DEPARTURE, NOW - timedelta(minutes=10),
                         seen(8, alt=0, on_ground=True, dist_nm=0.2))
    assert _conclude_dark_leg(ev, NOW) is None


def test_delay_pushback_reverts_to_waiting():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=35), NEVER_SEEN)
    new_time = NOW + timedelta(hours=2)
    action = _apply_delay_pushback(ev, LegRefresh(new_time))
    assert action == "delayed"
    assert ev.status == EventState.WAITING_LIVE
    assert ev.scheduled_time == new_time
    assert "delayed" in ev.status_note


def test_delay_pushback_reports_cancellation():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=35), NEVER_SEEN)
    assert _apply_delay_pushback(ev, LegRefresh(None, cancelled=True)) == "cancelled"


def test_delay_pushback_ignores_unchanged_schedule():
    sched = NOW - timedelta(minutes=35)
    ev = make_live_event(EventType.ARRIVAL, sched, NEVER_SEEN)
    assert _apply_delay_pushback(ev, LegRefresh(sched)) is None
    assert _apply_delay_pushback(ev, LegRefresh(None)) is None
    assert ev.status == EventState.LIVE  # untouched


def test_row_is_cancelled():
    assert row_is_cancelled({"status": {"generic": {"status": {"text": "Canceled"}}}})
    assert row_is_cancelled({"status": {"generic": {"status": {"text": "cancelled"}}}})
    assert not row_is_cancelled({"status": {"generic": {"status": {"text": "estimated"}}}})
    assert not row_is_cancelled({})
