"""Aircraft dossier cache and /info report assembly."""

from datetime import datetime, timedelta, timezone

import livery_tracker.aircraft as aircraft
from livery_tracker.adsb import Telemetry
from livery_tracker.airports import Airport
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

NOW = datetime.now(timezone.utc)

FULL_PROFILE = {
    "airline": "Alaska Airlines",
    "model": "Boeing 737-990(ER)",
    "livery": "Honoring Those Who Serve",
    "thumbnail": "https://t.plnspttrs.net/x_280.jpg",
    "photo_link": "https://www.planespotters.net/photo/1",
    "type_code": "B739",
    "manufacturer": "Boeing",
    "hex": "A2919A",
    "year": 2016,
    "owner_country": "United States",
    "operator_code": "ASA",
}


def make_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    return config


def stub_world(monkeypatch, *, resolve=None, telemetry=None, rows=None, route=None):
    monkeypatch.setattr(aircraft, "resolve_aircraft",
                        lambda reg, full=False: dict(resolve or FULL_PROFILE))
    monkeypatch.setattr(aircraft, "fetch_telemetry", lambda reg: telemetry)
    monkeypatch.setattr(aircraft, "fetch_flight_list", lambda reg: rows)
    monkeypatch.setattr(aircraft, "resolve_callsign_route", lambda cs: route)
    monkeypatch.setattr(
        aircraft.airport_db, "nearest",
        lambda lat, lon, **kw: Airport("KSFO", "SFO", "San Francisco", 37.6198, -122.3748),
    )


# -- cache ---------------------------------------------------------------------

def test_profile_is_cached_and_reused(monkeypatch):
    calls: list[str] = []

    def fake_resolve(reg, full=False):
        calls.append(reg)
        return dict(FULL_PROFILE)

    monkeypatch.setattr(aircraft, "resolve_aircraft", fake_resolve)
    first = aircraft.get_profile("N265AK")
    second = aircraft.get_profile("N265AK")

    assert calls == ["N265AK"]  # second call served from cache
    assert first["hex"] == second["hex"] == "A2919A"
    assert aircraft.load_cache()["N265AK"]["year"] == 2016


def test_refresh_forces_a_new_lookup(monkeypatch):
    calls: list[str] = []
    monkeypatch.setattr(aircraft, "resolve_aircraft",
                        lambda reg, full=False: calls.append(reg) or dict(FULL_PROFILE))
    aircraft.get_profile("N265AK")
    aircraft.get_profile("N265AK", refresh=True)
    assert len(calls) == 2


def test_cache_accumulates_and_never_loses_known_facts(monkeypatch):
    """The build year is only published while transmitting — keep it once seen."""
    monkeypatch.setattr(aircraft, "resolve_aircraft", lambda reg, full=False: dict(FULL_PROFILE))
    aircraft.get_profile("N265AK")

    # A later lookup while the aircraft is parked returns no year.
    parked = dict(FULL_PROFILE, year=None, hex="", livery="")
    monkeypatch.setattr(aircraft, "resolve_aircraft", lambda reg, full=False: parked)
    later = aircraft.get_profile("N265AK", refresh=True)

    assert later["year"] == 2016
    assert later["hex"] == "A2919A"
    assert later["livery"] == "Honoring Those Who Serve"


def test_stale_entry_is_refetched(monkeypatch):
    monkeypatch.setattr(aircraft, "resolve_aircraft", lambda reg, full=False: dict(FULL_PROFILE))
    aircraft.get_profile("N265AK")

    cache = aircraft.load_cache()
    cache["N265AK"]["updated_at"] = (NOW - timedelta(days=40)).isoformat()
    aircraft.atomic_write_json(aircraft._cache_path(), cache)

    calls: list[str] = []
    monkeypatch.setattr(aircraft, "resolve_aircraft",
                        lambda reg, full=False: calls.append(reg) or dict(FULL_PROFILE))
    aircraft.get_profile("N265AK")
    assert calls == ["N265AK"]


def test_unknown_entry_is_refetched_even_when_recent(monkeypatch):
    monkeypatch.setattr(
        aircraft, "resolve_aircraft",
        lambda reg, full=False: {"airline": "Unknown airline", "model": "Unknown type"},
    )
    aircraft.get_profile("N0000X")
    calls: list[str] = []
    monkeypatch.setattr(aircraft, "resolve_aircraft",
                        lambda reg, full=False: calls.append(reg) or dict(FULL_PROFILE))
    assert aircraft.get_profile("N0000X")["airline"] == "Alaska Airlines"
    assert calls == ["N0000X"]


def test_record_profile_seeds_cache_from_add():
    aircraft.record_profile("N8710M", {"airline": "Southwest Airlines", "model": "737 MAX 8"})
    assert aircraft.load_cache()["N8710M"]["airline"] == "Southwest Airlines"


# -- report --------------------------------------------------------------------

def test_report_shows_details_age_and_no_signal(monkeypatch):
    stub_world(monkeypatch, telemetry=None, rows=[])
    report, thumb = aircraft.build_report("N265AK", make_config(), FlightStore())

    assert "🔎 <b>N265AK</b> — Alaska Airlines" in report
    assert "Honoring Those Who Serve" in report
    assert "Boeing 737-990(ER) (B739)" in report
    assert f"Built: 2016 ({datetime.now().year - 2016} years old)" in report
    assert "A2919A" in report
    assert "No ADS-B signal" in report
    assert "Nothing scheduled" in report
    assert "Not watched" in report
    assert thumb.endswith("_280.jpg")


def test_report_describes_airborne_aircraft(monkeypatch):
    telemetry = Telemetry(lat=37.9, lon=-122.2, alt_ft=12400, on_ground=False,
                          gs_kts=310.0, baro_rate=-900, callsign="ASA1234", source="test")
    stub_world(monkeypatch, telemetry=telemetry, rows=[], route=("SEA", "SFO", "AS1234"))
    report, _ = aircraft.build_report("N265AK", make_config(), FlightStore())

    assert "Airborne at 12,400 ft" in report
    assert "310 kts" in report and "descending" in report
    assert "NM from your SFO" in report
    # No live FR24 row, so the registry route is shown but labelled as the
    # aircraft's usual one rather than asserted as fact.
    assert "Callsign ASA1234 — usual route SEA ➔ SFO" in report


def test_live_fr24_row_overrides_stale_callsign_registry(monkeypatch):
    """Observed with N559AS: adsbdb said LAS➔SFO while it actually flew SEA➔LIH."""
    telemetry = Telemetry(lat=25.0, lon=-150.0, alt_ft=38000, on_ground=False,
                          gs_kts=437.0, baro_rate=0, callsign="ASA237", source="test")
    live_row = {
        "identification": {"number": {"default": "AS237"}},
        "status": {"live": True},
        "airport": {"origin": {"code": {"iata": "SEA"}},
                    "destination": {"code": {"iata": "LIH"}}},
        "time": {"scheduled": {"departure": int(NOW.timestamp())}},
    }
    stub_world(monkeypatch, telemetry=telemetry, rows=[live_row],
               route=("LAS", "SFO", "AS237"))  # stale registry answer
    report, _ = aircraft.build_report("N559AS", make_config(), FlightStore())

    assert "Flying AS237: SEA ➔ LIH" in report
    assert "LAS" not in report


def test_report_describes_grounded_aircraft(monkeypatch):
    telemetry = Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                          gs_kts=8.0, baro_rate=None, callsign="", source="test")
    stub_world(monkeypatch, telemetry=telemetry, rows=[])
    report, _ = aircraft.build_report("N265AK", make_config(), FlightStore())
    assert "On the ground at SFO" in report


def test_report_lists_schedule_and_stars_watched_airports(monkeypatch):
    stamp = int((NOW + timedelta(hours=3)).timestamp())
    rows = [{
        "identification": {"number": {"default": "AS1234"}},
        "airport": {"origin": {"code": {"iata": "SEA"}},
                    "destination": {"code": {"iata": "SFO"}}},
        "time": {"scheduled": {"departure": stamp, "arrival": stamp + 7200}},
        "status": {"generic": {"status": {"text": "scheduled"}}},
    }]
    stub_world(monkeypatch, telemetry=None, rows=rows)
    report, _ = aircraft.build_report("N265AK", make_config(), FlightStore())

    assert "SEA➔SFO AS1234" in report
    assert "⭐" in report  # SFO is a watched airport


def test_report_includes_tracked_legs_and_watchlist_state(monkeypatch):
    stub_world(monkeypatch, telemetry=None, rows=[])
    store = FlightStore()
    store.upsert(FlightEvent(
        id="x", tail="N265AK", livery="", type=EventType.ARRIVAL, target_airport="SFO",
        scheduled_time=NOW + timedelta(hours=1), route_origin="SEA",
        route_destination="SFO", flight_number="AS1234", status=EventState.LIVE,
    ))
    config = make_config()
    config.watchlist["N265AK"] = {"airline": "Alaska Airlines"}

    report, _ = aircraft.build_report("N265AK", config, store)
    assert "🎯 Tracked today" in report
    assert "Arrival @ SFO" in report
    assert "On your watchlist" in report


def test_report_flags_unavailable_schedule_source(monkeypatch):
    stub_world(monkeypatch, telemetry=None, rows=None)  # None = all sources failed
    report, _ = aircraft.build_report("N265AK", make_config(), FlightStore())
    assert "Schedule source unavailable" in report
