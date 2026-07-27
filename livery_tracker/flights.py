"""Flight event records and flights_today.json persistence."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from .config import atomic_write_json, flights_file


class EventType(str, Enum):
    ARRIVAL = "ARRIVAL"
    DEPARTURE = "DEPARTURE"


class EventState(str, Enum):
    WAITING_2H = "WAITING_2H"      # digest sent, waiting for T-2h schedule refresh
    WAITING_LIVE = "WAITING_LIVE"  # refreshed, waiting for live-tracking window
    LIVE = "LIVE"                  # polling ADS-B every 120s
    LANDED = "LANDED"
    DEPARTED = "DEPARTED"
    CANCELLED = "CANCELLED"        # source reported the flight cancelled
    LOST = "LOST"                  # no ADS-B data past deadline

    @property
    def terminal(self) -> bool:
        return self in (
            EventState.LANDED, EventState.DEPARTED, EventState.CANCELLED, EventState.LOST
        )


@dataclass
class FlightEvent:
    """One tracked leg (arrival or departure) tied to one Telegram message."""

    id: str
    tail: str
    livery: str
    type: EventType
    target_airport: str            # IATA key into config target_airports
    scheduled_time: datetime       # tz-aware
    route_origin: str = "???"
    route_destination: str = "???"
    flight_number: str = ""
    status: EventState = EventState.WAITING_2H
    status_note: str = ""
    last_telemetry: dict[str, Any] = field(
        default_factory=lambda: {"lat": None, "lon": None, "alt": None, "gs": None, "dist_nm": None}
    )

    @staticmethod
    def make_id(tail: str, ev_type: EventType, when: datetime, airport: str) -> str:
        kind = "ARR" if ev_type == EventType.ARRIVAL else "DEP"
        return f"{tail}-{kind}-{when.strftime('%Y%m%d%H%M')}-{airport}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "tail": self.tail,
            "livery": self.livery,
            "type": self.type.value,
            "target_airport": self.target_airport,
            "scheduled_time": self.scheduled_time.isoformat(),
            "route_origin": self.route_origin,
            "route_destination": self.route_destination,
            "flight_number": self.flight_number,
            "status": self.status.value,
            "status_note": self.status_note,
            "last_telemetry": self.last_telemetry,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "FlightEvent":
        when = datetime.fromisoformat(raw["scheduled_time"])
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return cls(
            id=raw["id"],
            tail=raw["tail"],
            livery=raw.get("livery", ""),
            type=EventType(raw["type"]),
            target_airport=raw["target_airport"],
            scheduled_time=when,
            route_origin=raw.get("route_origin", "???"),
            route_destination=raw.get("route_destination", "???"),
            flight_number=raw.get("flight_number", ""),
            status=EventState(raw.get("status", "WAITING_2H")),
            status_note=raw.get("status_note", ""),
            last_telemetry=raw.get(
                "last_telemetry",
                {"lat": None, "lon": None, "alt": None, "gs": None, "dist_nm": None},
            ),
        )


class FlightStore:
    """Owns the flights_today.json list; every mutation is persisted atomically."""

    def __init__(self) -> None:
        self.events: dict[str, FlightEvent] = {}
        self.reload()

    def reload(self) -> None:
        path = flights_file()
        self.events = {}
        if path.exists():
            for raw in json.loads(path.read_text(encoding="utf-8")):
                ev = FlightEvent.from_dict(raw)
                self.events[ev.id] = ev

    def save(self) -> None:
        atomic_write_json(flights_file(), [ev.to_dict() for ev in self.events.values()])

    def upsert(self, event: FlightEvent) -> None:
        self.events[event.id] = event
        self.save()

    def get(self, event_id: str) -> FlightEvent | None:
        return self.events.get(event_id)

    def active(self) -> list[FlightEvent]:
        return [ev for ev in self.events.values() if not ev.status.terminal]

    def remove_where(self, predicate) -> list["FlightEvent"]:
        """Delete every event matching predicate; returns what was removed."""
        removed = [ev for ev in self.events.values() if predicate(ev)]
        for ev in removed:
            del self.events[ev.id]
        if removed:
            self.save()
        return removed

    def clear(self) -> None:
        self.events = {}
        self.save()
