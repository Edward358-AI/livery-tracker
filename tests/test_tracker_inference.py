"""Signal-loss inference and cancellation detection (pure logic, no network)."""

from datetime import datetime, timedelta, timezone

from livery_tracker.adsb import Telemetry
from livery_tracker.flights import EventState, EventType, FlightEvent
from livery_tracker.schedule_provider import LegRefresh, row_is_cancelled
from livery_tracker.tracker import (
    LIVE_MAX_OVERRUN,
    TOUCHDOWN_MAX_PROJECTION,
    WHEELS_ETA_MAX_DIST_NM,
    _apply_delay_pushback,
    _callsign_matches_flight,
    _conclude_dark_leg,
    _estimate_touchdown,
    _no_show_note,
    _predict_touchdown,
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


def test_never_seen_past_deadline_is_not_concluded_early():
    """A dark aircraft 45 minutes late is "likely delayed", not LOST — the
    source re-checks and the hard cap in job_poll own this case now."""
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=45), NEVER_SEEN)
    assert _conclude_dark_leg(ev, NOW) is None


def test_never_seen_past_hard_cap_is_lost():
    ev = make_live_event(
        EventType.ARRIVAL, NOW - LIVE_MAX_OVERRUN - timedelta(minutes=1), NEVER_SEEN
    )
    state, _ = _conclude_dark_leg(ev, NOW)
    assert state == EventState.LOST


def test_no_show_note_labels_a_dark_departure_as_likely_delayed():
    ev = make_live_event(EventType.DEPARTURE, NOW - timedelta(minutes=25), NEVER_SEEN)
    note = _no_show_note(ev, NOW)
    assert "25m past ETD" in note and "likely delayed" in note


def test_no_show_note_stays_quiet_inside_the_grace_period():
    ev = make_live_event(EventType.DEPARTURE, NOW - timedelta(minutes=5), NEVER_SEEN)
    assert _no_show_note(ev, NOW) == ""


def test_no_show_note_stays_quiet_once_the_aircraft_was_seen():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=25),
                         seen(8, alt=34000, dist_nm=300.0))
    assert _no_show_note(ev, NOW) == ""


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


# -- touchdown interpolation ----------------------------------------------------
#
# The 2-minute poll cadence means the concluding fix is on short final (early)
# or already rolling out (late). The estimator projects the height still to
# lose through the observed descent rate instead of stamping poll time.

def live_fix(alt, ground=False, rate=None, gs=140.0) -> Telemetry:
    return Telemetry(lat=37.6, lon=-122.4, alt_ft=alt, on_ground=ground,
                     gs_kts=gs, baro_rate=rate, callsign="ASA1234", source="test")


def test_touchdown_projected_forward_from_short_final():
    # 400 ft AGL descending 800 fpm: wheels meet the runway in ~30 s.
    est = _estimate_touchdown(live_fix(400, rate=-800), seen(2), None, NOW)
    assert est == NOW + timedelta(seconds=30)


def test_touchdown_interpolated_back_from_a_rollout_fix():
    # On the ground now; two minutes ago it was at 700 ft descending 700 fpm,
    # so it touched down about a minute before this poll.
    prev = seen(2, alt=700, baro_rate=-700)
    est = _estimate_touchdown(live_fix(0, ground=True), prev, None, NOW)
    assert est == NOW - timedelta(minutes=1)


def test_touchdown_uses_field_elevation_for_height():
    # 5,700 ft over a 5,000 ft field is 700 ft AGL, not 5,700.
    prev = seen(2, alt=5700, baro_rate=-700)
    est = _estimate_touchdown(live_fix(0, ground=True), prev, 5000.0, NOW)
    assert est == NOW - timedelta(minutes=1)


def test_touchdown_without_a_descent_rate_splits_the_gap():
    # Airborne then, on the ground now, no rate to project: the midpoint
    # halves the maximum error instead of always stamping late.
    prev = seen(2, alt=900, baro_rate=None)
    est = _estimate_touchdown(live_fix(0, ground=True), prev, None, NOW)
    assert est == NOW - timedelta(minutes=1)


def test_touchdown_never_leaves_the_observation_window():
    # A stale descent rate would project past "on the ground now" — clamp.
    prev = seen(2, alt=10000, baro_rate=-500)
    est = _estimate_touchdown(live_fix(0, ground=True), prev, None, NOW)
    assert est == NOW
    # And a short-final projection is capped rather than trusted forever.
    est = _estimate_touchdown(live_fix(4000, rate=-100), seen(2), None, NOW)
    assert est == NOW + TOUCHDOWN_MAX_PROJECTION


def test_touchdown_falls_back_to_poll_time_without_a_usable_prior_fix():
    already_down = _estimate_touchdown(
        live_fix(0, ground=True), seen(2, alt=0, on_ground=True), None, NOW
    )
    assert already_down == NOW
    no_history = _estimate_touchdown(live_fix(0, ground=True), {}, None, NOW)
    assert no_history == NOW


# -- predicted wheels-down for live arrivals ------------------------------------

def test_wheels_prediction_is_distance_over_speed():
    # 70 NM at 140 kts: half an hour out.
    est = _predict_touchdown(live_fix(12000), 70.0, None, NOW)
    assert est == NOW + timedelta(minutes=30)


def test_wheels_prediction_is_bounded_by_the_descent_still_to_fly():
    # 10 NM at 300 kts says 2 minutes — but 7,000 ft at 700 fpm says 10.
    # The descent is the binding constraint.
    est = _predict_touchdown(live_fix(7000, rate=-700, gs=300.0), 10.0, None, NOW)
    assert est == NOW + timedelta(minutes=10)


def test_wheels_prediction_measures_descent_against_the_field():
    # 5,700 ft over a 5,000 ft field: only 700 ft to lose, so distance
    # (2 NM at 120 kts = 1 min) and descent (1 min) agree.
    est = _predict_touchdown(live_fix(5700, rate=-700, gs=120.0), 2.0, 5000.0, NOW)
    assert est == NOW + timedelta(minutes=1)


def test_wheels_prediction_only_inside_the_window():
    assert _predict_touchdown(live_fix(34000, gs=450.0), WHEELS_ETA_MAX_DIST_NM + 1, None, NOW) is None
    assert _predict_touchdown(live_fix(0, ground=True), 3.0, None, NOW) is None
    assert _predict_touchdown(live_fix(2000, gs=30.0), 20.0, None, NOW) is None  # bogus speed


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


def test_delay_pushback_reports_aircraft_swap():
    ev = make_live_event(EventType.ARRIVAL, NOW - timedelta(minutes=35), NEVER_SEEN)
    assert _apply_delay_pushback(ev, LegRefresh(None, swapped=True)) == "swapped"


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


def test_callsign_guard_accepts_icao_equivalent_and_rejects_another_flight():
    # Real N8619F case: SWA3043 was airborne while stale data still assigned
    # the same tail to WN4244. ICAO/IATA prefixes differ; the suffix must not.
    assert _callsign_matches_flight("SWA3043", "WN3043")
    assert _callsign_matches_flight("UAL0012", "UA12")
    assert not _callsign_matches_flight("SWA3043", "WN4244")


def test_callsign_guard_does_not_reject_when_a_number_is_unavailable():
    assert _callsign_matches_flight(None, "WN4244")
    assert _callsign_matches_flight("SWA3043", "")
