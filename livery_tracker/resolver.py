"""Zero-touch aircraft metadata resolution when a tail is added.

Sources, in order:
  * Planespotters  — photo/thumbnail (its pub API exposes photo data only)
  * FR24           — airline, model, and the livery name, which FR24 encodes
                     in the airline field, e.g. "Alaska Airlines (Retro Livery)"
  * adsbdb         — static registration registry: airline + type even for an
                     aircraft that is parked and hasn't flown recently
  * adsb.fi        — live transponder description, last resort

The adsbdb step matters because every other metadata source depends on the
aircraft having flown recently; without it, a parked jet resolves as "Unknown".
"""

from __future__ import annotations

import logging
from typing import Any

from .schedule_provider import extract_aircraft_meta, fetch_flight_list
from .web import get_json

log = logging.getLogger(__name__)

PLANESPOTTERS_URL = "https://api.planespotters.net/pub/photos/reg/{reg}"
ADSBDB_AIRCRAFT_URL = "https://api.adsbdb.com/v0/aircraft/{reg}"
ADSBFI_URL = "https://opendata.adsb.fi/api/v2/registration/{reg}"


def _adsbdb_aircraft(reg: str) -> dict[str, str]:
    """Airline/model from the adsbdb registration registry (static, no flight needed)."""
    body = get_json(ADSBDB_AIRCRAFT_URL.format(reg=reg))
    response = (body or {}).get("response")
    if not isinstance(response, dict):
        return {}  # adsbdb answers with a bare string for unknown registrations
    aircraft = response.get("aircraft")
    if not isinstance(aircraft, dict):
        aircraft = response
    airline = (aircraft.get("registered_owner") or "").strip()
    model = " ".join(
        part for part in (
            (aircraft.get("manufacturer") or "").strip().title(),
            (aircraft.get("type") or "").strip(),
        ) if part
    )
    return {"airline": airline, "model": model}


def resolve_aircraft(reg: str) -> dict[str, Any]:
    """Best-effort airline/model/livery/photo lookup for a registration."""
    info: dict[str, Any] = {
        "airline": "Unknown airline",
        "model": "Unknown type",
        "livery": "",
        "thumbnail": "",
        "photo_link": "",
    }

    body = get_json(PLANESPOTTERS_URL.format(reg=reg))
    photos = (body or {}).get("photos") or []
    if photos:
        photo = photos[0]
        info["thumbnail"] = ((photo.get("thumbnail_large") or {}).get("src")) or ""
        info["photo_link"] = photo.get("link") or ""

    rows = fetch_flight_list(reg) or []
    meta = extract_aircraft_meta(rows)
    if meta["airline"]:
        info["airline"] = meta["airline"]
    if meta["model"]:
        info["model"] = meta["model"]
    if meta["livery"]:
        info["livery"] = meta["livery"]

    # Static registry — fills in aircraft that are parked or rarely tracked.
    if info["airline"] == "Unknown airline" or info["model"] == "Unknown type":
        registry = _adsbdb_aircraft(reg)
        if info["airline"] == "Unknown airline" and registry.get("airline"):
            info["airline"] = registry["airline"]
        if info["model"] == "Unknown type" and registry.get("model"):
            info["model"] = registry["model"]

    if info["airline"] == "Unknown airline" or info["model"] == "Unknown type":
        body = get_json(ADSBFI_URL.format(reg=reg))
        for ac in (body or {}).get("ac") or []:
            if info["model"] == "Unknown type" and ac.get("desc"):
                info["model"] = str(ac["desc"]).title()
            if info["airline"] == "Unknown airline" and ac.get("ownOp"):
                info["airline"] = str(ac["ownOp"]).title()
            break

    return info
