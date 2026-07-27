"""Airport-board harvesting (phase 1) and its interaction with the tail sweep."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import livery_tracker.schedule_provider as sp
import livery_tracker.tracker as tracker
from livery_tracker.config import Config
from livery_tracker.flights import EventType, FlightStore

NOW = datetime(2026, 7, 27, 18, 0, tzinfo=timezone.utc)


def board_entry(reg, flight_no, other_code, offset_min, mode):
    """A board entry: the queried airport's own side is deliberately absent."""
    stamp = int((NOW + timedelta(minutes=offset_min)).timestamp())
    side = "origin" if mode == "arrivals" else "destination"
    return {
        "flight": {
            "identification": {"number": {"default": flight_no}},
            "aircraft": {"registration": reg, "model": {"code": "B739"}},
            "airport": {side: {"code": {"iata": other_code, "icao": "K" + other_code}}},
            "time": {"scheduled": {"departure": stamp, "arrival": stamp + 7200},
                     "estimated": {"departure": None, "arrival": None}},
            "status": {"generic": {"status": {"text": "scheduled"}}},
        }
    }


def make_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6, "lon": -122.4},
    }
    config.watchlist = {"N265AK": {"livery": "Retro"}}
    return config


# -- implied airport code ------------------------------------------------------

def test_queried_airport_code_is_injected():
    entry = board_entry("N265AK", "AS1", "SEA", 30, "arrivals")["flight"]
    assert entry["airport"].get("destination") is None
    sp._inject_queried_airport(entry, "SFO", "arrivals")
    assert entry["airport"]["destination"]["code"]["iata"] == "SFO"
    assert entry["airport"]["origin"]["code"]["iata"] == "SEA"  # untouched


def test_injection_picks_the_right_side_for_departures():
    entry = board_entry("N265AK", "AS2", "LAX", 30, "departures")["flight"]
    sp._inject_queried_airport(entry, "SFO", "departures")
    assert entry["airport"]["origin"]["code"]["iata"] == "SFO"
    assert entry["airport"]["destination"]["code"]["iata"] == "LAX"


def test_injection_never_overwrites_a_real_code():
    entry = board_entry("N265AK", "AS3", "SEA", 30, "arrivals")["flight"]
    entry["airport"]["destination"] = {"code": {"iata": "OAK", "icao": "KOAK"}}
    sp._inject_queried_airport(entry, "SFO", "arrivals")
    assert entry["airport"]["destination"]["code"]["iata"] == "OAK"


# -- paging --------------------------------------------------------------------

def test_board_pages_until_the_window_is_covered(monkeypatch):
    pages = {
        1: [board_entry("N265AK", "AS1", "SEA", 60, "arrivals")],
        2: [board_entry("N265AK", "AS2", "SEA", 30 * 60, "arrivals")],   # +30h, past horizon
        3: [board_entry("N265AK", "AS3", "SEA", 40 * 60, "arrivals")],
    }
    seen: list[int] = []

    def fake_page(code, mode, page):
        seen.append(page)
        return pages.get(page, [])

    monkeypatch.setattr(sp, "_fetch_board_page", fake_page)
    rows = sp.fetch_airport_board("SFO", "arrivals", now=NOW)
    assert seen == [1, 2]      # stopped once past the 24h horizon
    assert len(rows) == 2


def test_board_stops_on_an_empty_page(monkeypatch):
    monkeypatch.setattr(
        sp, "_fetch_board_page",
        lambda code, mode, page: [board_entry("N265AK", "AS1", "SEA", 60, "arrivals")]
        if page == 1 else [],
    )
    assert len(sp.fetch_airport_board("SFO", "arrivals", now=NOW)) == 1


def test_board_failure_on_first_page_reports_none(monkeypatch):
    monkeypatch.setattr(sp, "_fetch_board_page", lambda code, mode, page: None)
    assert sp.fetch_airport_board("SFO", "arrivals", now=NOW) is None


# -- filtering to the watchlist ------------------------------------------------

def injected_rows(code, mode, *entries):
    """Rows exactly as fetch_airport_board returns them (implied code filled in)."""
    rows = [e["flight"] for e in entries]
    for row in rows:
        sp._inject_queried_airport(row, code, mode)
    return rows


def test_boards_keep_only_watched_aircraft(monkeypatch):
    def fake_board(code, mode, now=None):
        if mode != "arrivals":
            return []
        return injected_rows(
            code, mode,
            board_entry("N265AK", "AS100", "SEA", 60, "arrivals"),
            board_entry("N999XX", "UA200", "DEN", 90, "arrivals"),  # not watched
        )

    monkeypatch.setattr(sp, "fetch_airport_board", fake_board)
    events, ok = sp.harvest_airport_boards(make_config(), now=NOW)
    assert ok is True
    assert [e.tail for e in events] == ["N265AK"]
    assert events[0].type == EventType.ARRIVAL
    assert events[0].target_airport == "SFO"
    assert events[0].livery == "Retro"          # taken from the watchlist entry


def test_board_source_failure_is_reported(monkeypatch):
    monkeypatch.setattr(sp, "fetch_airport_board", lambda code, mode, now=None: None)
    events, ok = sp.harvest_airport_boards(make_config(), now=NOW)
    assert events == [] and ok is False


# -- phase interaction ---------------------------------------------------------

class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def test_board_and_tail_phases_do_not_duplicate_a_shared_flight(monkeypatch):
    """The same flight seen by both sweeps must collapse into one leg."""
    config = make_config()
    store = FlightStore()
    app = FakeApp(store, config)

    monkeypatch.setattr(
        sp, "fetch_airport_board",
        lambda code, mode, now=None: (
            injected_rows(code, mode, board_entry("N265AK", "AS100", "SEA", 60, "arrivals"))
            if mode == "arrivals" else []
        ),
    )
    board_events, _ = sp.harvest_airport_boards(config, now=NOW)
    assert len(tracker._register_new_events(app, board_events)) == 1
    original_time = next(iter(store.events.values())).scheduled_time

    # The tail sweep finds the same flight, estimate nudged four minutes later.
    later = board_entry("N265AK", "AS100", "SEA", 64, "arrivals")["flight"]
    sp._inject_queried_airport(later, "SFO", "arrivals")
    tail_events = sp.rows_to_events("N265AK", "Retro", [later], config, now=NOW)

    assert tracker._register_new_events(app, tail_events) == []  # updated, not added
    assert len(store.events) == 1                                # no duplicate
    survivor = next(iter(store.events.values()))
    drift_min = round((survivor.scheduled_time - original_time).total_seconds() / 60)
    assert drift_min == 4, "the fresher per-tail time should win"


def test_second_harvest_is_skipped_while_one_is_running():
    async def scenario():
        async with tracker._harvest_lock:
            assert tracker.harvest_in_progress() is True
            result = await tracker.run_harvest(FakeApp(FlightStore(), make_config()))
            return result

    result = asyncio.run(scenario())
    assert result.skipped is True
    assert result.new_legs == 0
    assert tracker.harvest_in_progress() is False   # released afterwards
