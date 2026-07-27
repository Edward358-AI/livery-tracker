"""/refresh and /add report which legs were found, not just how many."""

from datetime import datetime, timedelta, timezone

from livery_tracker.bot import MAX_LISTED_LEGS, _describe_new_legs
from livery_tracker.flights import EventState, EventType, FlightEvent
from livery_tracker.tracker import HarvestResult

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)


def leg(tail, flight, origin, dest, minutes, ev_type=EventType.ARRIVAL, livery="") -> FlightEvent:
    return FlightEvent(
        id=f"{tail}-{flight}", tail=tail, livery=livery, type=ev_type,
        target_airport=dest if ev_type == EventType.ARRIVAL else origin,
        scheduled_time=NOW + timedelta(minutes=minutes),
        route_origin=origin, route_destination=dest, flight_number=flight,
        status=EventState.WAITING_2H,
    )


def test_no_new_legs_renders_nothing():
    assert _describe_new_legs([]) == ""


def test_each_new_leg_is_listed():
    events = [
        leg("N265AK", "AS1234", "SEA", "SFO", 90, livery="Retro"),
        leg("N8658A", "WN400", "SJC", "LAS", 120, EventType.DEPARTURE),
    ]
    text = _describe_new_legs(events)
    assert "N265AK" in text and "AS1234" in text and "SEA➔SFO" in text
    assert "N8658A" in text and "WN400" in text
    assert '"Retro"' in text          # livery carried through
    assert "ETA" in text and "ETD" in text


def test_long_lists_are_truncated_for_telegram():
    events = [leg("N%03d" % i, f"AS{i}", "SEA", "SFO", i) for i in range(40)]
    text = _describe_new_legs(events)
    assert text.count("➔") == MAX_LISTED_LEGS
    assert f"…and {40 - MAX_LISTED_LEGS} more" in text


def test_harvest_result_merges_both_phases_in_time_order():
    board = leg("N1", "AS1", "SEA", "SFO", 300)
    tail = leg("N2", "AS2", "LAX", "SFO", 60)
    result = HarvestResult(board_events=[board], tail_events=[tail])

    assert result.new_legs == 2
    assert result.board_legs == 1 and result.tail_legs == 1
    assert [e.tail for e in result.new_events] == ["N2", "N1"]  # chronological


def test_skipped_harvest_reports_nothing_new():
    result = HarvestResult(skipped=True)
    assert result.new_legs == 0
    assert result.new_events == []
