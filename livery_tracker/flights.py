"""Flight event records, flights_today.json persistence, and the journal.

The journal (data/journal.jsonl) records every state transition, schedule
change, creation and removal of a leg — with the code path that caused it
and the evidence it acted on. It is the debugging record: when a digest
line looks wrong hours later, `grep <tail> data/journal*.jsonl` shows the
exact decision and what the sources were saying at that moment. Terminal
outcomes additionally land in history.jsonl with their final telemetry.
"""

from __future__ import annotations

import contextvars
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any

from .config import atomic_write_json, data_dir, flights_file

log = logging.getLogger(__name__)


class EventType(str, Enum):
    ARRIVAL = "ARRIVAL"
    DEPARTURE = "DEPARTURE"


class EventState(str, Enum):
    WAITING_2H = "WAITING_2H"      # pending; mirrored against the source hourly
    WAITING_LIVE = "WAITING_LIVE"  # pending (same handling), waiting for T-1h live start
    TURNAROUND_DELAY = "TURNAROUND_DELAY"  # inbound arrived after this leg's source estimate
    LIVE = "LIVE"                  # polling ADS-B every 120s
    LANDED = "LANDED"
    DEPARTED = "DEPARTED"
    DIVERTED = "DIVERTED"          # confirmed on the ground far from the target
    CANCELLED = "CANCELLED"        # source reported the flight cancelled
    SWAPPED = "SWAPPED"            # flight still runs, but with a different aircraft
    LOST = "LOST"                  # no ADS-B data past deadline

    @property
    def terminal(self) -> bool:
        return self in (
            EventState.LANDED,
            EventState.DEPARTED,
            EventState.DIVERTED,
            EventState.CANCELLED,
            EventState.SWAPPED,
            EventState.LOST,
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


# ---------------------------------------------------------------------------
# Journal: who changed a leg, and why
#
# Jobs and commands label themselves with set_journal_context() — a
# ContextVar, so concurrently running asyncio tasks can't mislabel each
# other's writes. The store diffs every upsert/removal against a snapshot
# and appends one row per real change; routine telemetry refreshes and a
# restart's rehydration produce nothing.
# ---------------------------------------------------------------------------

_JOURNAL_TRIGGER: contextvars.ContextVar[str] = contextvars.ContextVar(
    "journal_trigger", default=""
)
_JOURNAL_EVIDENCE: contextvars.ContextVar[dict | None] = contextvars.ContextVar(
    "journal_evidence", default=None
)

JOURNAL_KEEP_DAYS = 7


def set_journal_context(trigger: str, evidence: dict[str, Any] | None = None) -> None:
    """Label subsequent store changes in this task with their cause.

    Call at the top of a job for the coarse tag ("poll", "harvest"), and
    again at a decision branch with the inputs that drove it. The label
    lives in the current asyncio task's context, so it never bleeds into
    other jobs running concurrently.
    """
    _JOURNAL_TRIGGER.set(trigger)
    _JOURNAL_EVIDENCE.set(evidence or None)


def journal_file():
    return data_dir() / "journal.jsonl"


def append_journal(entry: dict[str, Any]) -> None:
    """Best-effort append; a journaling failure must never break tracking."""
    try:
        record = {"ts": datetime.now(timezone.utc).isoformat(), **entry}
        with journal_file().open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # noqa: BLE001
        log.warning("Journal write failed: %s", exc)


def rotate_journal(now: datetime | None = None) -> None:
    """Archive the current journal under yesterday's date and prune old files.

    Runs at the daily harvest, mirroring the digest rollover. Archives are
    named journal-YYYYMMDD.jsonl and kept for JOURNAL_KEEP_DAYS days.
    """
    now = now or datetime.now(timezone.utc)
    path = journal_file()
    try:
        if path.exists() and path.stat().st_size > 0:
            stamp = (now - timedelta(days=1)).strftime("%Y%m%d")
            target = data_dir() / f"journal-{stamp}.jsonl"
            with target.open("a", encoding="utf-8") as fh:
                fh.write(path.read_text(encoding="utf-8"))
            path.unlink()
        cutoff = (now - timedelta(days=JOURNAL_KEEP_DAYS)).strftime("%Y%m%d")
        for old in data_dir().glob("journal-*.jsonl"):
            datestr = old.stem.removeprefix("journal-")
            if datestr.isdigit() and datestr < cutoff:
                old.unlink()
    except Exception as exc:  # noqa: BLE001
        log.warning("Journal rotation failed: %s", exc)


def append_history(event: FlightEvent) -> None:
    """Log a concluded leg to data/history.jsonl for accuracy auditing.

    Every terminal decision (landed, presumed landed, diverted, lost, ...)
    lands here with its final telemetry, so inference thresholds can be
    checked against what actually happened and tuned over time.
    """
    entry = {"finalized_at": datetime.now(timezone.utc).isoformat(), **event.to_dict()}
    with (data_dir() / "history.jsonl").open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


class FlightStore:
    """Owns the flights_today.json list; every mutation is persisted atomically.

    Every mutation also flows through the journal: upsert diffs the leg
    against a snapshot of (status, time, note) and records real changes with
    the current journal context. Reloading seeds snapshots silently, so a
    restart's rehydration never journals phantom changes.
    """

    def __init__(self) -> None:
        self.events: dict[str, FlightEvent] = {}
        self._snapshots: dict[str, tuple[str, str, str]] = {}
        self.reload()

    @staticmethod
    def _snapshot(event: FlightEvent) -> tuple[str, str, str]:
        return (event.status.value, event.scheduled_time.isoformat(), event.status_note)

    def reload(self) -> None:
        path = flights_file()
        self.events = {}
        if path.exists():
            for raw in json.loads(path.read_text(encoding="utf-8")):
                ev = FlightEvent.from_dict(raw)
                self.events[ev.id] = ev
        self._snapshots = {ev.id: self._snapshot(ev) for ev in self.events.values()}

    def save(self) -> None:
        atomic_write_json(flights_file(), [ev.to_dict() for ev in self.events.values()])

    def upsert(self, event: FlightEvent) -> None:
        old = self._snapshots.get(event.id)
        new = self._snapshot(event)
        if old is None:
            self._journal(event, {"created": True, "status": event.status.value})
        elif old != new:
            change: dict[str, Any] = {}
            if old[0] != new[0]:
                change["status"] = [old[0], new[0]]
            if old[1] != new[1]:
                change["time"] = [old[1], new[1]]
            if old[2] != new[2]:
                change["note"] = [old[2], new[2]]
            # A late-running LIVE leg recomputes its lateness note every
            # poll; journaling each tick would bury the real transitions.
            if not (set(change) == {"note"} and event.status == EventState.LIVE):
                self._journal(event, change)
        self._snapshots[event.id] = new
        self.events[event.id] = event
        self.save()

    def _journal(self, event: FlightEvent, change: dict[str, Any]) -> None:
        entry: dict[str, Any] = {
            "id": event.id,
            "tail": event.tail,
            "flight": event.flight_number,
            "change": change,
        }
        trigger = _JOURNAL_TRIGGER.get()
        if trigger:
            entry["trigger"] = trigger
        evidence = _JOURNAL_EVIDENCE.get()
        if evidence:
            entry["evidence"] = evidence
        append_journal(entry)

    def get(self, event_id: str) -> FlightEvent | None:
        return self.events.get(event_id)

    def active(self) -> list[FlightEvent]:
        return [ev for ev in self.events.values() if not ev.status.terminal]

    def remove_where(self, predicate) -> list["FlightEvent"]:
        """Delete every event matching predicate; returns what was removed."""
        removed = [ev for ev in self.events.values() if predicate(ev)]
        for ev in removed:
            del self.events[ev.id]
            self._snapshots.pop(ev.id, None)
            self._journal(ev, {"removed": True, "status": ev.status.value})
        if removed:
            self.save()
        return removed

    def clear(self) -> None:
        for ev in self.events.values():
            self._journal(ev, {"removed": True, "status": ev.status.value})
        self.events = {}
        self._snapshots = {}
        self.save()
