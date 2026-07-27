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
from datetime import datetime, timedelta, timezone
from typing import Callable

from telegram.ext import Application, ContextTypes

from . import airports as airport_db
from . import schedule_provider
from .adsb import Telemetry, fetch_telemetry, resolve_callsign_route
from .config import Config
from .digest import DigestManager, fmt_local
from .flights import EventState, EventType, FlightEvent, FlightStore, append_history
from .geo import haversine_nm

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
    if result.new_time is not None:
        drift_min = round((result.new_time - event.scheduled_time).total_seconds() / 60)
        if abs(drift_min) >= 1:
            event.status_note = f"{'delayed' if drift_min > 0 else 'early'} {abs(drift_min)}m"
        event.scheduled_time = result.new_time
    event.status = EventState.WAITING_LIVE
    store.upsert(event)
    await _digest(application).refresh()
    schedule_event_jobs(application, event)


async def job_live_start(context: ContextTypes.DEFAULT_TYPE) -> None:
    application = context.application
    store: FlightStore = application.bot_data["store"]
    event = store.get(context.job.data)
    if event is None or event.status.terminal:
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
            event.status, event.status_note = outcome
            store.upsert(event)
            append_history(event)
            await _digest(application).refresh()
            context.job.schedule_removal()
            log.info("Leg %s concluded without signal: %s", event.id, event.status.value)
        return

    dist_nm = None
    if airport.get("lat") is not None:
        dist_nm = haversine_nm(telemetry.lat, telemetry.lon, airport["lat"], airport["lon"])
    prev_divert_hits = event.last_telemetry.get("divert_hits") or 0
    divert_candidate = (
        event.type == EventType.ARRIVAL
        and telemetry.on_ground
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

def _register_new_events(application: Application, events: list[FlightEvent]) -> int:
    """Store + schedule any events we haven't seen yet; returns how many were new."""
    store: FlightStore = application.bot_data["store"]
    new_count = 0
    for event in events:
        if store.get(event.id) is not None:
            continue  # already tracked (e.g. /refresh re-run)
        store.upsert(event)
        schedule_event_jobs(application, event)
        new_count += 1
    return new_count


async def harvest_single(application: Application, tail: str) -> tuple[int, bool]:
    """Targeted harvest for one tail (used right after /add). Refreshes the digest.

    Returns (new_leg_count, sources_ok); on source failure, watch mode is armed.
    """
    config: Config = application.bot_data["config"]
    livery = (config.watchlist.get(tail) or {}).get("livery", "")
    events, ok = await asyncio.to_thread(schedule_provider.harvest_tail, tail, livery, config)
    new_count = _register_new_events(application, events)
    if not ok:
        enable_watch_mode(application)
    await _digest(application).refresh()
    return new_count, ok


async def run_harvest(application: Application) -> int:
    """Scrape schedules for every watched tail and refresh the digest."""
    store: FlightStore = application.bot_data["store"]
    config: Config = application.bot_data["config"]

    # Roll stale legs (yesterday's) out of the store first.
    cutoff = _now() - STALE_AFTER
    for event in store.remove_where(lambda ev: ev.scheduled_time < cutoff):
        _cancel_jobs(application, event.id)

    disable_watch_mode(application)  # re-armed below only if sources fail again
    new_events = 0
    failed_tails: list[str] = []
    if config.watchlist and config.target_airports:
        tails = list(config.watchlist.items())
        for i, (tail, meta) in enumerate(tails):
            events, ok = await asyncio.to_thread(
                schedule_provider.harvest_tail, tail, meta.get("livery", ""), config
            )
            if not ok:
                failed_tails.append(tail)
            new_events += _register_new_events(application, events)
            if i < len(tails) - 1:
                await asyncio.to_thread(schedule_provider.polite_delay)
    else:
        log.info("Harvest ran with empty watchlist or no target airports")

    if failed_tails:
        newly_armed = enable_watch_mode(application)
        chat_id = application.bot_data.get("chat_id")
        if newly_armed and chat_id:
            try:
                await application.bot.send_message(
                    chat_id,
                    "⚠️ Schedule sources unreachable for: "
                    + ", ".join(failed_tails)
                    + ".\nADS-B watch mode is active — I'll pick these tails up "
                    "from live traffic if they fly to/from your airports.",
                )
            except Exception as exc:  # noqa: BLE001
                log.warning("Failed to send source-failure alert: %s", exc)

    await _digest(application).refresh()
    log.info("Harvest complete: %d new leg(s), %d source failure(s)",
             new_events, len(failed_tails))
    return new_events


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
    active = store.active()
    for event in active:
        schedule_event_jobs(application, event)
    if active:
        log.info("Rehydrated %d active leg(s) from disk", len(active))
