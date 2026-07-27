"""Aircraft dossier: a cached profile plus live whereabouts (/info).

Static facts (type, hex, build year, owner) are cached in
data/aircraft_cache.json and *accumulate* — the build year, for instance, is
only published by the live ADS-B feed while an aircraft is transmitting, so
once we happen to see it we keep it forever. Position and schedules are
always fetched fresh.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from . import airports as airport_db
from .adsb import Telemetry, fetch_telemetry, resolve_callsign_route
from .config import Config, atomic_write_json, data_dir
from .digest import STATE_EMOJI, fmt_local
from .flights import FlightStore
from .geo import haversine_nm
from .resolver import resolve_aircraft
from .schedule_provider import current_flight, fetch_flight_list, upcoming_flights

log = logging.getLogger(__name__)

# Static facts change rarely (repaints, sales); re-check monthly.
CACHE_MAX_AGE = timedelta(days=30)
PLACEHOLDERS = {"", "Unknown airline", "Unknown type", None}


def _cache_path():
    return data_dir() / "aircraft_cache.json"


def load_cache() -> dict[str, dict[str, Any]]:
    path = _cache_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        log.warning("aircraft cache is corrupt — starting fresh")
        return {}


def _merge(existing: dict[str, Any], fresh: dict[str, Any]) -> dict[str, Any]:
    """Fold new facts in without ever losing a known value to a blank one."""
    merged = dict(existing)
    for key, value in fresh.items():
        if value in PLACEHOLDERS:
            continue
        merged[key] = value
    return merged


def get_profile(reg: str, refresh: bool = False) -> dict[str, Any]:
    """The cached profile for a registration, resolving/refreshing as needed."""
    reg = reg.upper()
    cache = load_cache()
    entry = cache.get(reg, {})

    stale = True
    if entry.get("updated_at") and not refresh:
        age = datetime.now(timezone.utc) - datetime.fromisoformat(entry["updated_at"])
        complete = entry.get("airline") not in PLACEHOLDERS and entry.get("model") not in PLACEHOLDERS
        stale = age > CACHE_MAX_AGE or not complete

    if stale:
        fresh = resolve_aircraft(reg, full=True)
        entry = _merge(entry, fresh)
        entry["registration"] = reg
        entry.setdefault("first_seen", datetime.now(timezone.utc).isoformat())
        entry["updated_at"] = datetime.now(timezone.utc).isoformat()
        cache[reg] = entry
        atomic_write_json(_cache_path(), cache)

    return entry


def record_profile(reg: str, info: dict[str, Any]) -> None:
    """Fold metadata resolved elsewhere (e.g. /add) into the cache."""
    reg = reg.upper()
    cache = load_cache()
    entry = _merge(cache.get(reg, {}), info)
    entry["registration"] = reg
    entry.setdefault("first_seen", datetime.now(timezone.utc).isoformat())
    entry["updated_at"] = datetime.now(timezone.utc).isoformat()
    cache[reg] = entry
    atomic_write_json(_cache_path(), cache)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

def _describe_position(
    telemetry: Telemetry, config: Config, live_flight: dict[str, str] | None = None
) -> list[str]:
    """Where the aircraft is right now, in human terms."""
    lines: list[str] = []
    nearest = airport_db.nearest(telemetry.lat, telemetry.lon)
    near_name = ""
    if nearest:
        distance = haversine_nm(telemetry.lat, telemetry.lon, nearest.lat, nearest.lon)
        code = nearest.iata or nearest.icao
        near_name = f"{code}" + (f" ({distance:.0f} NM away)" if distance > 3 else "")

    if telemetry.on_ground:
        lines.append(f"• 🛬 On the ground{f' at {near_name}' if near_name else ''}")
    else:
        speed = f" · {telemetry.gs_kts:.0f} kts" if telemetry.gs_kts else ""
        trend = ""
        if telemetry.baro_rate is not None:
            if telemetry.baro_rate < -200:
                trend = " · descending"
            elif telemetry.baro_rate > 200:
                trend = " · climbing"
        lines.append(f"• ✈️ Airborne at {telemetry.alt_ft:,} ft{speed}{trend}")
        if near_name:
            lines.append(f"• Nearest airport: {near_name}")

    # Route: trust FR24's live flight first; the callsign registry can be stale.
    if live_flight:
        number = live_flight["flight_number"] or telemetry.callsign
        lines.append(f"• Flying {number}: {live_flight['origin']} ➔ {live_flight['destination']}")
    elif telemetry.callsign:
        route = resolve_callsign_route(telemetry.callsign)
        if route:
            origin, destination, _ = route
            lines.append(
                f"• Callsign {telemetry.callsign} — usual route {origin} ➔ {destination}"
            )
        else:
            lines.append(f"• Callsign: {telemetry.callsign}")

    # Distance to the user's own airports is the thing they actually care about.
    for code, info in sorted(config.target_airports.items()):
        if info.get("lat") is None:
            continue
        distance = haversine_nm(telemetry.lat, telemetry.lon, info["lat"], info["lon"])
        if distance <= 250:
            lines.append(f"• {distance:.0f} NM from your {code}")
    return lines


def build_report(
    reg: str, config: Config, store: FlightStore, refresh: bool = False
) -> tuple[str, str]:
    """(message_html, thumbnail_url) describing an aircraft and its whereabouts."""
    reg = reg.upper()
    profile = get_profile(reg, refresh=refresh)
    now = datetime.now(timezone.utc)

    airline = profile.get("airline") or "Unknown airline"
    livery = profile.get("livery") or ""
    header = f"🔎 <b>{reg}</b> — {airline}"
    lines = [header]
    if livery:
        lines.append(f"<i>“{livery}”</i>")
    lines.append("")

    # --- the aircraft itself -------------------------------------------------
    lines.append("<b>📋 Aircraft</b>")
    model = profile.get("model") or "Unknown type"
    type_code = profile.get("type_code")
    lines.append(f"• Type: {model}" + (f" ({type_code})" if type_code else ""))
    year = profile.get("year")
    if year:
        age = max(now.year - int(year), 0)
        lines.append(f"• Built: {year} ({age} year{'s' if age != 1 else ''} old)")
    if profile.get("hex"):
        lines.append(f"• Mode S / ICAO hex: <code>{profile['hex']}</code>")
    if profile.get("owner_country"):
        lines.append(f"• Registered in: {profile['owner_country']}")
    if profile.get("photo_link"):
        lines.append(f"• <a href=\"{profile['photo_link']}\">Photo on Planespotters</a>")

    # --- live position -------------------------------------------------------
    rows = fetch_flight_list(reg)  # also used for the itinerary below
    lines.append("")
    lines.append("<b>📡 Right now</b>")
    telemetry = fetch_telemetry(reg)
    if telemetry is None:
        lines.append("• No ADS-B signal — parked, or outside receiver coverage")
    else:
        lines.extend(_describe_position(telemetry, config, current_flight(rows or [])))

    # --- what we are already tracking today ----------------------------------
    tracked = sorted(
        (ev for ev in store.events.values() if ev.tail == reg),
        key=lambda ev: ev.scheduled_time,
    )
    if tracked:
        lines.append("")
        lines.append("<b>🎯 Tracked today</b>")
        for event in tracked:
            emoji = STATE_EMOJI.get(event.status, "•")
            lines.append(
                f"{emoji} {event.type.value.title()} @ {event.target_airport} — "
                f"{event.route_origin}➔{event.route_destination} "
                f"{fmt_local(event.scheduled_time)}"
            )

    # --- upcoming schedule ---------------------------------------------------
    lines.append("")
    if rows is None:
        lines.append("<i>Schedule source unavailable right now.</i>")
    else:
        flights = upcoming_flights(rows, now=now)
        if not flights:
            lines.append("<b>📅 Upcoming</b>\n• Nothing scheduled in the next 24h")
        else:
            watched = config.airport_codes()
            lines.append("<b>📅 Upcoming</b>")
            for flight in flights:
                star = "⭐ " if watched & {flight["origin"], flight["destination"]} else ""
                when = flight["departure"] or flight["arrival"]
                label = "cancelled" if flight["cancelled"] else fmt_local(when)
                number = f" {flight['flight_number']}" if flight["flight_number"] else ""
                lines.append(
                    f"{star}• {flight['origin']}➔{flight['destination']}{number} — {label}"
                )
            if any(watched & {f["origin"], f["destination"]} for f in flights):
                lines.append("<i>⭐ touches one of your airports</i>")

    # --- watchlist status ----------------------------------------------------
    lines.append("")
    if reg in config.watchlist:
        lines.append("👀 <i>On your watchlist</i>")
    else:
        lines.append(f"<i>Not watched — add it with /add {reg}</i>")

    return "\n".join(lines), profile.get("thumbnail") or ""
