import json
from datetime import datetime, timezone
from pathlib import Path

from livery_tracker.config import Config
from livery_tracker.flights import EventType
from livery_tracker.schedule_provider import extract_aircraft_meta, rows_to_events

FIXTURE = Path(__file__).parent / "fixtures" / "fr24_list_sample.json"


def fixture_rows():
    payload = json.loads(FIXTURE.read_text(encoding="utf-8"))
    return payload["result"]["response"]["data"]


def san_config() -> Config:
    config = Config()
    config.target_airports["SAN"] = {
        "icao": "KSAN",
        "name": "San Diego International Airport",
        "lat": 32.7336,
        "lon": -117.1897,
    }
    return config


def test_extract_aircraft_meta_parses_livery_from_airline_name():
    meta = extract_aircraft_meta(fixture_rows())
    assert meta["airline"] == "Alaska Airlines"
    assert meta["livery"] == "Honoring Those Who Serve"
    assert meta["model"] == "Boeing 737-990(ER)"


def test_rows_to_events_builds_arrival_and_departure_legs():
    # Fixture: RDU->SAN (arrival 1785385560) and SAN->RDU (departure 1785343260)
    now = datetime.fromtimestamp(1785340000, tz=timezone.utc)
    events = rows_to_events("N265AK", "Honoring Those Who Serve", fixture_rows(), san_config(), now=now)
    assert len(events) == 2

    arrivals = [e for e in events if e.type == EventType.ARRIVAL]
    departures = [e for e in events if e.type == EventType.DEPARTURE]
    assert len(arrivals) == 1 and len(departures) == 1

    arr = arrivals[0]
    assert arr.target_airport == "SAN"
    assert arr.route_origin == "RDU"
    assert arr.route_destination == "SAN"
    assert arr.flight_number == "AS474"
    assert int(arr.scheduled_time.timestamp()) == 1785385560

    dep = departures[0]
    assert dep.target_airport == "SAN"
    assert dep.route_origin == "SAN"
    assert int(dep.scheduled_time.timestamp()) == 1785343260


def test_rows_to_events_matches_by_icao_code_too():
    config = Config()
    config.target_airports["SAN"] = {"icao": "KSAN", "name": "San Diego", "lat": 32.7, "lon": -117.2}
    now = datetime.fromtimestamp(1785340000, tz=timezone.utc)
    rows = fixture_rows()
    # Strip IATA codes so only ICAO matching can succeed
    for row in rows:
        for side in ("origin", "destination"):
            row["airport"][side]["code"]["iata"] = ""
    events = rows_to_events("N265AK", "", rows, config, now=now)
    assert {e.type for e in events} == {EventType.ARRIVAL, EventType.DEPARTURE}


def test_rows_to_events_ignores_flights_outside_window():
    far_future = datetime.fromtimestamp(1785340000 - 5 * 86400, tz=timezone.utc)
    events = rows_to_events("N265AK", "", fixture_rows(), san_config(), now=far_future)
    assert events == []


def test_rows_to_events_no_watched_airports_matches_nothing():
    config = Config()
    config.target_airports["SFO"] = {"icao": "KSFO", "name": "SF", "lat": 37.6, "lon": -122.4}
    now = datetime.fromtimestamp(1785340000, tz=timezone.utc)
    assert rows_to_events("N265AK", "", fixture_rows(), config, now=now) == []
