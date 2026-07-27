"""Live ADS-B telemetry from the free community networks (adsb.fi, adsb.lol)."""

from __future__ import annotations

import logging
from dataclasses import dataclass

from .web import get_json

log = logging.getLogger(__name__)

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
    body = get_json(ADSBDB_CALLSIGN_URL.format(callsign=callsign.strip()))
    response = (body or {}).get("response")
    if not isinstance(response, dict):
        return None
    route = response.get("flightroute") or {}
    origin = ((route.get("origin") or {}).get("iata_code") or "").upper()
    dest = ((route.get("destination") or {}).get("iata_code") or "").upper()
    if not origin or not dest:
        return None
    flight_no = (route.get("callsign_iata") or callsign).strip()
    return origin, dest, flight_no


def fetch_telemetry(reg: str) -> Telemetry | None:
    """Latest position for a registration; None if no network is receiving it."""
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
    return None
