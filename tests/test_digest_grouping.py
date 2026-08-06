"""Digest grouping modes: airport (default), airline-per-airport, flat type."""

from datetime import datetime, timedelta, timezone

from livery_tracker.config import Config
from livery_tracker.digest import DEFAULT_GROUP_MODE, GROUP_MODES, render_digest
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

BASE = datetime(2026, 7, 27, 16, 0, tzinfo=timezone.utc)


def make_config(group_by: str = DEFAULT_GROUP_MODE) -> Config:
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
    # Same flight, both endpoints watched -> mergeable in the flat type view.
    store.upsert(leg("N642FR", EventType.DEPARTURE, "SFO", "SFO", "SJC", "F9100", 10))
    store.upsert(leg("N642FR", EventType.ARRIVAL, "SJC", "SFO", "SJC", "F9100", 50))
    return store


def sections_of(text: str) -> list[str]:
    """Section header lines (bold titles), in order."""
    return [
        line for line in text.splitlines()
        if line.startswith(("🔁", "🛬", "🛫", "🏢")) and "<b>" in line
    ]


def subheaders_of(block: str) -> list[str]:
    """Italic sub-header lines inside one airport section, in order."""
    return [line for line in block.splitlines() if "<i>" in line and line[0] in "🛬🛫🏢"]


# -- default: by airport --------------------------------------------------------

def test_default_mode_is_airport():
    assert DEFAULT_GROUP_MODE == "airport"
    assert Config().digest_group_by == "airport"
    assert next(iter(GROUP_MODES)) == "airport"  # listed first in /view


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


def test_group_by_airport_splits_arrivals_and_departures():
    text = render_digest(populated_store(), make_config("airport"))
    sfo, sjc = text.split("🛬🛫 <b>SJC</b>")
    assert subheaders_of(sfo) == ["🛬 <i>Arrivals</i>", "🛫 <i>Departures</i>"]
    assert subheaders_of(sjc) == ["🛬 <i>Arrivals</i>", "🛫 <i>Departures</i>"]
    # Arrivals block first: the SFO arrival precedes the SFO departure even
    # though the departure is scheduled 50 minutes earlier.
    assert sfo.index("N265AK") < sfo.index("N642FR")


def test_group_by_airport_omits_an_empty_direction():
    store = FlightStore()
    store.upsert(leg("N265AK", EventType.ARRIVAL, "SFO", "SEA", "SFO", "AS1234", 60))
    text = render_digest(store, make_config("airport"))
    assert "🛬 <i>Arrivals</i>" in text
    assert "🛫 <i>Departures</i>" not in text  # nothing departing -> no header


def test_group_by_airport_orders_legs_by_time_within_a_direction():
    store = populated_store()
    store.upsert(leg("N8658A", EventType.ARRIVAL, "SFO", "LAS", "SFO", "WN401", 20))
    text = render_digest(store, make_config("airport"))
    sfo_block = text.split("🛬🛫 <b>SJC</b>")[0]
    arrivals = sfo_block.split("🛫 <i>Departures</i>")[0]
    assert arrivals.index("WN401") < arrivals.index("AS1234")  # +20m before +60m


def test_group_by_airport_keeps_legs_from_removed_airports():
    store = populated_store()
    config = make_config("airport")
    config.target_airports.pop("SJC")  # airport removed after harvest
    text = render_digest(store, config)
    assert "🛬🛫 <b>SJC</b>" in text  # still grouped, just without a name


# -- by airline (within each airport) -------------------------------------------

def test_group_by_airline_nests_carriers_under_each_airport():
    text = render_digest(populated_store(), make_config("airline"))
    assert sections_of(text) == [
        "🛬🛫 <b>SFO</b> — San Francisco",
        "🛬🛫 <b>SJC</b> — San Jose",
    ]
    sfo, sjc = text.split("🛬🛫 <b>SJC</b>")
    assert subheaders_of(sfo) == ["🏢 <i>Alaska Airlines</i>", "🏢 <i>Frontier</i>"]
    assert subheaders_of(sjc) == ["🏢 <i>Frontier</i>", "🏢 <i>Southwest Airlines</i>"]
    # Per-airport views never merge: the hop shows at both ends.
    assert text.count("N642FR") == 2


def test_group_by_airline_falls_back_for_unknown_tails():
    store = FlightStore()
    store.upsert(leg("N999ZZ", EventType.ARRIVAL, "SFO", "LAX", "SFO", "XX1", 15))
    text = render_digest(store, make_config("airline"))  # tail not in watchlist
    assert "🏢 <i>Unknown airline</i>" in text


# -- flat type view -------------------------------------------------------------

def test_group_by_type_merges_and_splits_arrivals_departures():
    text = render_digest(populated_store(), make_config("type"))
    assert sections_of(text) == [
        "🔁 <b>Between your airports</b>",
        "🛬 <b>Arrivals</b>",
        "🛫 <b>Departures</b>",
    ]
    assert "N642FR" in text and text.count("N642FR") == 1  # merged into one line
    assert "@ SFO" in text  # airport shown inline in this mode


# -- shared behaviour ----------------------------------------------------------

def test_every_mode_renders_header_footer_and_all_tails():
    for mode in GROUP_MODES:
        text = render_digest(populated_store(), make_config(mode))
        assert "LIVERY DIGEST" in text
        assert "Updated" in text
        for tail in ("N265AK", "N8658A", "N642FR"):
            assert tail in text, f"{tail} missing in {mode} mode"


def test_unknown_mode_falls_back_to_the_default():
    text = render_digest(populated_store(), make_config("nonsense"))
    assert "🛬🛫 <b>SFO</b>" in text  # airport view, the default


def test_empty_digest_in_every_mode():
    for mode in GROUP_MODES:
        text = render_digest(FlightStore(), make_config(mode))
        assert "No watched aircraft scheduled" in text


def test_group_mode_persists_across_reload():
    config = make_config("type")
    config.save()
    assert Config.load().digest_group_by == "type"


def test_legacy_config_without_mode_gets_the_default():
    config = make_config("airport")
    config.save()
    path = __import__("livery_tracker.config", fromlist=["config_file"]).config_file()
    import json
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["digest_group_by"]  # config written before this feature existed
    path.write_text(json.dumps(raw), encoding="utf-8")
    assert Config.load().digest_group_by == "airport"
