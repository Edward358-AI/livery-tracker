"""Aircraft metadata resolution, including parked aircraft (the N8710M case)."""

import asyncio

import livery_tracker.resolver as resolver
import livery_tracker.tracker as tracker
from livery_tracker.config import Config
from livery_tracker.resolver import _adsbdb_aircraft, resolve_aircraft

# Real adsbdb shape for N8710M (Southwest 737 MAX 8, parked when queried).
ADSBDB_N8710M = {
    "response": {
        "aircraft": {
            "type": "737MAX 8",
            "icao_type": "B38M",
            "manufacturer": "Boeing",
            "mode_s": "ABFC71",
            "registration": "N8710M",
            "registered_owner": "Southwest Airlines",
            "registered_owner_country_name": "United States",
            "registered_owner_operator_flag_code": "SWA",
            "url_photo": None,
        }
    }
}

PLANESPOTTERS_ONE_PHOTO = {
    "photos": [{
        "thumbnail_large": {"src": "https://t.plnspttrs.net/x_280.jpg"},
        "link": "https://www.planespotters.net/photo/1926638/n8710m",
    }]
}


def stub_sources(monkeypatch, *, planespotters=None, adsbdb=None, adsbfi=None, rows=None):
    """Point every network source in the resolver at canned payloads."""
    def fake_get_json(url, **kwargs):
        if "planespotters" in url:
            return planespotters
        if "adsbdb" in url:
            return adsbdb
        if "adsb.fi" in url:
            return adsbfi
        return None

    monkeypatch.setattr(resolver, "get_json", fake_get_json)
    monkeypatch.setattr(resolver, "fetch_flight_list", lambda reg: rows)


# -- adsbdb registry parsing ---------------------------------------------------

def test_adsbdb_aircraft_parses_registry(monkeypatch):
    monkeypatch.setattr(resolver, "get_json", lambda url, **kw: ADSBDB_N8710M)
    assert _adsbdb_aircraft("N8710M") == {
        "airline": "Southwest Airlines",
        "model": "Boeing 737MAX 8",
        "type_code": "B38M",
        "manufacturer": "Boeing",
        "hex": "ABFC71",
        "owner_country": "United States",
        "operator_code": "SWA",
    }


def test_adsbdb_aircraft_handles_unknown_registration(monkeypatch):
    # adsbdb answers with a bare string, not an object, for unknown regs.
    monkeypatch.setattr(resolver, "get_json", lambda url, **kw: {"response": "unknown aircraft"})
    assert _adsbdb_aircraft("N0000X") == {}
    monkeypatch.setattr(resolver, "get_json", lambda url, **kw: None)
    assert _adsbdb_aircraft("N0000X") == {}


# -- the reported bug ----------------------------------------------------------

def test_parked_aircraft_resolves_from_registry(monkeypatch):
    """N8710M: photo exists, but FR24 and live ADS-B are both empty."""
    stub_sources(
        monkeypatch,
        planespotters=PLANESPOTTERS_ONE_PHOTO,
        rows=[],                       # FR24 has no recent flights
        adsbdb=ADSBDB_N8710M,
        adsbfi={"ac": []},             # transponder off
    )
    info = resolve_aircraft("N8710M")
    assert info["airline"] == "Southwest Airlines"
    assert info["model"] == "Boeing 737MAX 8"
    assert info["thumbnail"].endswith("_280.jpg")


def test_fr24_metadata_still_wins_over_registry(monkeypatch):
    """FR24 is preferred: it carries the livery name, which the registry lacks."""
    rows = [{
        "airline": {"name": "Alaska Airlines (Retro Livery)"},
        "aircraft": {"model": {"text": "Boeing 737-990(ER)"}},
    }]
    stub_sources(monkeypatch, planespotters=None, rows=rows, adsbdb=ADSBDB_N8710M)
    info = resolve_aircraft("N265AK")
    assert info["airline"] == "Alaska Airlines"
    assert info["model"] == "Boeing 737-990(ER)"
    assert info["livery"] == "Retro"


def test_totally_unknown_aircraft_keeps_placeholders(monkeypatch):
    stub_sources(monkeypatch, rows=None, adsbdb=None, adsbfi=None)
    info = resolve_aircraft("N0000X")
    assert info["airline"] == "Unknown airline"
    assert info["model"] == "Unknown type"


# -- self-healing of already-stored "Unknown" entries ---------------------------

def test_heal_fills_in_previously_unknown_entries(monkeypatch):
    config = Config()
    config.watchlist = {
        "N8710M": {"airline": "Unknown airline", "model": "Unknown type", "livery": ""},
        "N265AK": {"airline": "Alaska Airlines", "model": "B739", "livery": "Retro"},
    }
    called: list[str] = []

    def fake_resolve(reg):
        called.append(reg)
        return {
            "airline": "Southwest Airlines",
            "model": "Boeing 737MAX 8",
            "livery": "Imua One",
            "thumbnail": "https://t.plnspttrs.net/x_280.jpg",
        }

    monkeypatch.setattr(tracker, "resolve_aircraft", fake_resolve)
    healed = asyncio.run(tracker.heal_unknown_metadata(config))

    assert healed == 1
    assert called == ["N8710M"]  # the healthy entry is left alone
    assert config.watchlist["N8710M"]["airline"] == "Southwest Airlines"
    assert config.watchlist["N8710M"]["livery"] == "Imua One"
    assert config.watchlist["N265AK"]["livery"] == "Retro"  # untouched
    assert Config.load().watchlist["N8710M"]["model"] == "Boeing 737MAX 8"  # persisted


def test_heal_is_a_noop_when_nothing_is_unknown(monkeypatch):
    """Healthy watchlists must cost zero network calls on every harvest."""
    config = Config()
    config.watchlist = {"N265AK": {"airline": "Alaska Airlines", "model": "B739"}}
    calls: list[str] = []
    monkeypatch.setattr(tracker, "resolve_aircraft", lambda reg: calls.append(reg) or {})

    assert asyncio.run(tracker.heal_unknown_metadata(config)) == 0
    assert calls == []


def test_heal_leaves_entry_alone_when_still_unresolvable(monkeypatch):
    config = Config()
    config.watchlist = {"N0000X": {"airline": "Unknown airline", "model": "Unknown type"}}
    monkeypatch.setattr(
        tracker, "resolve_aircraft",
        lambda reg: {"airline": "Unknown airline", "model": "Unknown type",
                     "livery": "", "thumbnail": ""},
    )
    assert asyncio.run(tracker.heal_unknown_metadata(config)) == 0
    assert config.watchlist["N0000X"]["airline"] == "Unknown airline"
