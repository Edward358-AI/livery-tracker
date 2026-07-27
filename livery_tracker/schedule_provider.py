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
from datetime import datetime, timedelta, timezone
from typing import Any

from bs4 import BeautifulSoup
from curl_cffi import requests as curl_requests

from .config import Config
from .flights import EventType, FlightEvent

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


def fetch_flight_list(reg: str) -> list[dict[str, Any]]:
    """Raw FR24 schedule rows for a registration (newest first). [] on failure."""
    last_error: str = ""
    for profile in IMPERSONATE_PROFILES:
        try:
            resp = curl_requests.get(
                FR24_LIST_URL,
                params={"query": reg, "fetchBy": "reg", "page": 1, "limit": 25},
                impersonate=profile,
                timeout=30,
            )
            if resp.status_code == 200:
                payload = resp.json()
                data = (((payload.get("result") or {}).get("response") or {}).get("data")) or []
                return data
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:  # noqa: BLE001
            last_error = repr(exc)
        time.sleep(1 + random.uniform(0, 2))
    log.warning("FR24 flight list failed for %s (%s)", reg, last_error)
    return []


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


def refresh_leg_time(reg: str, event: FlightEvent) -> datetime | None:
    """T-2h re-scrape: current best ETA/ETD for an existing leg, or None."""
    rows = fetch_flight_list(reg)
    key = "arrival" if event.type == EventType.ARRIVAL else "departure"
    best: tuple[float, datetime] | None = None
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
            best = (drift, when)
    return best[1] if best else None


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


def harvest_tail(reg: str, livery: str, config: Config) -> list[FlightEvent]:
    """All watched-airport legs for one tail: FR24 first, FlightAware fallback."""
    rows = fetch_flight_list(reg)
    if rows:
        return rows_to_events(reg, livery, rows, config)
    return fetch_flightaware_events(reg, livery, config)
