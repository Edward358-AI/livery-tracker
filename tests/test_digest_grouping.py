"""Digest grouping modes: type (default), airport, airline."""

from datetime import datetime, timedelta, timezone

from livery_tracker.config import Config
from livery_tracker.digest import GROUP_MODES, render_digest
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

BASE = datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc)


def make_config(group_by: str = "type") -> Config:
    config = Config(digest_group_by=group_by)
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6, "lon": -122.4},
        "SJC": {"icao": "KSJC", "name": "San Jose", "lat": 37.4, "lon": -121.9},
    }
    config.watchlist = {
        "N265AK": {"airline": "Alaska Airlines", "model": "B739", "livery": ""},
        "N8658A": {"airline": "Southwest Airlines", "model": "B737", "livery": ""},
        "N642FR": {"airline": "Frontier", "model": "A320", "livery": "Hugh the Manatee"},
    }
    return config


def leg(tail, ev_type, airport, origin, dest, flight, minutes, **kw) -> FlightEvent:
    return FlightEvent(
        id=f"{tail}-{ev_type.value}-{flight}-{airport}",
        tail=tail,
        livery=kw.get("livery", ""),
        type=ev_type,
        target_airport=airport,
        scheduled_time=BASE + timedelta(minutes=minutes),
        route_origin=origin,
        route_destination=dest,
        flight_number=flight,
        status=kw.get("status", EventState.WAITING_2H),
    )


def populated_store() -> FlightStore:
    """Alaska SFO arrival, Southwest SJC departure, and a Frontier SFO->SJC hop."""
    store = FlightStore()
    store.upsert(leg("N265AK", EventType.ARRIVAL, "SFO", "SEA", "SFO", "AS1234", 60))
    store.upsert(leg("N8658A", EventType.DEPARTURE, "SJC", "SJC", "LAS", "WN400", 30))
    # Same flight, both endpoints watched -> a mergeable pair.
    store.upsert(leg("N642FR", EventType.DEPARTURE, "SFO", "SFO", "SJC", "F9100", 10))
    store.upsert(leg("N642FR", EventType.ARRIVAL, "SJC", "SFO", "SJC", "F9100", 50))
    return store


def sections_of(text: str) -> list[str]:
    """Section header lines (bold titles), in order."""
    return [
        line for line in text.splitlines()
        if line.startswith(("🔁", "🛬", "🛫", "🏢")) and "<b>" in line
    ]


# -- default: by type ----------------------------------------------------------

def test_group_by_type_merges_and_splits_arrivals_departures():
    text = render_digest(populated_store(), make_config("type"))
    assert sections_of(text) == [
        "🔁 <b>Between your airports</b>",
        "🛬 <b>Arrivals</b>",
        "🛫 <b>Departures</b>",
    ]
    assert "N642FR" in text and text.count("N642FR") == 1  # merged into one line
    assert "@ SFO" in text  # airport shown inline in this mode


# -- by airport ----------------------------------------------------------------

def test_group_by_airport_sections_every_airport_with_all_traffic():
    text = render_digest(populated_store(), make_config("airport"))
    assert sections_of(text) == [
        "🛬🛫 <b>SFO</b> — San Francisco",
        "🛬🛫 <b>SJC</b> — San Jose",
    ]
    sfo, sjc = text.split("🛬🛫 <b>SJC</b>")
    # SFO carries an arrival and a departure; SJC likewise.
    assert "N265AK" in sfo and "N642FR" in sfo
    assert "N8658A" in sjc and "N642FR" in sjc
    # The two-airport hop stays unmerged so it appears at both ends.
    assert text.count("N642FR") == 2
    # Header already names the airport, so lines omit the redundant suffix.
    assert "@ SFO" not in text and "@ SJC" not in text


def test_group_by_airport_orders_legs_by_time():
    text = render_digest(populated_store(), make_config("airport"))
    sfo_block = text.split("🛬🛫 <b>SJC</b>")[0]
    assert sfo_block.index("N642FR") < sfo_block.index("N265AK")  # +10m before +60m


def test_group_by_airport_keeps_legs_from_removed_airports():
    store = populated_store()
    config = make_config("airport")
    config.target_airports.pop("SJC")  # airport removed after harvest
    text = render_digest(store, config)
    assert "🛬🛫 <b>SJC</b>" in text  # still grouped, just without a name


# -- by airline ----------------------------------------------------------------

def test_group_by_airline_sections_each_carrier():
    text = render_digest(populated_store(), make_config("airline"))
    assert sections_of(text) == [
        "🏢 <b>Alaska Airlines</b>",
        "🏢 <b>Frontier</b>",
        "🏢 <b>Southwest Airlines</b>",
    ]
    assert text.count("N642FR") == 1  # merged, like the default mode


def test_group_by_airline_falls_back_for_unknown_tails():
    store = FlightStore()
    store.upsert(leg("N999ZZ", EventType.ARRIVAL, "SFO", "LAX", "SFO", "XX1", 15))
    text = render_digest(store, make_config("airline"))  # tail not in watchlist
    assert "🏢 <b>Unknown airline</b>" in text


# -- shared behaviour ----------------------------------------------------------

def test_every_mode_renders_header_footer_and_all_tails():
    for mode in GROUP_MODES:
        text = render_digest(populated_store(), make_config(mode))
        assert "LIVERY DIGEST" in text
        assert "Updated" in text
        for tail in ("N265AK", "N8658A", "N642FR"):
            assert tail in text, f"{tail} missing in {mode} mode"


def test_unknown_mode_falls_back_to_type():
    text = render_digest(populated_store(), make_config("nonsense"))
    assert "🛬 <b>Arrivals</b>" in text


def test_empty_digest_in_every_mode():
    for mode in GROUP_MODES:
        text = render_digest(FlightStore(), make_config(mode))
        assert "No watched aircraft scheduled" in text


def test_group_mode_persists_across_reload():
    config = make_config("airline")
    config.save()
    assert Config.load().digest_group_by == "airline"


def test_legacy_config_without_mode_defaults_to_type():
    config = make_config("type")
    config.save()
    path = __import__("livery_tracker.config", fromlist=["config_file"]).config_file()
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["digest_group_by"]  # config written before this feature existed
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Config.load().digest_group_by == "type"
