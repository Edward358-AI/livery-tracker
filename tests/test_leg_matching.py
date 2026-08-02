"""Leg re-matching at reconciliation time (the hourly sync's per-leg lookup).

Regression cover for the UA1116 incident: N475UA was harvested as operating
UA1116 SFO->AUS, United later swapped the tail onto UA1265 SFO->SEA, and the
refresh matched on origin alone -- adopting UA1265's 2:24 PM estimate and
reporting a 64-minute delay for a flight that was on time.
"""

from datetime import datetime, timedelta, timezone

import livery_tracker.schedule_provider as sp
from livery_tracker.flights import EventType, FlightEvent

BASE = datetime(2026, 7, 27, 20, 20, tzinfo=timezone.utc)  # 1:20 PM PDT


def row(flight, origin, dest, dep_offset_min, *, est_offset_min=None, status="estimated"):
    departure = int((BASE + timedelta(minutes=dep_offset_min)).timestamp())
    estimated = departure if est_offset_min is None else int(
        (BASE + timedelta(minutes=est_offset_min)).timestamp()
    )
    return {
        "identification": {"number": {"default": flight}},
        "airport": {"origin": {"code": {"iata": origin}},
                    "destination": {"code": {"iata": dest}}},
        "time": {"scheduled": {"departure": departure, "arrival": departure + 10800},
                 "estimated": {"departure": estimated, "arrival": None}},
        "status": {"generic": {"status": {"text": status}}},
    }


def ua1116_leg() -> FlightEvent:
    return FlightEvent(
        id="sim", tail="N475UA", livery="Retro", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=BASE,
        route_origin="SFO", route_destination="AUS", flight_number="UA1116",
    )


# -- the bug -------------------------------------------------------------------

def test_other_departure_from_same_airport_is_not_matched():
    """UA1265 SFO->SEA must never stand in for UA1116 SFO->AUS."""
    rows = [row("UA1265", "SFO", "SEA", 40, est_offset_min=64)]
    assert sp._best_leg_row(rows, ua1116_leg()) is None


def test_swap_is_reported_instead_of_a_fabricated_delay(monkeypatch):
    def fake_fetch(query, fetch_by="reg"):
        if fetch_by == "reg":          # our tail now flies a different flight
            return [row("UA1265", "SFO", "SEA", 40, est_offset_min=64)]
        return [row("UA1116", "SFO", "AUS", 0)]   # UA1116 itself runs on time

    monkeypatch.setattr(sp, "fetch_flight_list", fake_fetch)
    result = sp.refresh_leg_time("N475UA", ua1116_leg())

    assert result.swapped is True
    assert result.new_time is None     # crucially, no borrowed time
    assert result.cancelled is False


# -- correct behaviour still works ---------------------------------------------

def test_real_delay_on_the_same_flight_is_still_detected(monkeypatch):
    monkeypatch.setattr(
        sp, "fetch_flight_list",
        lambda q, fetch_by="reg": [row("UA1116", "SFO", "AUS", 0, est_offset_min=35)],
    )
    result = sp.refresh_leg_time("N475UA", ua1116_leg())
    assert result.swapped is False and result.cancelled is False
    assert round((result.new_time - BASE).total_seconds() / 60) == 35


def test_flight_number_wins_over_a_closer_wrong_flight():
    """A nearer-in-time neighbour must not beat the exact flight number."""
    rows = [
        row("UA1265", "SFO", "SEA", 5),      # closer in time, wrong flight
        row("UA1116", "SFO", "AUS", 30),     # the real one, further away
    ]
    when, matched = sp._best_leg_row(rows, ua1116_leg())
    assert sp._row_flight_number(matched) == "UA1116"
    assert round((when - BASE).total_seconds() / 60) == 30


def test_route_pair_matches_when_flight_number_changed():
    """Renumbered flight, same aircraft and route -> still our leg."""
    rows = [row("UA9999", "SFO", "AUS", 20)]
    when, _ = sp._best_leg_row(rows, ua1116_leg())
    assert round((when - BASE).total_seconds() / 60) == 20


def oak_arrival() -> FlightEvent:
    return FlightEvent(
        id="oak", tail="N8655D", livery="New Mexico One", type=EventType.ARRIVAL,
        target_airport="OAK", scheduled_time=BASE,
        route_origin="PHX", route_destination="OAK", flight_number="WN1050",
    )


def test_a_reused_number_on_another_route_is_not_matched():
    """The N8655D case: WN1050 served OAK, then the same number flew to BWI.
    A number match must still serve the watched airport, or the phantom leg
    keeps adopting very real-looking times from a flight going elsewhere."""
    rows = [row("WN1050", "PHX", "BWI", 10)]
    assert sp._best_leg_row(rows, oak_arrival()) is None


def test_a_rerouted_origin_still_matches_an_arrival():
    """Only the watched end is identity: WN1050 from LAS instead of PHX is
    still the same OAK arrival for the spotter."""
    rows = [row("WN1050", "LAS", "OAK", 10)]
    when, matched = sp._best_leg_row(rows, oak_arrival())
    assert sp._row_flight_number(matched) == "WN1050"


def test_a_departure_number_match_requires_our_origin():
    departure = FlightEvent(
        id="dep", tail="N8655D", livery="", type=EventType.DEPARTURE,
        target_airport="OAK", scheduled_time=BASE,
        route_origin="OAK", route_destination="SAN", flight_number="WN295",
    )
    assert sp._best_leg_row([row("WN295", "PHX", "BWI", 10)], departure) is None
    assert sp._best_leg_row([row("WN295", "OAK", "BWI", 10)], departure) is not None


def test_number_reuse_elsewhere_reports_gone_not_swapped(monkeypatch):
    """Both lookups see only the BWI flight: the leg is gone from our airport,
    but the aircraft still flies that number — "swapped" would be a lie."""
    monkeypatch.setattr(
        sp, "fetch_flight_list",
        lambda q, fetch_by="reg": [row("WN1050", "PHX", "BWI", 10)],
    )
    result = sp.refresh_leg_time("N8655D", oak_arrival())
    assert result.new_time is None
    assert result.swapped is False and result.cancelled is False


def test_cancellation_still_beats_swap_detection(monkeypatch):
    def fake_fetch(query, fetch_by="reg"):
        if fetch_by == "reg":
            return []
        return [row("UA1116", "SFO", "AUS", 0, status="canceled")]

    monkeypatch.setattr(sp, "fetch_flight_list", fake_fetch)
    result = sp.refresh_leg_time("N475UA", ua1116_leg())
    assert result.cancelled is True and result.swapped is False


def test_arrival_legs_are_matched_the_same_way():
    arrival = FlightEvent(
        id="a", tail="N475UA", livery="", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=BASE,
        route_origin="AUS", route_destination="SFO", flight_number="UA292",
    )
    # A different arrival into SFO must not be adopted.
    assert sp._best_leg_row([row("UA718", "MEX", "SFO", 30)], arrival) is None
    assert sp._best_leg_row([row("UA292", "AUS", "SFO", 30)], arrival) is not None


def test_no_rows_at_all_reports_nothing(monkeypatch):
    monkeypatch.setattr(sp, "fetch_flight_list", lambda q, fetch_by="reg": [])
    result = sp.refresh_leg_time("N475UA", ua1116_leg())
    assert result.new_time is None and not result.cancelled and not result.swapped
