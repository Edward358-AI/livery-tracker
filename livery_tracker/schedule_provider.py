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
# A stale estimate is not proof that the flight operated, especially when its
# inbound aircraft has only just arrived. Keep explicitly estimated departures
# longer than ordinary historic schedule rows so rebuilds can recover them.
ESTIMATED_DEPARTURE_PAST = timedelta(hours=6)


# Repeat lookups of the same tail within a few minutes (an impatient /info, a
# double /refresh) are served from memory, and every real request is spaced
# out — the schedule source is the one most likely to start blocking us.
#
# _LIST_SPACING is the single source of truth for how fast we may talk to
# FR24: it applies process-wide, to every caller and every code path. The
# harvest loop used to add its own 3-5s sleep on top, which only ever
# doubled the wait without changing the guarantee.
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


def clear_caches() -> int:
    """Forget every cached schedule: the memos and the 12h disk fallback.

    Returns the number of tails dropped from the disk cache. Aircraft facts
    (/info) and the airport database are untouched — neither is a schedule.
    """
    _LIST_MEMO.clear()
    _BOARD_MEMO.clear()
    path = _schedule_cache_path()
    dropped = 0
    if path.exists():
        try:
            dropped = len(json.loads(path.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
        path.unlink()
    log.info("Cleared schedule caches (%d cached tail(s) dropped)", dropped)
    return dropped


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


def _is_unresolved_estimated_departure(row: dict[str, Any]) -> bool:
    """Whether FR24 still explicitly calls this a pending departure."""
    estimated = ((row.get("time") or {}).get("estimated") or {}).get("departure")
    status = ((((row.get("status") or {}).get("generic") or {}).get("status")) or {})
    return bool(estimated and "estimated" in str(status.get("text", "")).lower())


def _within_harvest_window(
    row: dict[str, Any], event_type: EventType, when: datetime, now: datetime
) -> bool:
    """Keep ordinary rows for one hour, pending estimated departures for six."""
    if now - WINDOW_PAST <= when <= now + WINDOW_FUTURE:
        return True
    return (
        event_type == EventType.DEPARTURE
        and now - ESTIMATED_DEPARTURE_PAST <= when < now - WINDOW_PAST
        and _is_unresolved_estimated_departure(row)
    )


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
            if when is None or not _within_harvest_window(row, ev_type, when, now):
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


# ---------------------------------------------------------------------------
# Airport boards — "what is flying in/out of MY airports?"
#
# The inverse of the per-tail query. Cost scales with the number of airports
# (3) instead of the watchlist (77+), so it stays flat as the fleet grows.
# Entries carry the same shape as flight-list rows, except the queried
# airport's own code is omitted (it is implied), so we inject it back.
# ---------------------------------------------------------------------------

AIRPORT_BOARD_URL = "https://api.flightradar24.com/common/v1/airport.json"
BOARD_PAGE_SIZE = 100          # server-side maximum; larger values are rejected
BOARD_MAX_PAGES = 12           # stops runaway paging at very busy airports
_BOARD_MEMO = TTLCache(ttl_seconds=300)


def _fetch_board_page(code: str, mode: str, page: int) -> list[dict[str, Any]] | None:
    memo_key = (code.upper(), mode, page)
    cached = _BOARD_MEMO.get(memo_key)
    if cached is not MISS:
        return cached

    params = {
        "code": code,
        "plugin[]": "schedule",
        "plugin-setting[schedule][mode]": mode,
        "plugin-setting[schedule][timestamp]": int(time.time()),
        "page": page,
        "limit": BOARD_PAGE_SIZE,
    }
    for profile in IMPERSONATE_PROFILES:
        try:
            _LIST_SPACING.wait()
            resp = curl_requests.get(
                AIRPORT_BOARD_URL, params=params, impersonate=profile, timeout=40
            )
            if resp.status_code == 200:
                body = resp.json()
                airport = (((body.get("result") or {}).get("response") or {}).get("airport")) or {}
                schedule = ((airport.get("pluginData") or {}).get("schedule") or {})
                data = ((schedule.get(mode) or {}).get("data")) or []
                _BOARD_MEMO.set(memo_key, data)
                return data
        except Exception:  # noqa: BLE001
            pass
        time.sleep(1 + random.uniform(0, 2))
    log.warning("Airport board failed for %s %s page %s", code, mode, page)
    return None


def fetch_airport_board(
    code: str, mode: str, now: datetime | None = None
) -> list[dict[str, Any]] | None:
    """Board rows for one airport/direction covering the harvest window.

    Pages adaptively: a quiet airport needs one page, a hub several. Returns
    rows in flight-list shape, or None if the first page could not be read.
    """
    now = now or datetime.now(timezone.utc)
    horizon = now + WINDOW_FUTURE
    key = "arrival" if mode == "arrivals" else "departure"
    rows: list[dict[str, Any]] = []

    for page in range(1, BOARD_MAX_PAGES + 1):
        data = _fetch_board_page(code, mode, page)
        if data is None:
            return rows or None
        if not data:
            break
        latest: datetime | None = None
        for entry in data:
            flight = entry.get("flight") or entry
            _inject_queried_airport(flight, code, mode)
            rows.append(flight)
            when = _leg_time(flight, key)
            if when and (latest is None or when > latest):
                latest = when
        if latest is not None and latest >= horizon:
            break  # the window is covered
    return rows


def _inject_queried_airport(flight: dict[str, Any], code: str, mode: str) -> None:
    """Put back the airport code the board leaves implicit."""
    airport = flight.setdefault("airport", {})
    side = "destination" if mode == "arrivals" else "origin"
    entry = airport.get(side)
    if not isinstance(entry, dict):
        entry = {}
        airport[side] = entry
    if not entry.get("code"):
        entry["code"] = {"iata": code.upper(), "icao": ""}


def harvest_airport_boards(
    config: Config, now: datetime | None = None
) -> tuple[list[FlightEvent], bool]:
    """Sweep every configured airport's boards and keep watched aircraft.

    Returns (events, sources_ok). Reuses rows_to_events, so filtering and leg
    construction behave exactly as they do for the per-tail path.
    """
    now = now or datetime.now(timezone.utc)
    watchlist = {reg.upper(): meta for reg, meta in config.watchlist.items()}
    by_reg: dict[str, list[dict[str, Any]]] = {}
    sources_ok = True

    for code in config.target_airports:
        for mode in ("arrivals", "departures"):
            rows = fetch_airport_board(code, mode, now=now)
            if rows is None:
                sources_ok = False
                continue
            for row in rows:
                reg = ((row.get("aircraft") or {}).get("registration") or "").upper()
                if reg and reg in watchlist:
                    by_reg.setdefault(reg, []).append(row)

    events: list[FlightEvent] = []
    for reg, reg_rows in by_reg.items():
        livery = watchlist[reg].get("livery", "")
        events.extend(rows_to_events(reg, livery, reg_rows, config, now=now))
    return events, sources_ok


def row_is_cancelled(row: dict[str, Any]) -> bool:
    status = (((row.get("status") or {}).get("generic")) or {}).get("status") or {}
    return "cancel" in str(status.get("text", "")).lower()


@dataclass
class LegRefresh:
    new_time: datetime | None
    cancelled: bool = False
    swapped: bool = False  # the flight runs, but no longer with our aircraft
    delay_minutes: int | None = None  # source's own estimated-vs-scheduled figure
    completed: bool = False           # the source records this leg as already flown
    real_time: datetime | None = None  # the source's actual off/on time, if recorded


def _row_completed(row: dict[str, Any], key: str) -> tuple[bool, datetime | None]:
    """Whether the source's row says this side of the flight already happened.

    A real (actual) time is definitive. Otherwise the status text decides —
    and for departures a row marked live counts: the flight is in the air,
    so it has certainly left its origin.
    """
    times = row.get("time") or {}
    real_stamp = (times.get("real") or {}).get(key)
    real = datetime.fromtimestamp(real_stamp, tz=timezone.utc) if real_stamp else None
    status = (((row.get("status") or {}).get("generic")) or {}).get("status") or {}
    text = str(status.get("text", "")).lower()
    live = bool((row.get("status") or {}).get("live"))
    if key == "arrival":
        done = real is not None or text.startswith("landed")
    else:
        done = real is not None or live or text.startswith(("landed", "departed"))
    return done, real


def _leg_scheduled_and_estimated(
    row: dict[str, Any], key: str
) -> tuple[datetime | None, datetime | None]:
    """(published schedule, current estimate) for one side of a row.

    The delay must come from the source's own two figures. Comparing against
    the time *we* last stored measures drift since our previous check, which
    compounds across refreshes and cannot recover when an estimate improves.
    """
    times = row.get("time") or {}
    scheduled = (times.get("scheduled") or {}).get(key)
    estimated = ((times.get("estimated") or {}).get(key)
                 or (times.get("real") or {}).get(key))

    def convert(stamp):
        return datetime.fromtimestamp(stamp, tz=timezone.utc) if stamp else None

    return convert(scheduled), convert(estimated)


def _row_flight_number(row: dict[str, Any]) -> str:
    return ((((row.get("identification") or {}).get("number")) or {}).get("default") or "").strip().upper()


def _row_route(row: dict[str, Any]) -> tuple[str, str]:
    airport = row.get("airport") or {}
    origin = ((airport.get("origin") or {}).get("code")) or {}
    dest = ((airport.get("destination") or {}).get("code")) or {}
    return (
        (origin.get("iata") or origin.get("icao") or "").upper(),
        (dest.get("iata") or dest.get("icao") or "").upper(),
    )


def _best_leg_row(
    rows: list[dict[str, Any]], event: FlightEvent
) -> tuple[datetime, dict[str, Any]] | None:
    """The row that is genuinely *this* leg, or None.

    Identified by flight number first, then by the exact origin/destination
    pair. Matching on a single endpoint is not enough: an aircraft can have
    several departures from the same airport, and adopting a neighbouring
    flight's time invents a delay that never happened.
    """
    key = "arrival" if event.type == EventType.ARRIVAL else "departure"
    wanted_number = (event.flight_number or "").strip().upper()
    wanted_route = (event.route_origin, event.route_destination)

    by_number = [r for r in rows if wanted_number and _row_flight_number(r) == wanted_number]
    by_route = [r for r in rows if _row_route(r) == wanted_route]

    for pool in (by_number, by_route):
        best: tuple[float, datetime, dict[str, Any]] | None = None
        for row in pool:
            when = _leg_time(row, key)
            if when is None:
                continue
            drift = abs((when - event.scheduled_time).total_seconds())
            if drift < 6 * 3600 and (best is None or drift < best[0]):
                best = (drift, when, row)
        if best is not None:
            return (best[1], best[2])
    return None


def refresh_leg_time(reg: str, event: FlightEvent) -> LegRefresh:
    """Current best ETA/ETD (and cancellation flag) for one leg.

    The reconciliation primitive of the hourly sync (and of the poll-time
    re-checks while a leg is held in a source conflict).

    FR24 unassigns the registration from cancelled flights, so if the by-reg
    list no longer carries this leg, re-check by flight number — that's where
    a cancellation will show up.
    """
    key = "arrival" if event.type == EventType.ARRIVAL else "departure"
    best = _best_leg_row(fetch_flight_list(reg) or [], event)
    if best is not None:
        scheduled, estimated = _leg_scheduled_and_estimated(best[1], key)
        delay = None
        if scheduled and estimated:
            delay = round((estimated - scheduled).total_seconds() / 60)
        completed, real_time = _row_completed(best[1], key)
        return LegRefresh(
            best[0], cancelled=row_is_cancelled(best[1]), delay_minutes=delay,
            completed=completed, real_time=real_time,
        )

    # Our aircraft no longer lists this flight. Ask about the flight itself:
    # cancelled outright, or still running with a different tail (a swap)?
    if event.flight_number:
        by_flight = _best_leg_row(
            fetch_flight_list(event.flight_number, fetch_by="flight") or [], event
        )
        if by_flight is not None:
            if row_is_cancelled(by_flight[1]):
                return LegRefresh(by_flight[0], cancelled=True)
            return LegRefresh(None, swapped=True)
    return LegRefresh(None)


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
