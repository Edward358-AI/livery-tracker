"""Schedule harvesting from public flight-tracking sites (no paid APIs).

Primary source: Flightradar24's public flight-list JSON endpoint, fetched with
curl_cffi Chrome TLS impersonation. Fallback: scraping the FlightAware live
page for the registration.
"""

from __future__ import annotations

import json
import logging
import random
import re
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .config import Config
from .flights import EventType, FlightEvent
from .throttle import MISS, MinInterval, TTLCache

log = logging.getLogger(__name__)

FR24_LIST_URL = "https://api.flightradar24.com/common/v1/flight/list.json"
FLIGHTAWARE_URL = "https://www.flightaware.com/live/flights/{reg}"

# Rotated Chrome-family TLS fingerprints to stay under Cloudflare's radar.
IMPERSONATE_PROFILES = ["chrome", "chrome124", "edge101", "safari17_0"]

# Keep legs scheduled between (now - 1h) and (now + 24h).
WINDOW_PAST = timedelta(hours=1)
WINDOW_FUTURE = timedelta(hours=24)


def polite_delay() -> None:
    """3s +/- jitter between tail lookups, per the scraping etiquette in the spec."""
    time.sleep(3 + random.uniform(0, 2))


# Repeat lookups of the same tail within a few minutes (an impatient /info, a
# double /refresh) are served from memory, and every real request is spaced
# out — the schedule source is the one most likely to start blocking us.
_LIST_MEMO = TTLCache(ttl_seconds=300)
_LIST_SPACING = MinInterval(seconds=2.0)


def fetch_flight_list(query: str, fetch_by: str = "reg") -> list[dict[str, Any]] | None:
    """Raw FR24 schedule rows for a registration or flight number.

    Returns [] when FR24 answered but has nothing, None when every attempt
    failed — callers use that distinction to fall back / raise the alarm.
    Successful results are memoised briefly to absorb bursts.
    """
    memo_key = (query.upper(), fetch_by)
    cached = _LIST_MEMO.get(memo_key)
    if cached is not MISS:
        return cached

    last_error: str = ""
    for profile in IMPERSONATE_PROFILES:
        try:
            _LIST_SPACING.wait()
            resp = curl_requests.get(
                FR24_LIST_URL,
                params={"query": query, "fetchBy": fetch_by, "page": 1, "limit": 25},
                impersonate=profile,
                timeout=30,
            )
            if resp.status_code == 200:
                payload = resp.json()
                rows = (((payload.get("result") or {}).get("response") or {}).get("data")) or []
                _LIST_MEMO.set(memo_key, rows)
                return rows
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)
        time.sleep(1 + random.uniform(0, 2))
    log.warning("FR24 flight list failed for %s (%s)", query, last_error)
    return None


# ---------------------------------------------------------------------------
# Last-good-schedule cache (rides out intra-day FR24 outages)
# ---------------------------------------------------------------------------

CACHE_MAX_AGE = timedelta(hours=12)


def _schedule_cache_path():
    from .config import data_dir

    return data_dir() / "schedule_cache.json"


def cache_rows(reg: str, rows: list[dict[str, Any]]) -> None:
    from .config import atomic_write_json

    path = _schedule_cache_path()
    cache: dict[str, Any] = {}
    if path.exists():
        try:
            cache = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            cache = {}
    cache[reg] = {"fetched_at": datetime.now(timezone.utc).isoformat(), "rows": rows}
    atomic_write_json(path, cache)


def load_cached_rows(reg: str) -> list[dict[str, Any]] | None:
    path = _schedule_cache_path()
    if not path.exists():
        return None
    try:
        entry = json.loads(path.read_text(encoding="utf-8")).get(reg)
    except json.JSONDecodeError:
        return None
    if not entry:
        return None
    fetched_at = datetime.fromisoformat(entry["fetched_at"])
    if datetime.now(timezone.utc) - fetched_at > CACHE_MAX_AGE:
        return None
    return entry["rows"]


def extract_aircraft_meta(rows: list[dict[str, Any]]) -> dict[str, str]:
    """Airline / model / livery hints from FR24 rows.

    FR24 encodes special paint jobs in the airline name, e.g.
    "Alaska Airlines (Honoring Those Who Serve Livery)".
    """
    meta = {"airline": "", "model": "", "livery": ""}
    for row in rows:
        airline_name = ((row.get("airline") or {}).get("name")) or ""
        model_text = (((row.get("aircraft") or {}).get("model")) or {}).get("text") or ""
        if model_text and not meta["model"]:
            meta["model"] = model_text
        if airline_name and not meta["airline"]:
            match = re.match(r"^(.*?)\s*\((.+?)(?:\s+Livery)?\)\s*$", airline_name)
            if match:
                meta["airline"] = match.group(1).strip()
                meta["livery"] = match.group(2).strip()
            else:
                meta["airline"] = airline_name.strip()
        if meta["airline"] and meta["model"]:
            break
    return meta


def _leg_time(row: dict[str, Any], key: str) -> datetime | None:
    times = row.get("time") or {}
    stamp = ((times.get("estimated") or {}).get(key)) or ((times.get("scheduled") or {}).get(key))
    if not stamp:
        return None
    return datetime.fromtimestamp(stamp, tz=timezone.utc)


def rows_to_events(
    reg: str,
    livery: str,
    rows: list[dict[str, Any]],
    config: Config,
    now: datetime | None = None,
) -> list[FlightEvent]:
    """Filter FR24 rows against the configured airports and build event legs.

    A single flight can yield both a DEPARTURE (origin is watched) and an
    ARRIVAL (destination is watched) — each gets its own event/message.
    """
    now = now or datetime.now(timezone.utc)
    watched = config.airport_codes()
    events: list[FlightEvent] = []
    for row in rows:
        airport = row.get("airport") or {}
        origin = ((airport.get("origin") or {}).get("code")) or {}
        dest = ((airport.get("destination") or {}).get("code")) or {}
        origin_iata = (origin.get("iata") or "").upper()
        origin_icao = (origin.get("icao") or "").upper()
        dest_iata = (dest.get("iata") or "").upper()
        dest_icao = (dest.get("icao") or "").upper()
        flight_no = (((row.get("identification") or {}).get("number")) or {}).get("default") or ""

        legs: list[tuple[EventType, str, datetime | None]] = []
        if watched & {dest_iata, dest_icao}:
            match = config.airport_for_code(dest_iata or dest_icao)
            if match:
                legs.append((EventType.ARRIVAL, match[0], _leg_time(row, "arrival")))
        if watched & {origin_iata, origin_icao}:
            match = config.airport_for_code(origin_iata or origin_icao)
            if match:
                legs.append((EventType.DEPARTURE, match[0], _leg_time(row, "departure")))

        for ev_type, target_iata, when in legs:
            if when is None or not (now - WINDOW_PAST <= when <= now + WINDOW_FUTURE):
                continue
            events.append(
                FlightEvent(
                    id=FlightEvent.make_id(reg, ev_type, when, target_iata),
                    tail=reg,
                    livery=livery,
                    type=ev_type,
                    target_airport=target_iata,
                    scheduled_time=when,
                    route_origin=origin_iata or origin_icao or "???",
                    route_destination=dest_iata or dest_icao or "???",
                    flight_number=flight_no,
                )
            )
    return events


def current_flight(rows: list[dict[str, Any]]) -> dict[str, str] | None:
    """The flight FR24 currently marks live, if any.

    Preferred over the adsbdb callsign lookup for "where is it going right
    now", because community route databases lag real schedule changes.
    """
    for row in rows:
        if not ((row.get("status") or {}).get("live")):
            continue
        airport = row.get("airport") or {}
        origin = ((airport.get("origin") or {}).get("code")) or {}
        dest = ((airport.get("destination") or {}).get("code")) or {}
        return {
            "flight_number": (((row.get("identification") or {}).get("number")) or {}).get("default") or "",
            "origin": (origin.get("iata") or origin.get("icao") or "???").upper(),
            "destination": (dest.get("iata") or dest.get("icao") or "???").upper(),
        }
    return None


def upcoming_flights(
    rows: list[dict[str, Any]], now: datetime | None = None, limit: int = 6
) -> list[dict[str, Any]]:
    """Flatten FR24 rows into the near-future itinerary shown by /info."""
    now = now or datetime.now(timezone.utc)
    flights: list[dict[str, Any]] = []
    for row in rows:
        departure = _leg_time(row, "departure")
        arrival = _leg_time(row, "arrival")
        when = departure or arrival
        if when is None or not (now - timedelta(hours=2) <= when <= now + WINDOW_FUTURE):
            continue
        airport = row.get("airport") or {}
        origin = ((airport.get("origin") or {}).get("code")) or {}
        dest = ((airport.get("destination") or {}).get("code")) or {}
        flights.append({
            "flight_number": (((row.get("identification") or {}).get("number")) or {}).get("default") or "",
            "origin": (origin.get("iata") or origin.get("icao") or "???").upper(),
            "destination": (dest.get("iata") or dest.get("icao") or "???").upper(),
            "departure": departure,
            "arrival": arrival,
            "cancelled": row_is_cancelled(row),
        })
    flights.sort(key=lambda f: f["departure"] or f["arrival"])
    return flights[:limit]


def row_is_cancelled(row: dict[str, Any]) -> bool:
    status = (((row.get("status") or {}).get("generic")) or {}).get("status") or {}
    return "cancel" in str(status.get("text", "")).lower()


@dataclass
class LegRefresh:
    new_time: datetime | None
    cancelled: bool = False


def _best_leg_row(
    rows: list[dict[str, Any]], event: FlightEvent
) -> tuple[datetime, dict[str, Any]] | None:
    """The row matching a leg's route with the closest schedule time (<6h drift)."""
    key = "arrival" if event.type == EventType.ARRIVAL else "departure"
    best: tuple[float, datetime, dict[str, Any]] | None = None
    for row in rows:
        airport = row.get("airport") or {}
        origin = (((airport.get("origin") or {}).get("code")) or {}).get("iata", "") or ""
        dest = (((airport.get("destination") or {}).get("code")) or {}).get("iata", "") or ""
        if event.type == EventType.ARRIVAL and dest.upper() != event.route_destination:
            continue
        if event.type == EventType.DEPARTURE and origin.upper() != event.route_origin:
            continue
        when = _leg_time(row, key)
        if when is None:
            continue
        drift = abs((when - event.scheduled_time).total_seconds())
        if drift < 6 * 3600 and (best is None or drift < best[0]):
            best = (drift, when, row)
    return (best[1], best[2]) if best else None


def refresh_leg_time(reg: str, event: FlightEvent) -> LegRefresh:
    """T-2h re-scrape: current best ETA/ETD (and cancellation flag) for a leg.

    FR24 unassigns the registration from cancelled flights, so if the by-reg
    list no longer carries this leg, re-check by flight number — that's where
    a cancellation will show up.
    """
    best = _best_leg_row(fetch_flight_list(reg) or [], event)
    if best is None and event.flight_number:
        best = _best_leg_row(
            fetch_flight_list(event.flight_number, fetch_by="flight") or [], event
        )
    if best is None:
        return LegRefresh(None)
    return LegRefresh(best[0], cancelled=row_is_cancelled(best[1]))


# ---------------------------------------------------------------------------
# FlightAware fallback (best effort — used only if FR24 returns nothing)
# ---------------------------------------------------------------------------

def fetch_flightaware_events(
    reg: str, livery: str, config: Config, now: datetime | None = None
) -> list[FlightEvent]:
    """Scrape FlightAware's live page for the registration as a fallback.

    FlightAware embeds a `trackpollBootstrap` JSON blob in the page with the
    active/near-term flights for the ident.
    """
    now = now or datetime.now(timezone.utc)
    try:
        resp = curl_requests.get(
            FLIGHTAWARE_URL.format(reg=reg), impersonate="chrome", timeout=30
        )
        if resp.status_code != 200:
            log.warning("FlightAware fallback for %s: HTTP %s", reg, resp.status_code)
            return []
        soup = BeautifulSoup(resp.text, "html.parser")
        blob = None
        for script in soup.find_all("script"):
            text = script.string or ""
            if "trackpollBootstrap" in text:
                match = re.search(r"trackpollBootstrap\s*=\s*(\{.*?\});", text, re.DOTALL)
                if match:
                    blob = json.loads(match.group(1))
                break
        if not blob:
            return []
        events: list[FlightEvent] = []
        watched = config.airport_codes()
        for flight in (blob.get("flights") or {}).values():
            origin = ((flight.get("origin") or {}).get("iata")) or ((flight.get("origin") or {}).get("icao")) or ""
            dest = ((flight.get("destination") or {}).get("iata")) or ((flight.get("destination") or {}).get("icao")) or ""
            take_off = ((flight.get("gateDepartureTimes") or {}).get("scheduled"))
            landing = ((flight.get("gateArrivalTimes") or {}).get("scheduled"))
            flight_no = flight.get("codeShare", {}).get("iataIdent") or flight.get("friendlyIdent") or ""

            for ev_type, code, stamp in (
                (EventType.ARRIVAL, dest, landing),
                (EventType.DEPARTURE, origin, take_off),
            ):
                if not stamp or code.upper() not in watched:
                    continue
                match_ap = config.airport_for_code(code)
                if not match_ap:
                    continue
                when = datetime.fromtimestamp(stamp, tz=timezone.utc)
                if not (now - WINDOW_PAST <= when <= now + WINDOW_FUTURE):
                    continue
                events.append(
                    FlightEvent(
                        id=FlightEvent.make_id(reg, ev_type, when, match_ap[0]),
                        tail=reg,
                        livery=livery,
                        type=ev_type,
                        target_airport=match_ap[0],
                        scheduled_time=when,
                        route_origin=origin.upper() or "???",
                        route_destination=dest.upper() or "???",
                        flight_number=flight_no,
                    )
                )
        return events
    except Exception as exc:  # noqa: BLE001
        log.warning("FlightAware fallback failed for %s: %s", reg, exc)
        return []


def harvest_tail(reg: str, livery: str, config: Config) -> tuple[list[FlightEvent], bool]:
    """All watched-airport legs for one tail, plus whether any source answered.

    Order: FR24 live -> today's cached FR24 rows -> FlightAware scrape.
    (events, False) means every source failed — the caller should treat the
    tail as unknown, not as "no flights today".
    """
    rows = fetch_flight_list(reg)
    if rows is not None:
        cache_rows(reg, rows)
        return rows_to_events(reg, livery, rows, config), True
    cached = load_cached_rows(reg)
    if cached is not None:
        log.info("FR24 unreachable — using cached schedule for %s", reg)
        return rows_to_events(reg, livery, cached, config), True
    events = fetch_flightaware_events(reg, livery, config)
    if events:
        return events, True
    return [], False
