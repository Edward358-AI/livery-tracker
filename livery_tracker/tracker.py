"""Event scheduler and per-leg state machine (mirror-and-verify).

Three layers, each with a single owner of truth:

  Pending legs   — a mirror of the schedule source. An hourly sync re-reads
                   every tail that still has pending legs and adopts the
                   source's times verbatim, cancels what it cancels, and
                   withdraws legs it no longer lists. A failed fetch marks
                   legs "unverified" — it never drops them.
  Live legs      — ADS-B owns the present. Polling starts at T-1h; landings,
                   departures, diversions and go-arounds are concluded from
                   direct observation, never from the schedule.
  Conclusions    — verified against the source ~25 minutes later. Direct
                   observations stand even when the source disagrees; weak
                   inferences (LOST, signal-loss guesses) defer to it.

The one standing exception is the turnaround/position guard: when the
source's own data is physically impossible (an ETD before the same tail's
recorded landing, or the aircraft visibly parked at another airport), the
leg is held in TURNAROUND_DELAY rather than mirrored blindly.

Lifecycle per leg:
  WAITING_2H / WAITING_LIVE --(T-1h)--> LIVE (ADS-B poll every 120s)
  LIVE --> LANDED / DEPARTED / DIVERTED / LOST (terminal, then verified)

Every state change re-renders the daily digest message. All timers live in
python-telegram-bot's JobQueue, so a restart only needs `rehydrate()` to
re-register jobs from flights_today.json.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable

from telegram.ext import Application, ContextTypes

from . import adsb
from . import airports as airport_db
from . import schedule_provider
from .adsb import Telemetry, fetch_telemetry, resolve_callsign_route
from .config import Config
from .digest import DigestManager, fmt_local
from .flights import (
    EventState,
    EventType,
    FlightEvent,
    FlightStore,
    append_history,
    rotate_journal,
    set_journal_context,
)
from .geo import haversine_nm
from .resolver import resolve_aircraft

log = logging.getLogger(__name__)

LIVE_LEAD = timedelta(hours=1)     # ADS-B polling starts at T-1h for both leg types
POLL_INTERVAL = 120  # seconds
LOST_TIMEOUT = timedelta(minutes=30)   # never-seen past this: start asking the source why
STALE_AFTER = timedelta(hours=12)  # events this far past schedule are purged at harvest

# Hourly mirror sync: pending legs are reconciled against the source; legs
# being tracked live belong to ADS-B and are never touched by the sync.
# Legs entering their final stretch get a faster lane: a hot pass every 15
# minutes covering only tails with something due within two hours, so a
# late cancellation or swap can't hide in the hourly gap.
SYNC_INTERVAL = 3600               # seconds
HOT_SYNC_INTERVAL = 900            # seconds
HOT_WINDOW = timedelta(hours=2)    # a leg due within this joins the hot pass
DISCOVERY_INTERVAL = 3 * 3600      # boards-only sweep for new legs
UNVERIFIED_NOTE = "unverified — source unreachable"
WITHDRAWN_NOTE = "no longer scheduled for this aircraft"

# Post-conclusion verification: compare our live-tracking verdict with the
# source once its status page has had time to catch up.
VERIFY_DELAY = timedelta(minutes=25)

LANDED_MAX_ALT_FT = 500
LANDED_MAX_DIST_NM = 5.0
DEPARTED_MIN_ALT_FT = 10_000
DEPARTED_MIN_DIST_NM = 15.0

# Signal-loss inference: ADS-B coverage is patchy near the ground, so aircraft
# routinely vanish on short final. If we last saw the plane in a telling spot
# and it stays dark this long, conclude the leg rather than polling forever.
SILENT_GRACE = timedelta(minutes=6)          # ~3 missed polls
APPROACH_MAX_ALT_FT = 4_000
APPROACH_MAX_DIST_NM = 15.0
LIVE_MAX_OVERRUN = timedelta(hours=3)        # hard cap: give up this long past schedule

# Diversion: only conclude on CONFIRMED ground contact far from the target,
# seen on consecutive polls (debounce against bad/one-off samples).
DIVERT_MIN_DIST_NM = 30.0
DIVERT_CONFIRM_POLLS = 2

# Schedule-less ADS-B watch mode (activates only when schedule sources fail).
WATCH_INTERVAL = 900  # seconds between watch sweeps
# Community route DBs (adsbdb) can be stale — e.g. a callsign's origin moving
# from DFW to ONT. Origins are therefore only trusted when we actually observe
# the aircraft near that airport; destinations self-verify via live tracking.
WATCH_DEP_MAX_DIST_NM = 60.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _journal_evidence(
    telemetry: Telemetry | None = None,
    refresh: schedule_provider.LegRefresh | None = None,
    **extra: object,
) -> dict:
    """The inputs a decision acted on, in journal-serializable form."""
    evidence: dict[str, object] = dict(extra)
    if telemetry is not None:
        evidence["telemetry"] = {
            "callsign": telemetry.callsign,
            "alt_ft": telemetry.alt_ft,
            "on_ground": telemetry.on_ground,
            "gs_kts": telemetry.gs_kts,
            "lat": telemetry.lat,
            "lon": telemetry.lon,
            "source": telemetry.source,
        }
    if refresh is not None:
        evidence["refresh"] = {
            "new_time": refresh.new_time.isoformat() if refresh.new_time else None,
            "cancelled": refresh.cancelled,
            "swapped": refresh.swapped,
            "completed": refresh.completed,
            "delay_minutes": refresh.delay_minutes,
            "real_time": refresh.real_time.isoformat() if refresh.real_time else None,
            "matched_number": refresh.matched_number,
            "rerouted": refresh.rerouted,
        }
    return evidence


def _digest(application: Application) -> DigestManager:
    return application.bot_data["digest"]


def _cancel_jobs(application: Application, event_id: str) -> None:
    for suffix in ("live_start", "poll", "verify"):
        for job in application.job_queue.get_jobs_by_name(f"{event_id}:{suffix}"):
            job.schedule_removal()


TURNAROUND_CONFLICT_NOTE = "Awaiting turnaround / source conflict"
TURNAROUND_CONFLICT_MAX_LAG = timedelta(hours=12)


def _recorded_landing_time(event: FlightEvent) -> datetime | None:
    """The ADS-B-confirmed landing time stored on a terminal arrival."""
    if event.status != EventState.LANDED:
        return None
    seen_at = event.last_telemetry.get("seen_at")
    if not seen_at:
        return None
    try:
        when = datetime.fromisoformat(seen_at)
    except (TypeError, ValueError):
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _arrival_can_precede_outbound(outbound: FlightEvent, inbound_time: datetime) -> bool:
    """Whether an inbound could plausibly be the outbound's delayed rotation."""
    delay = inbound_time - outbound.scheduled_time
    return timedelta() < delay <= TURNAROUND_CONFLICT_MAX_LAG


# Distinguishing a delayed prerequisite from the departure's own return leg:
# on an out-and-back rotation (SFO->PHX then PHX->SFO) the return always
# arrives "after the ETD" — that is the schedule working, not failing. The
# tell is the gap: a dependent return needs at least a round trip's worth of
# time; a prerequisite that has slipped past the ETD shows up much closer
# (N475UA's genuine conflict was a 53-minute gap on a ~4h round trip).
RETURN_MIN_ROUND_TRIP = timedelta(minutes=90)   # cheap floor, avoids lookups
RETURN_CRUISE_KTS = 450.0
RETURN_MIN_TURN = timedelta(minutes=30)


def _is_plausible_dependent_return(
    outbound: FlightEvent, inbound: FlightEvent, inbound_time: datetime
) -> bool:
    """Whether the inbound can be the outbound's own return leg."""
    if inbound.route_origin != outbound.route_destination:
        return False
    gap = inbound_time - outbound.scheduled_time
    if gap < RETURN_MIN_ROUND_TRIP:
        return False
    near = airport_db.lookup(outbound.route_origin)
    far = airport_db.lookup(outbound.route_destination)
    if near is None or far is None:
        return False   # can't prove it — stay conservative and hold
    dist = haversine_nm(near.lat, near.lon, far.lat, far.lon)
    round_trip = timedelta(hours=2 * dist / RETURN_CRUISE_KTS) + RETURN_MIN_TURN
    return gap >= round_trip


def _has_turnaround_conflict(store: FlightStore, event: FlightEvent) -> bool:
    """Whether this outbound conflicts with the tail's inbound rotation.

    A tracker-confirmed landing is definitive. Before touchdown, an active
    inbound whose ETA is already after the outbound ETD is enough to flag an
    impossible source sequence, but never to manufacture a replacement time.
    The tail's own dependent return leg is exempt — it always arrives after
    the ETD, by design. May hit the local airport index; call via a thread
    from async code.
    """
    if event.type != EventType.DEPARTURE:
        return False
    for inbound in store.events.values():
        if (
            inbound.tail != event.tail
            or inbound.type != EventType.ARRIVAL
            or inbound.target_airport != event.target_airport
        ):
            continue
        landed_at = _recorded_landing_time(inbound)
        if (
            landed_at is not None
            and _arrival_can_precede_outbound(event, landed_at)
            and not _is_plausible_dependent_return(event, inbound, landed_at)
        ):
            return True
        if (
            inbound.status in (EventState.WAITING_2H, EventState.WAITING_LIVE, EventState.LIVE)
            and _arrival_can_precede_outbound(event, inbound.scheduled_time)
            and not _is_plausible_dependent_return(event, inbound, inbound.scheduled_time)
        ):
            return True
    return False


def _enter_turnaround_delay(event: FlightEvent) -> None:
    """Hold an impossible outbound estimate without inventing a replacement."""
    event.status = EventState.TURNAROUND_DELAY
    event.status_note = TURNAROUND_CONFLICT_NOTE


# Position sanity: a departure can't happen if the aircraft is visibly parked
# at some other airport. Only positive evidence counts — a dark transponder
# proves nothing (the aircraft may simply be powered down at its gate).
POSITION_CONFLICT_MIN_DIST_NM = 50.0
POSITION_CHECK_LEAD = timedelta(hours=2)   # how close to ETD the sync checks position
GATE_RELEASE_MAX_DIST_NM = 10.0  # on the ground this close to its own airport = it's here


def _position_conflict_note(
    event: FlightEvent, telemetry: Telemetry | None, airport: dict
) -> str:
    """A note when live ADS-B contradicts this departure, else ""."""
    if event.type != EventType.DEPARTURE or telemetry is None:
        return ""
    if not telemetry.on_ground:
        return ""   # airborne aircraft are judged by the callsign guard instead
    if airport.get("lat") is None:
        return ""
    dist = haversine_nm(telemetry.lat, telemetry.lon, airport["lat"], airport["lon"])
    if dist <= POSITION_CONFLICT_MIN_DIST_NM:
        return ""
    where = airport_db.nearest(telemetry.lat, telemetry.lon)
    place = (where.iata or where.icao) if where else f"{dist:.0f} NM away"
    return f"aircraft seen on the ground near {place} — awaiting schedule update"


def _foreign_callsign_note(event: FlightEvent, telemetry: Telemetry | None) -> str:
    """A note while the aircraft is positively operating a different flight.

    Wearing the previous rotation's callsign — parked at the gate after a
    late inbound, or still flying it — is evidence of "not yet", never of
    "never": only the source may declare the leg gone.
    """
    if telemetry is None:
        return ""
    seen = _designator_digits(telemetry.callsign, _ICAO_CALLSIGN_RE)
    expected = _designator_digits(event.flight_number, _IATA_FLIGHT_RE)
    if seen and expected and seen != expected:
        return f"aircraft still operating {telemetry.callsign.strip()} — awaiting rotation"
    return ""


def _conclude_from_source(
    event: FlightEvent, refresh: schedule_provider.LegRefresh
) -> None:
    """Adopt the source's record that this leg has already flown.

    Used when we did not observe the movement ourselves — the aircraft was
    dark, or already wearing its next callsign by the time we looked.
    """
    event.status = (
        EventState.LANDED if event.type == EventType.ARRIVAL else EventState.DEPARTED
    )
    stamp = refresh.real_time or refresh.new_time
    event.status_note = f"~{fmt_local(stamp)} (per source)" if stamp else "per source"


NO_SHOW_GRACE = timedelta(minutes=10)
ARRIVAL_LATE_MIN_DIST_NM = 40.0


def _no_show_note(event: FlightEvent, now: datetime) -> str:
    """Label an aircraft that is dark past its scheduled time as likely delayed.

    The state machine keeps polling — this only makes the digest honest about
    a departure the source still calls "on time" while nothing is moving.
    """
    if event.last_telemetry.get("lat") is not None:
        return ""
    if now <= event.scheduled_time + NO_SHOW_GRACE:
        return ""
    late = round((now - event.scheduled_time).total_seconds() / 60)
    kind = "ETD" if event.type == EventType.DEPARTURE else "ETA"
    return f"no ADS-B contact {late}m past {kind} — likely delayed"


# Airline designators are fixed-width by standard, which is what makes this
# separable at all: an ICAO designator (used in ADS-B callsigns) is exactly
# three letters, and an IATA designator (used in published schedules) is
# exactly two characters — which may include a digit, as in Y4, B6, F9, 9W.
# Both are followed by a 1-4 digit flight number and an optional suffix
# letter. Matching on "all the digits" folds the airline code into the
# number, so Y47790 would never match its own callsign VOI7790.
_ICAO_CALLSIGN_RE = re.compile(r"^([A-Z]{3})(\d{1,4})([A-Z]?)$")
_IATA_FLIGHT_RE = re.compile(r"^([A-Z0-9]{2})(\d{1,4})([A-Z]?)$")


def _designator_digits(text: str | None, pattern: re.Pattern[str]) -> str:
    """The flight number from a designator, or "" if it isn't one.

    Anything that is not a well-formed designator — most importantly a bare
    registration, which some feeds send as the callsign on ferry and
    positioning flights — yields "" so the caller treats it as unknown
    rather than as evidence of a mismatch.
    """
    match = pattern.match((text or "").strip().upper())
    return match.group(2).lstrip("0") if match else ""


def _flight_digits(designator: str | None) -> str:
    """Flight number from either designator form (schedule or callsign)."""
    return _designator_digits(designator, _IATA_FLIGHT_RE) or _designator_digits(
        designator, _ICAO_CALLSIGN_RE
    )


def _callsign_matches_flight(callsign: str | None, flight_number: str) -> bool:
    """Whether live ADS-B telemetry belongs to this scheduled flight.

    Only a real ICAO callsign can contradict a real IATA flight number. If
    either side is missing or malformed we cannot prove anything, so we stay
    silent and leave the normal state machine alone.
    """
    seen = _designator_digits(callsign, _ICAO_CALLSIGN_RE)
    expected = _designator_digits(flight_number, _IATA_FLIGHT_RE)
    return not seen or not expected or seen == expected


def _callsign_confirms_flight(callsign: str | None, flight_number: str) -> bool:
    """Positive proof (not mere absence of contradiction) that the aircraft
    is operating this flight — both designators present and agreeing."""
    seen = _designator_digits(callsign, _ICAO_CALLSIGN_RE)
    expected = _designator_digits(flight_number, _IATA_FLIGHT_RE)
    return bool(seen) and seen == expected


async def _mark_swapped(application: Application, event: FlightEvent, note: str) -> None:
    """End a leg the aircraft is no longer operating.

    No cascade is needed any more: the hourly sync reconciles every pending
    leg of every tail against the source, so a stale sibling cannot outlive
    the next sync pass.
    """
    store: FlightStore = application.bot_data["store"]
    event.status = EventState.SWAPPED
    event.status_note = note
    store.upsert(event)
    append_history(event)
    _cancel_jobs(application, event.id)
    log.info("Leg %s dropped: %s", event.id, note)
    await _digest(application).refresh()


# ---------------------------------------------------------------------------
# Job scheduling
# ---------------------------------------------------------------------------

def schedule_event_jobs(application: Application, event: FlightEvent) -> None:
    """(Re-)register the next timer for a leg based on its state and the clock."""
    if event.status.terminal:
        return
    jq = application.job_queue
    now = _now()
    _cancel_jobs(application, event.id)

    if event.status in (EventState.WAITING_2H, EventState.WAITING_LIVE):
        # Both waiting states behave identically now: the hourly sync owns
        # schedule freshness, so the only timer left is the T-1h live start.
        when = max(event.scheduled_time - LIVE_LEAD, now + timedelta(seconds=10))
        jq.run_once(job_live_start, when=when, name=f"{event.id}:live_start", data=event.id)
    elif event.status in (EventState.LIVE, EventState.TURNAROUND_DELAY):
        jq.run_repeating(
            job_poll, interval=POLL_INTERVAL, first=5, name=f"{event.id}:poll", data=event.id
        )


def _apply_schedule(event: FlightEvent, result: schedule_provider.LegRefresh) -> None:
    """Adopt the source's current time and its own delay figure.

    The note is always recomputed from the latest reading, so an estimate
    that improves (or an aircraft that catches up) clears the label instead
    of leaving a stale one behind.
    """
    if result.new_time is None:
        return
    event.scheduled_time = result.new_time
    delay = result.delay_minutes
    if delay is None:
        return
    if delay >= 1:
        event.status_note = f"delayed {delay}m"
    elif delay <= -1:
        event.status_note = f"early {abs(delay)}m"
    else:
        event.status_note = ""   # back on schedule


async def job_live_start(context: ContextTypes.DEFAULT_TYPE) -> None:
    """T-1h: hand the leg to ADS-B. No network — the hourly sync keeps the
    schedule current, so this only gates on the store-local turnaround check."""
    application = context.application
    store: FlightStore = application.bot_data["store"]
    set_journal_context("live_start")
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
        return

    if await asyncio.to_thread(_has_turnaround_conflict, store, event):
        _enter_turnaround_delay(event)
        store.upsert(event)
        schedule_event_jobs(application, event)
        await _digest(application).refresh()
        log.info("Awaiting turnaround for %s: source ETD predates recorded arrival", event.id)
        return

    event.status = EventState.LIVE
    store.upsert(event)
    schedule_event_jobs(application, event)
    await _digest(application).refresh()
    log.info("Live tracking started for %s", event.id)


async def job_poll(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Every 120s while LIVE: pull ADS-B telemetry, update digest, detect endings."""
    application = context.application
    store: FlightStore = application.bot_data["store"]
    config: Config = application.bot_data["config"]
    set_journal_context("poll")
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
        context.job.schedule_removal()
        return

    airport = config.target_airports.get(event.target_airport) or {}
    telemetry = await asyncio.to_thread(fetch_telemetry, event.tail)
    now = _now()

    # Source-conflict hold: the outbound estimate contradicts something we
    # observed directly (the inbound landed after it, or the aircraft is
    # visibly parked elsewhere). Keep re-reading the source, but only live
    # evidence of the expected flight releases the leg: its callsign airborne,
    # or the aircraft at its own departure gate already wearing that callsign.
    conflicted = event.status == EventState.TURNAROUND_DELAY or await asyncio.to_thread(
        _has_turnaround_conflict, store, event
    )
    if conflicted:
        refresh = await asyncio.to_thread(schedule_provider.refresh_leg_time, event.tail, event)
        set_journal_context("poll.hold", _journal_evidence(telemetry, refresh))
        if refresh.cancelled:
            event.status = EventState.CANCELLED
            event.status_note = ""
            store.upsert(event)
            append_history(event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Flight cancelled while awaiting turnaround: %s", event.id)
            return
        _apply_schedule(event, refresh)

        airborne_as_ours = (
            telemetry is not None
            and not telemetry.on_ground
            and _callsign_matches_flight(telemetry.callsign, event.flight_number)
        )
        origin_dist = None
        if telemetry is not None and airport.get("lat") is not None:
            origin_dist = haversine_nm(
                telemetry.lat, telemetry.lon, airport["lat"], airport["lon"]
            )
        at_origin_as_ours = (
            event.type == EventType.DEPARTURE
            and telemetry is not None
            and telemetry.on_ground
            and origin_dist is not None
            and origin_dist <= GATE_RELEASE_MAX_DIST_NM
            and _callsign_confirms_flight(telemetry.callsign, event.flight_number)
        )
        if airborne_as_ours or at_origin_as_ours:
            set_journal_context(
                "poll.hold.released", _journal_evidence(telemetry, refresh)
            )
            event.status = EventState.LIVE  # fall through to normal detection
            if event.status_note in (TURNAROUND_CONFLICT_NOTE,) or "awaiting" in event.status_note:
                event.status_note = ""
            store.upsert(event)
        else:
            if refresh.completed:
                # The source recorded the flight as flown while we held the
                # leg — whatever premise justified the hold is gone.
                _conclude_from_source(event, refresh)
                store.upsert(event)
                append_history(event)
                await _digest(application).refresh()
                context.job.schedule_removal()
                log.info("Leg %s concluded per source: %s", event.id, event.status.value)
                return
            if await asyncio.to_thread(_has_turnaround_conflict, store, event):
                note = TURNAROUND_CONFLICT_NOTE
            else:
                # No landing-vs-ETD impossibility, so the source is not in a
                # known-faulty window: its word decides whether a held leg
                # still exists at all.
                if refresh.swapped or refresh.new_time is None:
                    await _mark_swapped(application, event, WITHDRAWN_NOTE)
                    context.job.schedule_removal()
                    return
                note = await asyncio.to_thread(
                    _position_conflict_note, event, telemetry, airport
                ) or _foreign_callsign_note(event, telemetry)
            if note:
                if now > event.scheduled_time + LIVE_MAX_OVERRUN:
                    event.status = EventState.LOST
                    event.status_note = "gave up waiting"
                    store.upsert(event)
                    append_history(event)
                    _schedule_verification(application, event)
                    await _digest(application).refresh()
                    context.job.schedule_removal()
                    log.info("Leg %s abandoned while awaiting turnaround", event.id)
                    return
                event.status = EventState.TURNAROUND_DELAY
                event.status_note = note
                store.upsert(event)
                await _digest(application).refresh()
                return
            # Conflict resolved but the flight is not airborne yet: wait normally.
            event.status = EventState.WAITING_LIVE
            store.upsert(event)
            await _digest(application).refresh()
            schedule_event_jobs(application, event)
            log.info("Source conflict cleared for %s", event.id)
            return

    if telemetry is None:
        never_seen = event.last_telemetry.get("lat") is None
        if never_seen and now > event.scheduled_time + NO_SHOW_GRACE:
            # Dark past its time. Ask the source right away — a delayed
            # estimate gets mirrored within minutes instead of leaving a
            # bare "no ADS-B contact yet" line. But absence of the leg from
            # the source only counts once the older deadline has passed,
            # and we never invent an outcome: no explanation just means
            # "likely delayed" until the hard cap.
            refresh = await asyncio.to_thread(
                schedule_provider.refresh_leg_time, event.tail, event
            )
            set_journal_context("poll.no_show", _journal_evidence(refresh=refresh))
            action = _apply_delay_pushback(event, refresh)
            if action == "delayed":
                store.upsert(event)
                await _digest(application).refresh()
                schedule_event_jobs(application, event)
                context.job.schedule_removal()
                log.info("Leg %s pushed back: %s", event.id, event.status_note)
                return
            if action == "cancelled":
                event.status, event.status_note = EventState.CANCELLED, ""
            elif action == "completed":
                _conclude_from_source(event, refresh)
            elif action == "swapped" and now > event.scheduled_time + LOST_TIMEOUT:
                event.status, event.status_note = EventState.SWAPPED, WITHDRAWN_NOTE
            elif now > event.scheduled_time + LIVE_MAX_OVERRUN:
                event.status, event.status_note = EventState.LOST, ""
            else:
                note = _no_show_note(event, now)
                if note and event.status_note != note:
                    event.status_note = note
                    store.upsert(event)
                    await _digest(application).refresh()
                return
            store.upsert(event)
            append_history(event)
            if event.status == EventState.LOST:
                _schedule_verification(application, event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Leg %s concluded without signal: %s", event.id, event.status.value)
            return

        outcome = _conclude_dark_leg(event, now)
        if outcome is not None:
            state, note = outcome
            set_journal_context(
                "poll.dark_leg",
                _journal_evidence(last_telemetry=dict(event.last_telemetry)),
            )
            event.status, event.status_note = state, note
            store.upsert(event)
            append_history(event)
            _schedule_verification(application, event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Leg %s concluded without signal: %s", event.id, event.status.value)
        return

    # A mismatched callsign means the aircraft is not flying THIS leg right
    # now — most often because it is still on (or fresh off) the previous
    # leg of its own rotation, wearing that flight's callsign. That proves
    # "not yet", never "never": whether the leg is truly gone is the
    # source's call. It still guards conclusions — foreign telemetry must
    # never complete our leg (the original WN3043/WN4244 poison case).
    if not _callsign_matches_flight(telemetry.callsign, event.flight_number):
        refresh = await asyncio.to_thread(
            schedule_provider.refresh_leg_time, event.tail, event
        )
        set_journal_context("poll.callsign_mismatch", _journal_evidence(telemetry, refresh))
        if refresh.cancelled:
            event.status = EventState.CANCELLED
            event.status_note = ""
            store.upsert(event)
            append_history(event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Flight cancelled while on another leg: %s", event.id)
            return
        if refresh.swapped or refresh.new_time is None:
            # Source-confirmed: the flight no longer runs with this aircraft.
            await _mark_swapped(
                application, event, f"aircraft operating {telemetry.callsign} instead"
            )
            context.job.schedule_removal()
            return
        if refresh.completed:
            # The source records this leg as already flown — the mismatched
            # callsign is just the aircraft wearing its next assignment (the
            # WN3982 case: landed 10:51, at the gate already squawking the
            # next flight by the time a rebuild re-created the leg).
            _conclude_from_source(event, refresh)
            store.upsert(event)
            append_history(event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Leg %s concluded per source: %s", event.id, event.status.value)
            return
        # Still ours per the source: mirror its delay and hold for the rotation.
        _apply_schedule(event, refresh)
        event.status = EventState.TURNAROUND_DELAY
        event.status_note = (
            _foreign_callsign_note(event, telemetry) or TURNAROUND_CONFLICT_NOTE
        )
        store.upsert(event)
        await _digest(application).refresh()
        log.info("Holding %s: %s", event.id, event.status_note)
        return

    set_journal_context("poll.detection", _journal_evidence(telemetry))
    dist_nm = None
    if airport.get("lat") is not None:
        dist_nm = haversine_nm(telemetry.lat, telemetry.lon, airport["lat"], airport["lon"])
    prev_divert_hits = event.last_telemetry.get("divert_hits") or 0
    # An aircraft still sitting at its origin is on the ground and far from
    # the destination — indistinguishable from a diversion unless we require
    # having actually seen it fly this leg first. Without this, a delayed
    # departure gets reported as "diverted" at its own origin airport.
    was_airborne = bool(event.last_telemetry.get("was_airborne")) or not telemetry.on_ground
    divert_candidate = (
        event.type == EventType.ARRIVAL
        and telemetry.on_ground
        and was_airborne
        and dist_nm is not None
        and dist_nm >= DIVERT_MIN_DIST_NM
    )
    event.last_telemetry = {
        "lat": telemetry.lat,
        "lon": telemetry.lon,
        "alt": telemetry.alt_ft,
        "gs": telemetry.gs_kts,
        "dist_nm": dist_nm,
        "on_ground": telemetry.on_ground,
        "baro_rate": telemetry.baro_rate,
        "was_airborne": was_airborne,
        "seen_at": now.isoformat(),
        "divert_hits": prev_divert_hits + 1 if divert_candidate else 0,
    }

    finished = False
    stamp = fmt_local(now)
    if event.type == EventType.ARRIVAL:
        near = dist_nm is not None and dist_nm <= LANDED_MAX_DIST_NM
        low = telemetry.on_ground or telemetry.alt_ft < LANDED_MAX_ALT_FT
        if near and low:
            event.status = EventState.LANDED
            event.status_note = stamp
            finished = True
        elif divert_candidate and prev_divert_hits + 1 >= DIVERT_CONFIRM_POLLS:
            where = await asyncio.to_thread(airport_db.nearest, telemetry.lat, telemetry.lon)
            place = f" near {where.iata or where.icao}" if where else ""
            event.status = EventState.DIVERTED
            event.status_note = (
                f"on ground{place}, {dist_nm:.0f} NM from {event.target_airport} (~{stamp})"
            )
            finished = True
    else:
        airborne = not telemetry.on_ground
        away = dist_nm is not None and dist_nm >= DEPARTED_MIN_DIST_NM
        high = telemetry.alt_ft >= DEPARTED_MIN_ALT_FT
        if airborne and (high or away):
            event.status = EventState.DEPARTED
            event.status_note = stamp
            finished = True

    # Keep the displayed time honest while a leg is live. State is untouched
    # here — conclusions still come only from observation.
    #
    # A live arrival still far out mirrors the source's ETA continuously —
    # not just when late. Earliness matters as much as lateness to a spotter
    # (a long-haul running 30 minutes early is a missed shot), and the
    # stored estimate froze at the last pending-side sync. Memoised: ~1 real
    # read per 5 min per tracked arrival, only while it stays far out.
    if (
        not finished
        and event.type == EventType.ARRIVAL
        and dist_nm is not None
        and dist_nm > ARRIVAL_LATE_MIN_DIST_NM
    ):
        refresh = await asyncio.to_thread(
            schedule_provider.refresh_leg_time, event.tail, event
        )
        set_journal_context("poll.enroute_eta", _journal_evidence(telemetry, refresh))
        if (
            refresh.new_time is not None
            and abs((refresh.new_time - event.scheduled_time).total_seconds()) >= 60
        ):
            _apply_schedule(event, refresh)
        elif now > event.scheduled_time + NO_SHOW_GRACE:
            # Source offers nothing newer, but the aircraft is visibly not
            # going to make its time — say so rather than staying silent.
            late = round((now - event.scheduled_time).total_seconds() / 60)
            event.status_note = f"running {late}m late"

    if not finished and now > event.scheduled_time + NO_SHOW_GRACE:
        late = round((now - event.scheduled_time).total_seconds() / 60)
        if event.type == EventType.DEPARTURE and telemetry.on_ground:
            # A visible, overdue, parked departure gets the same source checks
            # a dark one does — otherwise its stored time fossilizes (nothing
            # else may touch a seen live leg) and a big gate delay would ride
            # into the 3h cap as a false LOST. Memoised: ~1 real read per
            # 5 min, only while the aircraft sits here. A swapped/absent
            # answer is deliberately ignored — we are looking at the aircraft.
            refresh = await asyncio.to_thread(
                schedule_provider.refresh_leg_time, event.tail, event
            )
            set_journal_context("poll.gate_delay", _journal_evidence(telemetry, refresh))
            if refresh.cancelled:
                event.status = EventState.CANCELLED
                event.status_note = ""
                store.upsert(event)
                append_history(event)
                await _digest(application).refresh()
                context.job.schedule_removal()
                log.info("Flight cancelled at the gate: %s", event.id)
                return
            if (
                refresh.new_time is not None
                and refresh.new_time > event.scheduled_time + DELAY_MIN_PUSHBACK
            ):
                _apply_schedule(event, refresh)
                if event.scheduled_time - now > LIVE_LEAD:
                    # The corrected ETD left the live window: hand the leg
                    # back to the mirror and re-arm live start at its T-1h.
                    event.status = EventState.WAITING_LIVE
                    store.upsert(event)
                    await _digest(application).refresh()
                    schedule_event_jobs(application, event)
                    context.job.schedule_removal()
                    log.info(
                        "Leg %s stood down until %s (gate delay)",
                        event.id, event.scheduled_time,
                    )
                    return
            else:
                event.status_note = f"running {late}m late — still on the ground"

    # Hard cap: don't chase a leg that never resolves (holding forever, bad data).
    if not finished and now > event.scheduled_time + LIVE_MAX_OVERRUN:
        event.status = EventState.LOST
        event.status_note = "gave up waiting"
        finished = True

    store.upsert(event)
    if finished:
        append_history(event)
        _schedule_verification(application, event)
    await _digest(application).refresh()
    if finished:
        context.job.schedule_removal()
        log.info("Leg %s finished: %s", event.id, event.status.value)


DELAY_MIN_PUSHBACK = timedelta(minutes=10)


def _apply_delay_pushback(
    event: FlightEvent, refresh: schedule_provider.LegRefresh
) -> str | None:
    """When a no-show flight's schedule moved later, wait instead of giving up.

    Returns "delayed" (event mutated back to WAITING_LIVE), "cancelled",
    "completed" (the source recorded the flight as flown — a coverage gap on
    our side), "swapped", or None when the schedule offers no explanation.
    """
    if refresh.cancelled:
        return "cancelled"
    if refresh.completed:
        return "completed"
    if refresh.swapped:
        return "swapped"
    if refresh.new_time is not None and refresh.new_time > event.scheduled_time + DELAY_MIN_PUSHBACK:
        delay_min = round((refresh.new_time - event.scheduled_time).total_seconds() / 60)
        event.scheduled_time = refresh.new_time
        event.status = EventState.WAITING_LIVE
        event.status_note = f"delayed {delay_min}m"
        return "delayed"
    return None


def _conclude_dark_leg(event: FlightEvent, now: datetime) -> tuple[EventState, str] | None:
    """Decide what a signal-less poll means for a LIVE leg, if anything yet.

    Returns (state, note) to finish the leg, or None to keep polling.
    """
    last = event.last_telemetry
    if last.get("lat") is None:
        # Never saw the aircraft at all. job_poll owns the source re-checks
        # and "likely delayed" annotation; here we only enforce the hard cap.
        if now > event.scheduled_time + LIVE_MAX_OVERRUN:
            return EventState.LOST, ""
        return None

    seen_at_raw = last.get("seen_at")
    if not seen_at_raw:
        return None
    silent_for = now - datetime.fromisoformat(seen_at_raw)
    if silent_for < SILENT_GRACE:
        return None

    stamp = fmt_local(now)
    if event.type == EventType.ARRIVAL:
        alt = last.get("alt")
        dist = last.get("dist_nm")
        rate = last.get("baro_rate")
        # Low + near is only a landing if the plane wasn't climbing — a
        # go-around (or fresh departure) looks identical except for the rate.
        climbing = rate is not None and rate > 500
        on_approach = (
            (last.get("on_ground") or (alt is not None and alt <= APPROACH_MAX_ALT_FT))
            and dist is not None
            and dist <= APPROACH_MAX_DIST_NM
            and not climbing
        )
        if on_approach:
            return EventState.LANDED, f"~{stamp} (signal lost on approach)"
    else:
        if not last.get("on_ground"):
            return EventState.DEPARTED, f"~{stamp} (signal lost after takeoff)"

    if now > event.scheduled_time + LIVE_MAX_OVERRUN:
        return EventState.LOST, "signal lost"
    return None


# ---------------------------------------------------------------------------
# Post-conclusion verification
#
# ~25 minutes after live tracking concludes a leg, its verdict is compared
# with the source's row. Direct observations (a watched touchdown, a
# double-confirmed diversion) stand even when the source disagrees; weak
# inferences (LOST, signal-loss guesses) defer to whatever the source can
# prove. Every check is logged to history.jsonl for auditing.
# ---------------------------------------------------------------------------

VERIFIABLE_STATES = (
    EventState.LANDED, EventState.DEPARTED, EventState.DIVERTED, EventState.LOST
)


def _schedule_verification(application: Application, event: FlightEvent) -> None:
    application.job_queue.run_once(
        job_verify_conclusion, when=VERIFY_DELAY, name=f"{event.id}:verify", data=event.id
    )


def _conclusion_is_strong(event: FlightEvent) -> bool:
    """Whether the conclusion rests on direct observation rather than inference."""
    if event.status == EventState.LOST:
        return False
    return "signal lost" not in (event.status_note or "")


def _row_status_text(row: dict) -> str:
    status = (((row.get("status") or {}).get("generic")) or {}).get("status") or {}
    return str(status.get("text", "")).lower()


def _reconcile_conclusion(
    event: FlightEvent, rows: list[dict], now: datetime
) -> str | None:
    """Compare a live-tracking conclusion with the source's row for the leg.

    Returns "confirmed", "annotated", "corrected" or "revived" — the event is
    mutated for the last three — or None when the source has no matching row
    to compare against.
    """
    key = "arrival" if event.type == EventType.ARRIVAL else "departure"
    best = schedule_provider._best_leg_row(rows, event)
    if best is None:
        return None
    estimate, row = best
    text = _row_status_text(row)
    real_stamp = ((row.get("time") or {}).get("real") or {}).get(key)
    real = datetime.fromtimestamp(real_stamp, tz=timezone.utc) if real_stamp else None
    live = bool((row.get("status") or {}).get("live"))
    if event.type == EventType.ARRIVAL:
        # "live" means en route — that confirms a departure, not an arrival.
        completed = real is not None or text.startswith("landed")
    else:
        completed = real is not None or live or text.startswith(("landed", "departed"))
    says_diverted = "divert" in text
    says_cancelled = "cancel" in text

    if event.status in (EventState.LANDED, EventState.DEPARTED):
        if says_diverted or says_cancelled:
            if _conclusion_is_strong(event):
                event.status_note = (event.status_note + " (source disagrees)").strip()
                return "annotated"
            event.status = EventState.DIVERTED if says_diverted else EventState.CANCELLED
            event.status_note = "per source"
            return "corrected"
        if completed:
            if real is not None and "signal lost" in (event.status_note or ""):
                event.status_note = f"{fmt_local(real)} (confirmed by source)"
                return "corrected"
            return "confirmed"
        # The source still shows the flight pending.
        if _conclusion_is_strong(event):
            return "confirmed"  # we watched it happen; the source just lags
        if estimate > now:
            event.status = EventState.WAITING_LIVE
            event.scheduled_time = estimate
            event.status_note = "reopened — source shows the flight still pending"
            return "revived"
        event.status_note = (event.status_note + " (unconfirmed by source)").strip()
        return "annotated"

    if event.status == EventState.DIVERTED:
        if says_diverted:
            return "confirmed"
        # Two ground fixes far from the target beat a lagging status page.
        event.status_note = (event.status_note + " (source disagrees)").strip()
        return "annotated"

    # LOST — inherently weak; adopt whatever the source can prove.
    if says_cancelled:
        event.status = EventState.CANCELLED
        event.status_note = "per source"
        return "corrected"
    if completed:
        event.status = (
            EventState.LANDED if event.type == EventType.ARRIVAL else EventState.DEPARTED
        )
        stamp = real or estimate
        event.status_note = f"~{fmt_local(stamp)} (per source)"
        return "corrected"
    if estimate > now:
        event.status = EventState.WAITING_LIVE
        event.scheduled_time = estimate
        event.status_note = "reopened — source still expects this flight"
        return "revived"
    return "confirmed"  # neither of us can prove anything; LOST stands


async def job_verify_conclusion(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    store: FlightStore = application.bot_data["store"]
    set_journal_context("verify")
    event = store.get(context.job.data)
    if event is None or event.status not in VERIFIABLE_STATES:
        return
    rows = await asyncio.to_thread(schedule_provider.fetch_flight_list, event.tail)
    if rows is None:
        log.info("Verification skipped for %s — source unreachable", event.id)
        return
    action = _reconcile_conclusion(event, rows, _now())
    if action is None:
        log.info("Verification for %s: no matching source row", event.id)
        return
    if action == "confirmed":
        log.info("Verification for %s: source agrees (%s)", event.id, event.status.value)
        return
    set_journal_context(f"verify.{action}")
    store.upsert(event)
    if action == "revived":
        schedule_event_jobs(application, event)
    else:
        append_history(event)
    await _digest(application).refresh()
    log.info(
        "Verification for %s: %s -> %s (%s)",
        event.id, action, event.status.value, event.status_note,
    )


# ---------------------------------------------------------------------------
# Harvesting
# ---------------------------------------------------------------------------

SAME_LEG_TOLERANCE = timedelta(hours=3)


def _same_flight_leg(a: FlightEvent, b: FlightEvent) -> bool:
    """Same physical flight leg, even if the estimated time drifted between
    harvests (event ids embed the time, so ids alone can't dedupe this)."""
    return (
        a.tail == b.tail
        and a.type == b.type
        and a.target_airport == b.target_airport
        and a.flight_number == b.flight_number
        and a.route_origin == b.route_origin
        and a.route_destination == b.route_destination
        and abs((a.scheduled_time - b.scheduled_time).total_seconds())
        <= SAME_LEG_TOLERANCE.total_seconds()
    )


def _register_new_events(
    application: Application, events: list[FlightEvent]
) -> list[FlightEvent]:
    """Store + schedule any events we haven't seen yet; returns the genuinely new
    ones (a flight whose time merely drifted updates in place and is not new)."""
    store: FlightStore = application.bot_data["store"]
    created: list[FlightEvent] = []
    for event in events:
        if store.get(event.id) is not None:
            continue  # already tracked (e.g. /refresh re-run)
        existing = next(
            (ev for ev in store.events.values() if _same_flight_leg(ev, event)), None
        )
        if existing is not None:
            # What the source killed, the source can revive: a leg withdrawn
            # as swapped (or discovered cancelled) whose flight the source now
            # lists for this tail again comes back as a normal pending leg —
            # swap-backs are as routine as swaps (AS751 was swapped off
            # N537AS and back on within a morning). Observed conclusions
            # (landed/departed/diverted) and LOST stay locked; an incoming
            # cancelled row never revives anything.
            if (
                existing.status in (EventState.SWAPPED, EventState.CANCELLED)
                and not event.status.terminal
            ):
                existing.status = EventState.WAITING_2H
                existing.status_note = ""
                existing.scheduled_time = event.scheduled_time
                store.upsert(existing)
                schedule_event_jobs(application, existing)
                created.append(existing)
                log.info(
                    "Leg %s revived: the source lists %s for this tail again",
                    existing.id, existing.flight_number,
                )
                continue
            # Same flight with a drifted estimate: update, don't duplicate —
            # but only while the mirror still owns this leg's schedule
            # (_syncable, the same boundary the sync respects). Once ADS-B has
            # the aircraft in sight, the displayed time is the one the sync
            # last reconciled together with its delay note; nudging the time
            # here would leave that note, which only the sync recomputes,
            # quietly disagreeing with it.
            drift = (event.scheduled_time - existing.scheduled_time).total_seconds()
            if _syncable(existing) and abs(drift) >= 60:
                existing.scheduled_time = event.scheduled_time
                store.upsert(existing)
                if existing.status in (EventState.WAITING_2H, EventState.WAITING_LIVE):
                    schedule_event_jobs(application, existing)
            continue
        store.upsert(event)
        schedule_event_jobs(application, event)
        created.append(event)
    return created


def _dedupe_store(application: Application) -> int:
    """Collapse duplicate legs already in the store (same flight, drifted time).

    Keeps a terminal copy if one exists (it reflects what actually happened),
    otherwise the earliest-scheduled one.
    """
    store: FlightStore = application.bot_data["store"]
    groups: list[list[FlightEvent]] = []
    for event in sorted(store.events.values(), key=lambda e: e.scheduled_time):
        for group in groups:
            if _same_flight_leg(group[0], event):
                group.append(event)
                break
        else:
            groups.append([event])
    removed = 0
    for group in groups:
        if len(group) < 2:
            continue
        keeper = next((e for e in group if e.status.terminal), group[0])
        for event in group:
            if event is keeper:
                continue
            _cancel_jobs(application, event.id)
            store.remove_where(lambda ev, eid=event.id: ev.id == eid)
            removed += 1
    if removed:
        log.info("Removed %d duplicate leg(s)", removed)
    return removed


async def harvest_single(
    application: Application, tail: str
) -> tuple[list[FlightEvent], bool]:
    """Targeted harvest for one tail (used right after /add). Refreshes the digest.

    Returns (new_legs, sources_ok); on source failure, watch mode is armed.
    """
    config: Config = application.bot_data["config"]
    set_journal_context("harvest.add")
    livery = (config.watchlist.get(tail) or {}).get("livery", "")
    events, ok = await asyncio.to_thread(schedule_provider.harvest_tail, tail, livery, config)
    new_legs = _register_new_events(application, events)
    if not ok:
        enable_watch_mode(application)
    await _digest(application).refresh()
    return new_legs, ok


METADATA_PLACEHOLDERS = {"", "Unknown airline", "Unknown type"}


def _is_missing(value: object) -> bool:
    return str(value or "").strip() in METADATA_PLACEHOLDERS


async def heal_unknown_metadata(config: Config) -> int:
    """Re-resolve watchlist entries whose airline or type never came through.

    A tail added while the aircraft is parked has no FR24 schedule rows and no
    live transponder data, so it can land in the watchlist as "Unknown". Once
    the data exists, fill it in rather than leaving it wrong forever.
    """
    stale = [
        tail for tail, meta in config.watchlist.items()
        if _is_missing(meta.get("airline")) or _is_missing(meta.get("model"))
    ]
    healed = 0
    for tail in stale:
        info = await asyncio.to_thread(resolve_aircraft, tail)
        entry = config.watchlist[tail]
        changed = False
        for key in ("airline", "model", "livery", "thumbnail"):
            if _is_missing(entry.get(key)) and not _is_missing(info.get(key)):
                entry[key] = info[key]
                changed = True
        if changed:
            healed += 1
            log.info("Filled in metadata for %s: %s / %s",
                     tail, entry.get("airline"), entry.get("model"))
    if healed:
        config.save()
    return healed


@dataclass
class HarvestResult:
    """Outcome of a two-phase harvest, for the caller to report."""

    board_events: list[FlightEvent] = field(default_factory=list)
    tail_events: list[FlightEvent] = field(default_factory=list)
    failed_tails: list[str] = field(default_factory=list)
    skipped: bool = False          # another harvest was already running
    board_sources_ok: bool = True
    discarded_legs: int = 0        # set by rebuild_schedule

    @property
    def board_legs(self) -> int:
        return len(self.board_events)

    @property
    def tail_legs(self) -> int:
        return len(self.tail_events)

    @property
    def new_legs(self) -> int:
        return self.board_legs + self.tail_legs

    @property
    def new_events(self) -> list[FlightEvent]:
        """Everything newly discovered this harvest, in chronological order."""
        return sorted(
            self.board_events + self.tail_events, key=lambda e: e.scheduled_time
        )


# One harvest at a time. A sweep now outlives the /refresh cooldown, so this
# is what actually prevents two overlapping sweeps hammering the sources.
_harvest_lock = asyncio.Lock()


def harvest_in_progress() -> bool:
    return _harvest_lock.locked()


# Conclusions we watched happen (each also cross-checked against the source
# by the verification job). A rebuild rebuilds the future — it must not
# rewrite the past, or a landing we directly observed gets re-derived from
# the source alone, badly (the WN3982 case). Derived verdicts (SWAPPED,
# CANCELLED, LOST) stay clearable: they come from reading the source or from
# absence of signal, and re-deriving them is exactly what a rebuild is for.
OBSERVED_CONCLUSIONS = (EventState.LANDED, EventState.DEPARTED, EventState.DIVERTED)


async def rebuild_schedule(application: Application) -> HarvestResult:
    """Re-derive today's schedule from the sources, keeping observed history.

    For when stored state has gone wrong — a stale assignment, a bad status
    frozen into a terminal leg — and the fastest cure is to rebuild from the
    sources rather than reason about what to patch. Deliberately keeps the
    watchlist, airports, aircraft dossiers, history, and every conclusion we
    directly observed (landed / departed / diverted); a wrong one of those is
    /dropflight's job. Everything else — pending legs, derived verdicts, and
    the schedule caches — is cleared and re-derived.
    """
    if _harvest_lock.locked():
        log.info("Harvest already running — rebuild ignored")
        return HarvestResult(skipped=True)

    async with _harvest_lock:
        store: FlightStore = application.bot_data["store"]
        set_journal_context("rebuild")
        removed = store.remove_where(lambda ev: ev.status not in OBSERVED_CONCLUSIONS)
        for event in removed:
            _cancel_jobs(application, event.id)
        discarded = len(removed)

        cached_tails = await asyncio.to_thread(schedule_provider.clear_caches)
        adsb.clear_caches()
        log.info(
            "Rebuild: discarded %d leg(s) (kept %d observed conclusion(s)) "
            "and %d cached schedule(s)",
            discarded, len(store.events), cached_tails,
        )
        result = await _run_harvest_locked(application)
        result.discarded_legs = discarded
        return result


async def run_harvest(application: Application) -> HarvestResult:
    """Two-phase harvest.

    Phase 1 sweeps the airport boards — a handful of requests that cover every
    watched tail at once, so the digest is populated in well under a minute.
    Phase 2 then does the slower authoritative per-tail sweep, which catches
    flights the boards omit (a board entry has no registration until the
    airline assigns one) and refreshes times. Both phases feed the same
    de-duplicating registration path, so a flight found twice updates in place
    rather than appearing twice.
    """
    if _harvest_lock.locked():
        log.info("Harvest already running — ignoring duplicate request")
        return HarvestResult(skipped=True)

    async with _harvest_lock:
        return await _run_harvest_locked(application)


async def _run_harvest_locked(application: Application) -> HarvestResult:
    store: FlightStore = application.bot_data["store"]
    config: Config = application.bot_data["config"]
    set_journal_context("harvest")
    result = HarvestResult()
    await heal_unknown_metadata(config)

    # Roll stale legs (yesterday's) out of the store first, then collapse any
    # duplicates left over from schedule-time drift.
    cutoff = _now() - STALE_AFTER
    for event in store.remove_where(lambda ev: ev.scheduled_time < cutoff):
        _cancel_jobs(application, event.id)
    _dedupe_store(application)

    disable_watch_mode(application)  # re-armed below only if sources fail again

    if not (config.watchlist and config.target_airports):
        log.info("Harvest ran with empty watchlist or no target airports")
        await _digest(application).refresh()
        return result

    # --- Phase 1: airport boards (fast, flat cost) --------------------------
    started = _now()
    board_events, boards_ok = await asyncio.to_thread(
        schedule_provider.harvest_airport_boards, config
    )
    result.board_sources_ok = boards_ok
    result.board_events = _register_new_events(application, board_events)
    log.info(
        "Board sweep: %d new leg(s) from %d airport(s) in %.1fs%s",
        result.board_legs, len(config.target_airports),
        (_now() - started).total_seconds(),
        "" if boards_ok else " (some boards unavailable)",
    )
    # Publish what we have immediately — the slow sweep only adds to this.
    await _digest(application).refresh()

    # --- Phase 2: per-tail sweep (authoritative, catches board gaps) --------
    started = _now()
    for tail, meta in config.watchlist.items():
        events, ok = await asyncio.to_thread(
            schedule_provider.harvest_tail, tail, meta.get("livery", ""), config
        )
        if not ok:
            result.failed_tails.append(tail)
        result.tail_events.extend(_register_new_events(application, events))
    log.info(
        "Tail sweep: %d additional leg(s) across %d tail(s) in %.1fs",
        result.tail_legs, len(config.watchlist), (_now() - started).total_seconds(),
    )

    if result.failed_tails:
        newly_armed = enable_watch_mode(application)
        chat_id = application.bot_data.get("chat_id")
        if newly_armed and chat_id:
            try:
                await application.bot.send_message(
                    chat_id,
                    "⚠️ Schedule sources unreachable for: "
                    + ", ".join(result.failed_tails)
                    + ".\nADS-B watch mode is active — I'll pick these tails up "
                    "from live traffic if they fly to/from your airports.",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to send source-failure alert: %s", exc)

    await _digest(application).refresh()
    log.info("Harvest complete: %d new leg(s) (%d board, %d tail), %d source failure(s)",
             result.new_legs, result.board_legs, result.tail_legs,
             len(result.failed_tails))
    return result


async def purge_events(
    application: Application,
    predicate: Callable[[FlightEvent], bool],
    trigger: str = "purge",
) -> int:
    """Drop matching legs (e.g. after /remove or /rmairport) and refresh the digest."""
    store: FlightStore = application.bot_data["store"]
    set_journal_context(trigger)
    removed = store.remove_where(predicate)
    for event in removed:
        _cancel_jobs(application, event.id)
    if removed:
        await _digest(application).refresh()
    return len(removed)


async def job_daily_harvest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await asyncio.to_thread(rotate_journal)
    await run_harvest(context.application)


# ---------------------------------------------------------------------------
# Hourly mirror sync
#
# The invariant this maintains: a pending leg in the digest always reflects
# what the source currently says, at most one hour stale. Legs being tracked
# live belong to ADS-B and are never touched here. A failed fetch marks legs
# "unverified" rather than dropping them — inconclusive is not gone.
# ---------------------------------------------------------------------------

SYNC_STATES = (
    EventState.WAITING_2H, EventState.WAITING_LIVE, EventState.TURNAROUND_DELAY
)


def _never_seen_live(event: FlightEvent) -> bool:
    """A leg that went live but ADS-B has never actually seen.

    Live in name only — the aircraft is dark at a gate somewhere, so there
    is nothing for the live layer to own yet. Until the first position
    arrives, the mirror keeps custody: a delay published after T-1h must
    still reach the digest.
    """
    return event.status == EventState.LIVE and event.last_telemetry.get("lat") is None


def _syncable(event: FlightEvent) -> bool:
    return event.status in SYNC_STATES or _never_seen_live(event)


RENUMBER_MATCH_TOLERANCE = timedelta(hours=6)  # same bound _best_leg_row uses


def _renumbered_sibling(
    store: FlightStore, leg: FlightEvent, refresh: schedule_provider.LegRefresh
) -> FlightEvent | None:
    """The same movement, already tracked under the number the source now uses.

    When a leg's refresh only matched by route (the source stopped listing
    its number for this tail), the by-route fallback keeps it tracking — the
    right call while it is the only copy. But once discovery has registered
    the movement under its current number, this leg is a stale twin: the
    aircraft was, in every sense that matters, swapped off the old number.
    (Observed as AS1625 and AS1603 side by side for one N537AS arrival.)
    """
    matched = (refresh.matched_number or "").strip().upper()
    if not matched or matched == (leg.flight_number or "").strip().upper():
        return None
    if refresh.new_time is None:
        return None
    for other in store.events.values():
        if (
            other.id != leg.id
            and other.tail == leg.tail
            and other.type == leg.type
            and other.target_airport == leg.target_airport
            and other.route_origin == leg.route_origin
            and other.route_destination == leg.route_destination
            and (other.flight_number or "").strip().upper() == matched
            and abs((other.scheduled_time - refresh.new_time).total_seconds())
            <= RENUMBER_MATCH_TOLERANCE.total_seconds()
        ):
            return other
    return None


async def job_hourly_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_schedule_sync(context.application)


async def job_hot_sync(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_schedule_sync(context.application, hot_only=True)


async def run_schedule_sync(
    application: Application, hot_only: bool = False
) -> dict[str, int] | None:
    """Reconcile pending legs with the source. Returns per-action counts, or
    None when a harvest was already running (it does the same work).

    hot_only restricts the pass to tails with a leg due within HOT_WINDOW —
    the cheap 15-minute lane for flights entering their final stretch.
    """
    if _harvest_lock.locked():
        log.info("Harvest in progress — %s sync skipped", "hot" if hot_only else "hourly")
        return None
    async with _harvest_lock:
        return await _run_sync_locked(application, hot_only=hot_only)


async def _run_sync_locked(
    application: Application, hot_only: bool = False
) -> dict[str, int]:
    store: FlightStore = application.bot_data["store"]
    config: Config = application.bot_data["config"]
    sync_tag = "hot_sync" if hot_only else "hourly_sync"
    set_journal_context(sync_tag)
    now = _now()
    counts = {"tails": 0, "updated": 0, "withdrawn": 0, "cancelled": 0,
              "unverified": 0, "discovered": 0, "conflicts": 0}
    changed = False

    pending_by_tail: dict[str, list[FlightEvent]] = {}
    for ev in store.events.values():
        if _syncable(ev):
            pending_by_tail.setdefault(ev.tail, []).append(ev)
    if hot_only:
        horizon = now + HOT_WINDOW
        pending_by_tail = {
            tail: legs for tail, legs in pending_by_tail.items()
            if any(leg.scheduled_time <= horizon for leg in legs)
        }

    for tail, legs in pending_by_tail.items():
        counts["tails"] += 1
        rows = await asyncio.to_thread(schedule_provider.fetch_flight_list, tail)
        if rows is None:
            set_journal_context(f"{sync_tag}.unverified")
            for leg in legs:
                counts["unverified"] += 1
                if leg.status_note != UNVERIFIED_NOTE:
                    leg.status_note = UNVERIFIED_NOTE
                    store.upsert(leg)
                    changed = True
            continue
        schedule_provider.cache_rows(tail, rows)

        for leg in legs:
            if not _syncable(leg):
                continue  # state moved while we were fetching
            # refresh_leg_time re-reads the same memoised rows, so per-leg
            # reconciliation costs no extra requests beyond the tail fetch.
            refresh = await asyncio.to_thread(
                schedule_provider.refresh_leg_time, tail, leg
            )
            if refresh.cancelled:
                set_journal_context(
                    f"{sync_tag}.cancelled", _journal_evidence(refresh=refresh)
                )
                leg.status = EventState.CANCELLED
                leg.status_note = ""
                store.upsert(leg)
                append_history(leg)
                _cancel_jobs(application, leg.id)
                counts["cancelled"] += 1
                changed = True
                continue
            if refresh.swapped or refresh.new_time is None:
                if leg.status == EventState.TURNAROUND_DELAY or await asyncio.to_thread(
                    _has_turnaround_conflict, store, leg
                ):
                    continue  # known-faulty source window: hold, don't withdraw
                if _never_seen_live(leg) and now <= leg.scheduled_time + LOST_TIMEOUT:
                    continue  # absence right at departure time proves nothing yet
                set_journal_context(
                    f"{sync_tag}.withdrawn", _journal_evidence(refresh=refresh)
                )
                leg.status = EventState.SWAPPED
                if refresh.swapped:
                    leg.status_note = "now flown by another aircraft"
                elif refresh.rerouted:
                    # Not a swap in any real sense: the aircraft still flies
                    # this number, but its route left our airport (WN1050
                    # served OAK one day and BWI the next).
                    leg.status_note = f"flight no longer serves {leg.target_airport}"
                else:
                    leg.status_note = WITHDRAWN_NOTE
                store.upsert(leg)
                append_history(leg)
                _cancel_jobs(application, leg.id)
                counts["withdrawn"] += 1
                changed = True
                continue
            twin = _renumbered_sibling(store, leg, refresh)
            if twin is not None:
                set_journal_context(
                    f"{sync_tag}.renumbered", _journal_evidence(refresh=refresh)
                )
                leg.status = EventState.SWAPPED
                leg.status_note = f"aircraft now operating {refresh.matched_number}"
                store.upsert(leg)
                append_history(leg)
                _cancel_jobs(application, leg.id)
                counts["withdrawn"] += 1
                changed = True
                log.info(
                    "Leg %s withdrawn: movement now tracked as %s (%s)",
                    leg.id, refresh.matched_number, twin.id,
                )
                continue
            if _never_seen_live(leg) and refresh.completed:
                # Flew entirely inside a coverage gap; the source knows.
                set_journal_context(
                    f"{sync_tag}.completed", _journal_evidence(refresh=refresh)
                )
                _conclude_from_source(leg, refresh)
                store.upsert(leg)
                append_history(leg)
                _cancel_jobs(application, leg.id)
                counts["updated"] += 1
                changed = True
                continue
            # Present and running: mirror the source's time and delay figure.
            set_journal_context(f"{sync_tag}.adopt", _journal_evidence(refresh=refresh))
            old_time, old_note = leg.scheduled_time, leg.status_note
            if leg.status_note == UNVERIFIED_NOTE:
                leg.status_note = ""
            _apply_schedule(leg, refresh)
            if (
                _never_seen_live(leg)
                and leg.scheduled_time - now > LIVE_LEAD
            ):
                # The delay pushed the leg back out of its live window: hand
                # it back to the mirror until T-1h comes around again.
                leg.status = EventState.WAITING_LIVE
            if leg.scheduled_time != old_time or leg.status_note != old_note:
                store.upsert(leg)
                if leg.scheduled_time != old_time and leg.status in (
                    EventState.WAITING_2H, EventState.WAITING_LIVE
                ):
                    schedule_event_jobs(application, leg)
                counts["updated"] += 1
                changed = True

        # Free discovery: the rows are already here, so register any legs the
        # boards/daily harvest have not seen yet (or whose time moved so far
        # the reconciler withdrew the old copy).
        set_journal_context(f"{sync_tag}.discovered")
        livery = (config.watchlist.get(tail) or {}).get("livery", "")
        events = schedule_provider.rows_to_events(tail, livery, rows, config, now=now)
        created = _register_new_events(application, events)
        if created:
            counts["discovered"] += len(created)
            changed = True

        # Position sanity: a departure due soon while the aircraft is visibly
        # parked at another airport is the source failing us — hold the leg.
        due = next(
            (
                l for l in legs
                if l.type == EventType.DEPARTURE
                and l.status in (EventState.WAITING_2H, EventState.WAITING_LIVE)
                and l.scheduled_time <= now + POSITION_CHECK_LEAD
            ),
            None,
        )
        if due is not None:
            telemetry = await asyncio.to_thread(fetch_telemetry, tail)
            airport = config.target_airports.get(due.target_airport) or {}
            note = await asyncio.to_thread(
                _position_conflict_note, due, telemetry, airport
            )
            if note:
                set_journal_context(
                    f"{sync_tag}.position_conflict", _journal_evidence(telemetry)
                )
                due.status = EventState.TURNAROUND_DELAY
                due.status_note = note
                store.upsert(due)
                schedule_event_jobs(application, due)
                counts["conflicts"] += 1
                changed = True
                log.info("Position conflict for %s: %s", due.id, note)

    if changed:
        await _digest(application).refresh()
    log.info(
        "%s sync: %d tail(s) — %d updated, %d withdrawn, %d cancelled, "
        "%d discovered, %d position conflict(s), %d unverified",
        "Hot" if hot_only else "Hourly", counts["tails"], counts["updated"],
        counts["withdrawn"], counts["cancelled"], counts["discovered"],
        counts["conflicts"], counts["unverified"],
    )
    return counts


async def job_board_discovery(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_board_discovery(context.application)


async def run_board_discovery(application: Application) -> int | None:
    """Boards-only sweep for legs on tails with nothing currently pending.

    Flat cost (a few requests per airport) regardless of watchlist size; the
    per-tail authoritative sweep still happens at the daily harvest.
    """
    if _harvest_lock.locked():
        log.info("Harvest in progress — board discovery skipped")
        return None
    async with _harvest_lock:
        # Without its own tag, this job's writes get journaled under whatever
        # label another job left behind — job contexts are not as isolated as
        # the ContextVar design assumed (observed as a poll-tagged row
        # changing a pending leg's time).
        set_journal_context("board_discovery")
        config: Config = application.bot_data["config"]
        if not (config.watchlist and config.target_airports):
            return 0
        board_events, ok = await asyncio.to_thread(
            schedule_provider.harvest_airport_boards, config
        )
        created = _register_new_events(application, board_events)
        if created:
            await _digest(application).refresh()
        log.info(
            "Board discovery: %d new leg(s)%s",
            len(created), "" if ok else " (some boards unavailable)",
        )
        return len(created)


# ---------------------------------------------------------------------------
# Schedule-less ADS-B watch mode (last-resort fallback)
#
# When every schedule source fails, we can still catch flights: poll the
# watched tails' live positions every 15 minutes; when one is airborne,
# resolve its route from the callsign (adsbdb.com) and synthesize legs on
# the spot if the route touches a watched airport.
# ---------------------------------------------------------------------------

def enable_watch_mode(application: Application) -> bool:
    """Arm the watch sweep; returns True if it wasn't already running."""
    jq = application.job_queue
    if jq.get_jobs_by_name("adsb_watch"):
        return False
    jq.run_repeating(job_adsb_watch, interval=WATCH_INTERVAL, first=30, name="adsb_watch")
    log.info("ADS-B watch mode armed (every %ds)", WATCH_INTERVAL)
    return True


def disable_watch_mode(application: Application) -> None:
    for job in application.job_queue.get_jobs_by_name("adsb_watch"):
        job.schedule_removal()


def synthesize_watch_events(
    tail: str,
    livery: str,
    telemetry: Telemetry,
    origin: str,
    dest: str,
    flight_no: str,
    config: Config,
    now: datetime,
) -> list[FlightEvent]:
    """Build legs for an airborne aircraft found without a schedule.

    The arrival ETA is estimated from current distance and ground speed; the
    id embeds callsign+date so repeated watch sweeps never duplicate a leg.
    """
    events: list[FlightEvent] = []
    date_tag = now.strftime("%Y%m%d")

    match = config.airport_for_code(dest)
    if match:
        iata, info = match
        dist = haversine_nm(telemetry.lat, telemetry.lon, info["lat"], info["lon"])
        speed = telemetry.gs_kts if telemetry.gs_kts and telemetry.gs_kts > 100 else 400.0
        eta = now + timedelta(hours=dist / speed)
        ev = FlightEvent(
            id=f"{tail}-ARR-{flight_no}-{date_tag}-{iata}",
            tail=tail,
            livery=livery,
            type=EventType.ARRIVAL,
            target_airport=iata,
            scheduled_time=eta,
            route_origin=origin,
            route_destination=dest,
            flight_number=flight_no,
            status=EventState.LIVE,
        )
        ev.status_note = "found via ADS-B watch"
        events.append(ev)

    match = config.airport_for_code(origin)
    if match:
        iata, info = match
        dist = haversine_nm(telemetry.lat, telemetry.lon, info["lat"], info["lon"])
        if dist <= WATCH_DEP_MAX_DIST_NM:
            # We can see the aircraft actually leaving this airport — trustworthy.
            ev = FlightEvent(
                id=f"{tail}-DEP-{flight_no}-{date_tag}-{iata}",
                tail=tail,
                livery=livery,
                type=EventType.DEPARTURE,
                target_airport=iata,
                scheduled_time=now,
                route_origin=origin,
                route_destination=dest,
                flight_number=flight_no,
            )
            if telemetry.alt_ft >= DEPARTED_MIN_ALT_FT or dist >= DEPARTED_MIN_DIST_NM:
                ev.status = EventState.DEPARTED
                ev.status_note = f"~{fmt_local(now)} (detected via ADS-B watch)"
            else:
                ev.status = EventState.LIVE
                ev.status_note = "found via ADS-B watch"
            events.append(ev)
        else:
            log.info(
                "Watch: skipping DEP leg for %s — %.0f NM from claimed origin %s "
                "(route DB may be stale)", tail, dist, origin,
            )

    return events


async def job_adsb_watch(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    store: FlightStore = application.bot_data["store"]
    config: Config = application.bot_data["config"]
    set_journal_context("watch")
    now = _now()
    created = 0

    for tail, meta in config.watchlist.items():
        telemetry = await asyncio.to_thread(fetch_telemetry, tail)
        await asyncio.sleep(1.5)  # stay polite to the community APIs
        if telemetry is None or telemetry.on_ground or not telemetry.callsign:
            continue
        route = await asyncio.to_thread(resolve_callsign_route, telemetry.callsign)
        if route is None:
            continue
        origin, dest, flight_no = route
        events = synthesize_watch_events(
            tail, meta.get("livery", ""), telemetry, origin, dest, flight_no, config, now
        )
        for event in events:
            if store.get(event.id) is not None:
                continue
            store.upsert(event)
            if event.status.terminal:
                append_history(event)
            else:
                schedule_event_jobs(application, event)
            created += 1

    if created:
        await _digest(application).refresh()
        log.info("ADS-B watch created %d leg(s)", created)


# ---------------------------------------------------------------------------
# Startup rehydration
# ---------------------------------------------------------------------------

def rehydrate(application: Application) -> None:
    """After a restart, resume every non-terminal leg from flights_today.json."""
    store: FlightStore = application.bot_data["store"]
    set_journal_context("startup")
    _dedupe_store(application)
    active = store.active()
    for event in active:
        schedule_event_jobs(application, event)
    if active:
        log.info("Rehydrated %d active leg(s) from disk", len(active))
