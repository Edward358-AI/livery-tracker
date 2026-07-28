"""Live telemetry: community ADS-B networks first, FR24 as coverage backstop.

adsb.fi / adsb.lol are terrestrial community networks — dense near cities and
airports, but blind over oceans and remote terrain. FR24's feed includes
satellite ADS-B, so it's queried last to fill those gaps (e.g. a SEA->LIH
flight mid-Pacific).
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

from .throttle import MISS, TTLCache
from .web import get_json

log = logging.getLogger(__name__)

# Positions go stale fast, so this window is short — just enough that a repeat
# /info (or two legs of the same aircraft polling together) reuses one fetch.
_TELEMETRY_MEMO = TTLCache(ttl_seconds=45)

# Routes for a callsign are static for the day; the registry is slow-moving.
_ROUTE_MEMO = TTLCache(ttl_seconds=3600)


def clear_caches() -> None:
    """Drop memoised positions and callsign routes (used by a rebuild)."""
    _TELEMETRY_MEMO.clear()
    _ROUTE_MEMO.clear()

SOURCES = [
    "https://opendata.adsb.fi/api/v2/registration/{reg}",
    "https://api.adsb.lol/v2/registration/{reg}",
]

ADSBDB_CALLSIGN_URL = "https://api.adsbdb.com/v0/callsign/{callsign}"


@dataclass
class Telemetry:
    lat: float
    lon: float
    alt_ft: int          # 0 when on the ground
    on_ground: bool
    gs_kts: float | None
    baro_rate: float | None  # ft/min, negative = descending
    callsign: str
    source: str


def resolve_callsign_route(callsign: str) -> tuple[str, str, str] | None:
    """(origin, destination, iata_flight_no) for a live callsign, via adsbdb.com.

    adsbdb returns the string "unknown callsign" instead of an object when it
    has no route on file, so guard the shape carefully.
    """
    callsign = callsign.strip()
    cached = _ROUTE_MEMO.get(callsign)
    if cached is not MISS:
        return cached
    body = get_json(ADSBDB_CALLSIGN_URL.format(callsign=callsign))
    response = (body or {}).get("response")
    if not isinstance(response, dict):
        return None
    route = response.get("flightroute") or {}
    origin = ((route.get("origin") or {}).get("iata_code") or "").upper()
    dest = ((route.get("destination") or {}).get("iata_code") or "").upper()
    if not origin or not dest:
        return None
    flight_no = (route.get("callsign_iata") or callsign).strip()
    resolved = (origin, dest, flight_no)
    _ROUTE_MEMO.set(callsign, resolved)
    return resolved


def fetch_telemetry(reg: str) -> Telemetry | None:
    """Latest position for a registration; None if no network is receiving it."""
    reg = reg.upper()
    cached = _TELEMETRY_MEMO.get(reg)
    if cached is not MISS:
        return cached
    telemetry = _fetch_telemetry_uncached(reg)
    _TELEMETRY_MEMO.set(reg, telemetry)
    return telemetry


def _fetch_telemetry_uncached(reg: str) -> Telemetry | None:
    for template in SOURCES:
        url = template.format(reg=reg)
        body = get_json(url)
        if not body:
            continue
        aircraft = body.get("ac") or []
        for ac in aircraft:
            lat, lon = ac.get("lat"), ac.get("lon")
            if lat is None or lon is None:
                continue
            alt_baro = ac.get("alt_baro")
            on_ground = alt_baro == "ground"
            alt_ft = 0 if on_ground else int(alt_baro) if isinstance(alt_baro, (int, float)) else 0
            return Telemetry(
                lat=float(lat),
                lon=float(lon),
                alt_ft=alt_ft,
                on_ground=on_ground,
                gs_kts=ac.get("gs"),
                baro_rate=ac.get("baro_rate"),
                callsign=(ac.get("flight") or "").strip(),
                source=url.split("/")[2],
            )
    return _fr24_telemetry(reg)


FR24_SEARCH_URL = "https://www.flightradar24.com/v1/search/web/find"
FR24_CLICK_URL = "https://data-live.flightradar24.com/clickhandler/"
FR24_MAX_STALENESS_S = 600


def _fr24_telemetry(reg: str) -> Telemetry | None:
    """FR24 live position for a registration (satellite-backed coverage)."""
    from curl_cffi import requests as curl_requests

    try:
        resp = curl_requests.get(
            FR24_SEARCH_URL,
            params={"query": reg, "limit": 10},
            impersonate="chrome",
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        live = [
            r for r in (resp.json().get("results") or [])
            if r.get("type") == "live"
            and ((r.get("detail") or {}).get("reg") or "").upper() == reg.upper()
        ]
        if not live:
            return None
        resp = curl_requests.get(
            FR24_CLICK_URL,
            params={"version": "1.5", "flight": live[0]["id"]},
            impersonate="chrome",
            timeout=20,
        )
        if resp.status_code != 200:
            return None
        body = resp.json()
        trail = body.get("trail") or []
        if not trail:
            return None
        point = trail[0]
        if time.time() - (point.get("ts") or 0) > FR24_MAX_STALENESS_S:
            return None
        alt = point.get("alt") or 0
        rate = None
        if len(trail) >= 2:
            prev = trail[1]
            dt = (point.get("ts") or 0) - (prev.get("ts") or 0)
            if dt > 0:
                rate = (alt - (prev.get("alt") or 0)) / dt * 60  # ft/min
        callsign = ((live[0].get("detail") or {}).get("callsign")) or ""
        return Telemetry(
            lat=float(point["lat"]),
            lon=float(point["lng"]),
            alt_ft=int(alt),
            on_ground=alt <= 0,
            gs_kts=float(point.get("spd") or 0),
            baro_rate=rate,
            callsign=callsign.strip(),
            source="flightradar24",
        )
    except Exception as exc:  # noqa: BLE001
        log.debug("FR24 telemetry fallback failed for %s: %s", reg, exc)
        return None
