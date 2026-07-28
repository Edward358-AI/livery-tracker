"""Event scheduler and per-leg state machine.

Lifecycle per leg:
  WAITING_2H  --(T-2h: re-scrape ETA/ETD)-->  WAITING_LIVE
  WAITING_LIVE --(T-45m arr / T-15m dep)-->   LIVE (ADS-B poll every 120s)
  LIVE --> LANDED / DEPARTED / LOST (terminal)

Every state change re-renders the single daily digest message. All timers live
in python-telegram-bot's JobQueue, so a restart only needs `rehydrate()` to
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
from .flights import EventState, EventType, FlightEvent, FlightStore, append_history
from .geo import haversine_nm
from .resolver import resolve_aircraft

log = logging.getLogger(__name__)

REFRESH_LEAD = timedelta(hours=2)
LIVE_LEAD = {EventType.ARRIVAL: timedelta(minutes=45), EventType.DEPARTURE: timedelta(minutes=15)}
POLL_INTERVAL = 120  # seconds
LOST_TIMEOUT = timedelta(minutes=30)
STALE_AFTER = timedelta(hours=12)  # events this far past schedule are purged at harvest

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


def _digest(application: Application) -> DigestManager:
    return application.bot_data["digest"]


def _cancel_jobs(application: Application, event_id: str) -> None:
    for suffix in ("refresh", "live_start", "poll"):
        for job in application.job_queue.get_jobs_by_name(f"{event_id}:{suffix}"):
            job.schedule_removal()


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


async def _mark_swapped(
    application: Application, event: FlightEvent, note: str, cascade: bool = True
) -> None:
    """End a leg the aircraft is no longer operating, and check its siblings.

    An aircraft taken off one flight has usually lost the rest of that
    rotation too, so the tail's other pending legs are re-verified rather
    than left sitting in the digest until each reaches its own T-2h check.
    """
    store: FlightStore = application.bot_data["store"]
    event.status = EventState.SWAPPED
    event.status_note = note
    store.upsert(event)
    append_history(event)
    _cancel_jobs(application, event.id)
    log.info("Leg %s dropped: %s", event.id, note)

    if cascade:
        await _recheck_rotation(application, event.tail, skip_id=event.id)
    await _digest(application).refresh()


async def _recheck_rotation(
    application: Application, tail: str, skip_id: str | None = None
) -> int:
    """Re-verify a tail's other pending legs after a confirmed swap.

    fetch_flight_list memoises for five minutes, so these checks normally
    reuse the rows already fetched and cost no extra requests.
    """
    store: FlightStore = application.bot_data["store"]
    siblings = [
        ev for ev in store.events.values()
        if ev.tail == tail
        and ev.id != skip_id
        and ev.status in (EventState.WAITING_2H, EventState.WAITING_LIVE)
    ]
    dropped = 0
    for sibling in siblings:
        refresh = await asyncio.to_thread(
            schedule_provider.refresh_leg_time, sibling.tail, sibling
        )
        if refresh.swapped:
            # cascade=False: one pass is enough, and it keeps this bounded.
            await _mark_swapped(
                application, sibling, "now flown by another aircraft", cascade=False
            )
            dropped += 1
    if dropped:
        log.info("Rotation re-check dropped %d further leg(s) for %s", dropped, tail)
    return dropped


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

    if event.status == EventState.WAITING_2H:
        when = max(event.scheduled_time - REFRESH_LEAD, now + timedelta(seconds=10))
        jq.run_once(job_refresh, when=when, name=f"{event.id}:refresh", data=event.id)
    elif event.status == EventState.WAITING_LIVE:
        when = max(event.scheduled_time - LIVE_LEAD[event.type], now + timedelta(seconds=10))
        jq.run_once(job_live_start, when=when, name=f"{event.id}:live_start", data=event.id)
    elif event.status == EventState.LIVE:
        jq.run_repeating(
            job_poll, interval=POLL_INTERVAL, first=5, name=f"{event.id}:poll", data=event.id
        )


async def job_refresh(context: ContextTypes.DEFAULT_TYPE) -> None:
    """T-2h: re-scrape the schedule, update ETA/ETD, refresh the digest."""
    application = context.application
    store: FlightStore = application.bot_data["store"]
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
        return

    result = await asyncio.to_thread(schedule_provider.refresh_leg_time, event.tail, event)
    if result.cancelled:
        event.status = EventState.CANCELLED
        store.upsert(event)
        append_history(event)
        await _digest(application).refresh()
        log.info("Flight cancelled: %s", event.id)
        return
    if result.swapped:
        # The flight is operating, just not with our aircraft. End the leg here:
        # letting it reach live tracking would watch the *aircraft* rather than
        # the flight, and report a departure/landing from whatever it really flew.
        await _mark_swapped(application, event, "now flown by another aircraft")
        return
    _apply_schedule(event, result)
    event.status = EventState.WAITING_LIVE
    store.upsert(event)
    await _digest(application).refresh()
    schedule_event_jobs(application, event)


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
    application = context.application
    store: FlightStore = application.bot_data["store"]
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
        return

    # One last schedule read before we start believing ADS-B. Estimates firm
    # up close to the event, so this is where a T-2h figure that has since
    # improved gets corrected — and a late swap or cancellation gets caught.
    result = await asyncio.to_thread(
        schedule_provider.refresh_leg_time, event.tail, event
    )
    if result.cancelled:
        event.status = EventState.CANCELLED
        store.upsert(event)
        append_history(event)
        await _digest(application).refresh()
        log.info("Flight cancelled before live tracking: %s", event.id)
        return
    if result.swapped:
        await _mark_swapped(application, event, "now flown by another aircraft")
        return
    _apply_schedule(event, result)

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
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
        context.job.schedule_removal()
        return

    airport = config.target_airports.get(event.target_airport) or {}
    telemetry = await asyncio.to_thread(fetch_telemetry, event.tail)
    now = _now()

    if telemetry is None:
        outcome = _conclude_dark_leg(event, now)
        if outcome is not None:
            state, note = outcome
            if state == EventState.LOST and event.last_telemetry.get("lat") is None:
                # Never seen and past deadline — but maybe it's just delayed.
                refresh = await asyncio.to_thread(
                    schedule_provider.refresh_leg_time, event.tail, event
                )
                action = _apply_delay_pushback(event, refresh)
                if action == "delayed":
                    store.upsert(event)
                    await _digest(application).refresh()
                    schedule_event_jobs(application, event)
                    context.job.schedule_removal()
                    log.info("Leg %s pushed back: %s", event.id, event.status_note)
                    return
                if action == "cancelled":
                    state, note = EventState.CANCELLED, ""
                elif action == "swapped":
                    state, note = EventState.SWAPPED, "now flown by another aircraft"
            event.status, event.status_note = state, note
            store.upsert(event)
            append_history(event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Leg %s concluded without signal: %s", event.id, event.status.value)
        return

    # A tail can have stale schedule assignments. Never use one aircraft's
    # takeoff/landing to complete a different numbered flight — that was the
    # cause of N8619F being shown as both WN3043 and WN4244 from one OAK
    # departure. The mismatch is strong evidence the aircraft was swapped.
    if not _callsign_matches_flight(telemetry.callsign, event.flight_number):
        await _mark_swapped(
            application, event, f"aircraft operating {telemetry.callsign} instead"
        )
        context.job.schedule_removal()
        return

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

    # Hard cap: don't chase a leg that never resolves (holding forever, bad data).
    if not finished and now > event.scheduled_time + LIVE_MAX_OVERRUN:
        event.status = EventState.LOST
        event.status_note = "gave up waiting"
        finished = True

    store.upsert(event)
    if finished:
        append_history(event)
    await _digest(application).refresh()
    if finished:
        context.job.schedule_removal()
        log.info("Leg %s finished: %s", event.id, event.status.value)


DELAY_MIN_PUSHBACK = timedelta(minutes=10)


def _apply_delay_pushback(
    event: FlightEvent, refresh: schedule_provider.LegRefresh
) -> str | None:
    """When a no-show flight's schedule moved later, wait instead of giving up.

    Returns "delayed" (event mutated back to WAITING_LIVE), "cancelled", or
    None when the schedule offers no explanation.
    """
    if refresh.cancelled:
        return "cancelled"
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
        # Never saw the aircraft at all.
        if now > event.scheduled_time + LOST_TIMEOUT:
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
            # Same flight with a drifted estimate: update, don't duplicate.
            drift = (event.scheduled_time - existing.scheduled_time).total_seconds()
            if not existing.status.terminal and abs(drift) >= 60:
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


async def rebuild_schedule(application: Application) -> HarvestResult:
    """Throw away today's tracked legs and cached schedules, then re-harvest.

    For when stored state has gone wrong — a stale assignment, a bad status
    frozen into a terminal leg — and the fastest cure is to rebuild from the
    sources rather than reason about what to patch. Deliberately keeps the
    watchlist, airports, aircraft dossiers and history: none of those are
    schedule state.
    """
    if _harvest_lock.locked():
        log.info("Harvest already running — rebuild ignored")
        return HarvestResult(skipped=True)

    async with _harvest_lock:
        store: FlightStore = application.bot_data["store"]
        for event in list(store.events.values()):
            _cancel_jobs(application, event.id)
        discarded = len(store.events)
        store.clear()

        cached_tails = await asyncio.to_thread(schedule_provider.clear_caches)
        adsb.clear_caches()
        log.info(
            "Rebuild: discarded %d leg(s) and %d cached schedule(s)",
            discarded, cached_tails,
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
    application: Application, predicate: Callable[[FlightEvent], bool]
) -> int:
    """Drop matching legs (e.g. after /remove or /rmairport) and refresh the digest."""
    store: FlightStore = application.bot_data["store"]
    removed = store.remove_where(predicate)
    for event in removed:
        _cancel_jobs(application, event.id)
    if removed:
        await _digest(application).refresh()
    return len(removed)


async def job_daily_harvest(context: ContextTypes.DEFAULT_TYPE) -> None:
    await run_harvest(context.application)


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
    _dedupe_store(application)
    active = store.active()
    for event in active:
        schedule_event_jobs(application, event)
    if active:
        log.info("Rehydrated %d active leg(s) from disk", len(active))
