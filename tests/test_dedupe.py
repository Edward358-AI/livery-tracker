"""Duplicate-leg prevention: schedule-time drift must not spawn twin legs."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.tracker import _dedupe_store, _register_new_events, _same_flight_leg


class FakeApp:
    def __init__(self, store):
        self.bot_data = {"store": store}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


NOW = datetime(2026, 7, 27, 4, 5, tzinfo=timezone.utc)  # 9:05 PM PDT Jul 26


def leg(minutes_offset: int = 0, **overrides) -> FlightEvent:
    when = NOW + timedelta(minutes=minutes_offset)
    defaults = dict(
        id=FlightEvent.make_id("N8658A", EventType.DEPARTURE, when, "SFO"),
        tail="N8658A",
        livery="",
        type=EventType.DEPARTURE,
        target_airport="SFO",
        scheduled_time=when,
        route_origin="SFO",
        route_destination="LAX",
        flight_number="WN3645",
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


def test_same_flight_leg_tolerates_drift_but_not_other_flights():
    a = leg(0)
    assert _same_flight_leg(a, leg(4))            # the real WN3645 9:05/9:09 case
    assert not _same_flight_leg(a, leg(4 * 60))   # 4h apart: a different rotation
    assert not _same_flight_leg(a, leg(4, flight_number="WN9999"))
    assert not _same_flight_leg(a, leg(4, type=EventType.ARRIVAL))
    assert not _same_flight_leg(a, leg(4, target_airport="SJC"))


def test_register_updates_existing_leg_instead_of_duplicating():
    store = FlightStore()
    original = leg(0, status=EventState.WAITING_LIVE)
    store.upsert(original)
    app = FakeApp(store)

    added = _register_new_events(app, [leg(4)])  # same flight, estimate moved 4 min
    assert added == []
    assert len(store.events) == 1
    survivor = next(iter(store.events.values()))
    assert survivor.id == original.id
    assert survivor.scheduled_time == NOW + timedelta(minutes=4)

    added = _register_new_events(app, [leg(0, flight_number="WN1242",
                                           route_destination="SJC", target_airport="SJC",
                                           id="other")])
    assert len(added) == 1  # genuinely different flight still gets created
    assert added[0].flight_number == "WN1242"  # the caller can report what was found
    assert len(store.events) == 2


def test_source_can_revive_a_swapped_leg():
    """The AS751 case: swapped off in the morning, swapped back on by noon.
    A terminal 🔀 must not keep the flight dead for the rest of the day once
    the source lists it for this tail again."""
    store = FlightStore()
    dead = leg(0, status=EventState.SWAPPED)
    dead.status_note = "aircraft now operating AS1603"
    store.upsert(dead)
    app = FakeApp(store)

    added = _register_new_events(app, [leg(6, id="fresh")])

    survivor = store.get(dead.id)
    assert survivor.status == EventState.WAITING_2H
    assert survivor.status_note == ""
    assert survivor.scheduled_time == NOW + timedelta(minutes=6)
    assert added == [survivor], "a revival is reportable news"
    assert len(store.events) == 1, "revived in place, not duplicated"


def test_a_cancelled_row_never_revives_a_cancelled_leg():
    """Cancelled rows keep appearing in the source all day; discovery now
    carries that flag, so re-finding one must not flap the leg back open."""
    store = FlightStore()
    dead = leg(0, status=EventState.CANCELLED)
    store.upsert(dead)

    added = _register_new_events(
        FakeApp(store), [leg(4, id="fresh", status=EventState.CANCELLED)]
    )

    assert added == []
    assert store.get(dead.id).status == EventState.CANCELLED


def test_register_does_not_touch_terminal_legs():
    store = FlightStore()
    done = leg(0, status=EventState.DEPARTED)
    store.upsert(done)
    added = _register_new_events(FakeApp(store), [leg(4)])
    assert added == []
    assert store.get(done.id).scheduled_time == NOW  # untouched


def test_dedupe_store_collapses_existing_twins():
    store = FlightStore()
    store.upsert(leg(0, status=EventState.WAITING_LIVE))
    store.upsert(leg(4, status=EventState.WAITING_LIVE))
    store.upsert(leg(0, flight_number="WN1242", route_destination="SJC",
                     target_airport="SJC", id="unrelated"))
    removed = _dedupe_store(FakeApp(store))
    assert removed == 1
    assert len(store.events) == 2  # one WN3645 survivor + the unrelated leg


def test_dedupe_store_prefers_terminal_copy():
    store = FlightStore()
    store.upsert(leg(0, status=EventState.WAITING_LIVE))
    finished = leg(4, status=EventState.DEPARTED)
    store.upsert(finished)
    _dedupe_store(FakeApp(store))
    assert list(store.events.values()) == [finished]
