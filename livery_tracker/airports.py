"""Airport geocoding backed by the free OurAirports dataset (cached locally)."""

from __future__ import annotations

import csv
import logging
import time
from dataclasses import dataclass
from io import StringIO

import httpx

from .config import data_dir
from .web import USER_AGENT

log = logging.getLogger(__name__)

OURAIRPORTS_CSV_URL = "https://davidmegginson.github.io/ourairports-data/airports.csv"
CACHE_MAX_AGE_DAYS = 90

# Bigger fields win when several airports share an IATA code.
_TYPE_RANK = {
    "large_airport": 4,
    "medium_airport": 3,
    "small_airport": 2,
    "seaplane_base": 1,
    "heliport": 0,
    "closed": -1,
}


@dataclass
class Airport:
    icao: str
    iata: str
    name: str
    lat: float
    lon: float
    rank: int = 0  # size score from _TYPE_RANK (+1 for scheduled service)


_index: dict[str, Airport] | None = None


def _cache_path():
    return data_dir() / "airports.csv"


def _download_csv() -> str | None:
    log.info("Downloading OurAirports airport database (one-time, ~9 MB)...")
    try:
        resp = httpx.get(
            OURAIRPORTS_CSV_URL,
            headers={"User-Agent": USER_AGENT},
            timeout=120,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.error("OurAirports download failed: HTTP %s", resp.status_code)
            return None
        return resp.text
    except Exception as exc:  # noqa: BLE001
        log.error("OurAirports download failed: %s", exc)
        return None


def _load_csv_text() -> str | None:
    path = _cache_path()
    if path.exists():
        age_days = (time.time() - path.stat().st_mtime) / 86400
        if age_days < CACHE_MAX_AGE_DAYS:
            return path.read_text(encoding="utf-8")
    text = _download_csv()
    if text:
        path.write_text(text, encoding="utf-8")
        return text
    if path.exists():  # stale cache beats nothing
        return path.read_text(encoding="utf-8")
    return None


def _build_index() -> dict[str, Airport]:
    text = _load_csv_text()
    index: dict[str, Airport] = {}
    if not text:
        return index
    rank: dict[str, int] = {}
    for row in csv.DictReader(StringIO(text)):
        try:
            lat, lon = float(row["latitude_deg"]), float(row["longitude_deg"])
        except (ValueError, KeyError):
            continue
        icao = (row.get("icao_code") or row.get("ident") or "").strip().upper()
        iata = (row.get("iata_code") or "").strip().upper()
        r = _TYPE_RANK.get(row.get("type", ""), 0) + (1 if row.get("scheduled_service") == "yes" else 0)
        airport = Airport(
            icao=icao, iata=iata, name=row.get("name", "").strip(), lat=lat, lon=lon, rank=r
        )
        for code in {icao, iata, (row.get("ident") or "").strip().upper()} - {""}:
            if r >= rank.get(code, -99):
                rank[code] = r
                index[code] = airport
    return index


def lookup(code: str) -> Airport | None:
    """Resolve an IATA or ICAO code to an Airport, or None if unknown."""
    global _index
    if _index is None:
        _index = _build_index()
    return _index.get(code.strip().upper())


def nearest(lat: float, lon: float, min_rank: int = 3) -> Airport | None:
    """Closest airport to a position (default: medium-size or better).

    Used to name a diversion airport, so small strips and heliports are
    excluded by rank to avoid mislabeling.
    """
    from .geo import haversine_nm

    global _index
    if _index is None:
        _index = _build_index()
    best: Airport | None = None
    best_dist = float("inf")
    seen: set[int] = set()
    for airport in _index.values():
        if id(airport) in seen:
            continue
        seen.add(id(airport))
        if airport.rank < min_rank:
            continue
        dist = haversine_nm(lat, lon, airport.lat, airport.lon)
        if dist < best_dist:
            best, best_dist = airport, dist
    return best
