"""Zero-touch aircraft metadata resolution when a tail is added.

Photo/thumbnail comes from the Planespotters public API; airline, model and
livery name come from FR24's flight-list metadata (Planespotters' pub API only
exposes photo data). adsb.fi's aircraft description is a last-resort fallback.
"""

from __future__ import annotations

import logging
from typing import Any

from .schedule_provider import extract_aircraft_meta, fetch_flight_list
from .web import get_json

log = logging.getLogger(__name__)

PLANESPOTTERS_URL = "https://api.planespotters.net/pub/photos/reg/{reg}"
ADSBFI_URL = "https://opendata.adsb.fi/api/v2/registration/{reg}"


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

    if info["airline"] == "Unknown airline" or info["model"] == "Unknown type":
        body = get_json(ADSBFI_URL.format(reg=reg))
        for ac in (body or {}).get("ac") or []:
            if info["model"] == "Unknown type" and ac.get("desc"):
                info["model"] = str(ac["desc"]).title()
            if info["airline"] == "Unknown airline" and ac.get("ownOp"):
                info["airline"] = str(ac["ownOp"]).title()
            break

    return info
