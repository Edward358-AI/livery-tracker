"""Shared HTTP bits for the free/public APIs."""

from __future__ import annotations

import logging
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Planespotters (and general courtesy): server-side clients must identify
# themselves with a contact URL, e.g. "MyFlightTracker/1.2 (+https://example.com)".
USER_AGENT = "LiveryTracker/1.0 (+https://github.com/Edward358-AI/livery-tracker)"

DEFAULT_TIMEOUT = 25.0


def get_json(url: str, *, timeout: float = DEFAULT_TIMEOUT) -> Any | None:
    """GET a JSON document with our identifying UA; None on any failure."""
    try:
        resp = httpx.get(
            url,
            headers={"User-Agent": USER_AGENT},
            timeout=timeout,
            follow_redirects=True,
        )
        if resp.status_code != 200:
            log.warning("GET %s -> HTTP %s", url, resp.status_code)
            return None
        return resp.json()
    except Exception as exc:  # noqa: BLE001 - network layer, degrade gracefully
        log.warning("GET %s failed: %s", url, exc)
        return None
