"""The daily digest: one Telegram message per day, always edited in place.

Optionally posted by a second, dedicated bot (DIGEST_BOT_TOKEN) so the command
chat stays free of flight traffic entirely.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta

from telegram import Bot
from telegram.constants import ParseMode
from telegram.error import BadRequest

from .config import Config, atomic_write_json, data_dir
from .flights import EventState, EventType, FlightEvent, FlightStore

log = logging.getLogger(__name__)

STATE_EMOJI = {
    EventState.WAITING_2H: "🟡",
    EventState.WAITING_LIVE: "🕒",
    EventState.TURNAROUND_DELAY: "⚠️",
    EventState.LIVE: "🚨",
    EventState.LANDED: "✅",
    EventState.DEPARTED: "🛫",
    EventState.DIVERTED: "↪️",
    EventState.CANCELLED: "❌",
    EventState.SWAPPED: "🔀",
    EventState.LOST: "⚠️",
}


def fmt_local(when: datetime) -> str:
    """'3:45 PM PDT', or 'Mon 3:45 PM PDT' when the time isn't today.

    The 24h harvest window can pull in tomorrow's flights, so a bare time
    would be ambiguous. Windows spells out zone names; compress to initials.
    """
    local = when.astimezone()
    tz = local.strftime("%Z")
    if " " in tz:
        tz = "".join(word[0] for word in tz.split())
    day = f"{local.strftime('%a')} " if local.date() != datetime.now().astimezone().date() else ""
    return f"{day}{local.strftime('%I:%M %p').lstrip('0')} {tz}".strip()


def _gate_suffix(event: FlightEvent) -> str:
    """' · T2 D15' when the source publishes gate/terminal, '' otherwise."""
    terminal = event.terminal
    label = " ".join(
        part for part in (f"T{terminal}" if terminal.isdigit() else terminal, event.gate)
        if part
    )
    return f" · {label}" if label else ""


def _leg_detail(event: FlightEvent) -> str:
    """The status phrase for one leg ('ETA 3:45 PM PDT', '12,400 ft · ...', ...)."""
    when_label = "ETA" if event.type == EventType.ARRIVAL else "ETD"
    if event.status == EventState.LIVE:
        tele = event.last_telemetry
        bits = []
        if tele.get("alt") is not None:
            bits.append(f"{tele['alt']:,} ft")
        if tele.get("gs") is not None:
            bits.append(f"{tele['gs']:.0f} kts")
        if tele.get("dist_nm") is not None:
            bits.append(f"{tele['dist_nm']:.0f} NM {'out' if event.type == EventType.ARRIVAL else 'away'}")
        # The expected time stays on the line even once telemetry is flowing:
        # "48 NM out" alone never says whether that is early, late, or on time.
        detail = f"{when_label} {fmt_local(event.scheduled_time)}{_gate_suffix(event)} — "
        detail += " · ".join(bits) if bits else "polling, no ADS-B contact yet"
        if event.status_note:
            detail += f" ({event.status_note})"
        return detail
    if event.status == EventState.LANDED:
        return f"landed {event.status_note}".strip()
    if event.status == EventState.DEPARTED:
        return f"departed {event.status_note}".strip()
    if event.status == EventState.DIVERTED:
        return f"diverted — {event.status_note}" if event.status_note else "diverted"
    if event.status == EventState.CANCELLED:
        return "cancelled"
    if event.status == EventState.SWAPPED:
        # The state machinery writes a precise reason ("now flown by another
        # aircraft", "flight no longer serves OAK", "aircraft now operating
        # AS1603") — show it rather than flattening every case to "swapped".
        return event.status_note or "aircraft swapped off this flight"
    if event.status == EventState.LOST:
        return f"tracking lost{f' ({event.status_note})' if event.status_note else ''}"
    if event.status == EventState.TURNAROUND_DELAY:
        return event.status_note or "Awaiting turnaround / source conflict"
    detail = f"{when_label} {fmt_local(event.scheduled_time)}{_gate_suffix(event)}"
    if event.status == EventState.WAITING_LIVE and event.status_note:
        detail += f" ({event.status_note})"
    return detail


# Equipment type codes (B739, A388, ...) come from the aircraft dossier
# cache. Loaded at most every few minutes, so rendering never does a file
# read per leg; a tail without a known code simply shows no label.
_TYPE_CODES: dict[str, str] = {}
_type_codes_loaded = 0.0
_TYPE_CODE_TTL = 600.0


def _type_code(tail: str) -> str:
    global _TYPE_CODES, _type_codes_loaded
    from . import aircraft

    now = time.monotonic()
    if now - _type_codes_loaded > _TYPE_CODE_TTL:
        _TYPE_CODES = {
            reg.upper(): str(entry.get("type_code") or "").upper()
            for reg, entry in aircraft.load_cache().items()
        }
        _type_codes_loaded = now
    return _TYPE_CODES.get(tail.upper(), "")


def _tail_label(tail: str) -> str:
    code = _type_code(tail)
    return f"<b>{tail}</b> ({code})" if code else f"<b>{tail}</b>"


def format_leg(event: FlightEvent, show_airport: bool = True) -> str:
    """One leg. `show_airport` is off when the section header already names it."""
    livery = f' "{event.livery}"' if event.livery else ""
    flight = f" {event.flight_number}" if event.flight_number else ""
    route = f"{event.route_origin}➔{event.route_destination}{flight}"
    emoji = STATE_EMOJI.get(event.status, "•")
    where = f" @ {event.target_airport}" if show_airport else ""
    return f"{emoji} {_tail_label(event.tail)}{livery} — {route}{where}, {_leg_detail(event)}"


def _merged_line(dep: FlightEvent, arr: FlightEvent) -> str:
    """One line for a flight between two watched airports (two legs under the hood)."""
    livery = f' "{dep.livery or arr.livery}"' if (dep.livery or arr.livery) else ""
    flight = f" {dep.flight_number}" if dep.flight_number else ""
    route = f"{dep.route_origin}➔{dep.route_destination}{flight}"
    # Show the emoji of the phase currently in progress; once airborne, the arrival's.
    current = dep if not dep.status.terminal else arr
    emoji = STATE_EMOJI.get(current.status, "•")
    return f"{emoji} {_tail_label(dep.tail)}{livery} — {route}, {_leg_detail(dep)} → {_leg_detail(arr)}"


MAX_PAIR_GAP = timedelta(hours=20)  # dep and arr legs of one flight are at most this far apart


def _pair_legs(
    events: list[FlightEvent],
) -> tuple[list[tuple[FlightEvent, FlightEvent]], list[FlightEvent], list[FlightEvent]]:
    """Match departure+arrival legs of the same flight (both endpoints watched)."""
    arrivals = [e for e in events if e.type == EventType.ARRIVAL]
    departures = [e for e in events if e.type == EventType.DEPARTURE]
    pairs: list[tuple[FlightEvent, FlightEvent]] = []
    paired_ids: set[str] = set()
    for arr in arrivals:
        for dep in departures:
            if dep.id in paired_ids:
                continue
            same_flight = (
                dep.tail == arr.tail
                and dep.flight_number == arr.flight_number
                and dep.route_origin == arr.route_origin
                and dep.route_destination == arr.route_destination
            )
            if same_flight and timedelta(0) <= arr.scheduled_time - dep.scheduled_time <= MAX_PAIR_GAP:
                pairs.append((dep, arr))
                paired_ids.update((dep.id, arr.id))
                break
    solo_arrivals = [e for e in arrivals if e.id not in paired_ids]
    solo_departures = [e for e in departures if e.id not in paired_ids]
    return pairs, solo_arrivals, solo_departures


# ---------------------------------------------------------------------------
# Grouping modes — how the digest sections the day's flights (/view)
# ---------------------------------------------------------------------------

GROUP_MODES = {
    "type": "arrivals vs departures",
    "airport": "all traffic per airport",
    "airline": "one section per airline",
}
DEFAULT_GROUP_MODE = "type"

Section = tuple[str, list[str]]


def _airline_of(event: FlightEvent, config: Config) -> str:
    """Airline for a tail, from the metadata /add resolved into the watchlist."""
    return (config.watchlist.get(event.tail) or {}).get("airline") or "Unknown airline"


def _sections_by_type(events: list[FlightEvent], config: Config) -> list[Section]:
    """Default: arrivals and departures, with two-airport flights merged."""
    pairs, arrivals, departures = _pair_legs(events)
    sections: list[Section] = []
    if pairs:
        sections.append(
            ("🔁 <b>Between your airports</b>", [_merged_line(d, a) for d, a in pairs])
        )
    if arrivals:
        sections.append(("🛬 <b>Arrivals</b>", [format_leg(e) for e in arrivals]))
    if departures:
        sections.append(("🛫 <b>Departures</b>", [format_leg(e) for e in departures]))
    return sections


def _sections_by_airport(events: list[FlightEvent], config: Config) -> list[Section]:
    """All traffic in and out of each airport, in time order.

    Legs are deliberately not merged here: a flight between two watched
    airports is a real departure at one and a real arrival at the other, so
    it belongs under both headings.
    """
    buckets: dict[str, list[FlightEvent]] = {}
    for event in events:
        buckets.setdefault(event.target_airport, []).append(event)

    sections: list[Section] = []
    for code in sorted(buckets):
        name = (config.target_airports.get(code) or {}).get("name", "")
        title = f"🛬🛫 <b>{code}</b>" + (f" — {name}" if name else "")
        legs = sorted(buckets[code], key=lambda e: e.scheduled_time)
        sections.append((title, [format_leg(e, show_airport=False) for e in legs]))
    return sections


def _sections_by_airline(events: list[FlightEvent], config: Config) -> list[Section]:
    """One section per airline, each in time order."""
    pairs, arrivals, departures = _pair_legs(events)
    buckets: dict[str, list[tuple[datetime, str]]] = {}

    def add(event: FlightEvent, line: str) -> None:
        buckets.setdefault(_airline_of(event, config), []).append(
            (event.scheduled_time, line)
        )

    for dep, arr in pairs:
        add(dep, _merged_line(dep, arr))
    for event in arrivals + departures:
        add(event, format_leg(event))

    sections: list[Section] = []
    for airline in sorted(buckets):
        lines = [line for _, line in sorted(buckets[airline], key=lambda item: item[0])]
        sections.append((f"🏢 <b>{airline}</b>", lines))
    return sections


_SECTION_BUILDERS = {
    "type": _sections_by_type,
    "airport": _sections_by_airport,
    "airline": _sections_by_airline,
}


# Telegram rejects a message over 4096 UTF-16 units; leave room for the
# footer and a continuation marker rather than sailing right up to it.
TELEGRAM_LIMIT = 4096
SAFE_LIMIT = 3900


def telegram_length(text: str) -> int:
    """Telegram counts UTF-16 code units, so most emoji cost 2, not 1."""
    return len(text.encode("utf-16-le")) // 2


def _digest_body(store: FlightStore, config: Config, now: datetime) -> list[Section]:
    events = sorted(store.events.values(), key=lambda e: e.scheduled_time)
    if not events:
        return [("", ["No watched aircraft scheduled at your target airports today."])]
    builder = _SECTION_BUILDERS.get(config.digest_group_by, _sections_by_type)
    return builder(events, config)


def render_digest(store: FlightStore, config: Config, now: datetime | None = None) -> str:
    """The whole digest as one string (used when it comfortably fits)."""
    return "\n".join(render_digest_parts(store, config, now, limit=None))


def render_digest_parts(
    store: FlightStore,
    config: Config,
    now: datetime | None = None,
    limit: int | None = SAFE_LIMIT,
) -> list[str]:
    """The digest as one message, or several when it would exceed `limit`.

    Splits on section boundaries where possible, and within a section when a
    single section is itself too long, so the digest can never become
    unsendable no matter how many aircraft are watched. `limit=None` disables
    splitting entirely.
    """
    now = now or datetime.now().astimezone()
    airports = ", ".join(sorted(config.target_airports)) or "no airports configured"
    header = [
        f"✈️ <b>LIVERY DIGEST — {now.strftime('%a %b %d')}</b>",
        f"<i>Watching {len(config.watchlist)} aircraft at {airports}</i>",
        "",
    ]
    footer = f"<i>Updated {fmt_local(now)}</i>"

    body: list[str] = []
    for title, section_lines in _digest_body(store, config, now):
        if title:
            body.append(title)
        body.extend(section_lines)
        body.append("")

    if limit is None:
        return ["\n".join(header + body + [footer])]

    continuation = [
        f"✈️ <b>LIVERY DIGEST — {now.strftime('%a %b %d')}</b> <i>(cont.)</i>",
        "",
    ]
    parts: list[str] = []
    current = list(header)
    has_body = False

    for line in body:
        # Reserve room for whichever trailer this chunk ends up carrying.
        projected = telegram_length("\n".join(current + [line, footer]))
        if has_body and projected > limit:
            parts.append("\n".join(current + ["<i>(continued below)</i>"]).rstrip())
            current = list(continuation)
            has_body = False
        current.append(line)
        has_body = True

    parts.append("\n".join(current + [footer]).rstrip())
    return parts


class DigestManager:
    """Owns today's digest message: sends it once per day, edits it thereafter."""

    def __init__(
        self, bot: Bot, chat_id: int, store: FlightStore, config: Config, owns_bot: bool = False
    ) -> None:
        self.bot = bot
        self.chat_id = chat_id
        self.store = store
        self.config = config
        self.owns_bot = owns_bot  # separate digest bot we must initialize/shutdown
        self._ready = not owns_bot

    def _state_path(self):
        return data_dir() / "digest_state.json"

    def _load_state(self) -> dict:
        path = self._state_path()
        if path.exists():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                pass
        return {}

    async def ensure_ready(self) -> None:
        if not self._ready:
            await self.bot.initialize()
            self._ready = True

    async def aclose(self) -> None:
        if self.owns_bot and self._ready:
            await self.bot.shutdown()
            self._ready = False

    @staticmethod
    def _stored_ids(state: dict) -> list[int]:
        """Message ids from state, accepting the pre-split single-id format."""
        ids = state.get("message_ids")
        if isinstance(ids, list) and ids:
            return ids
        single = state.get("message_id")
        return [single] if single else []

    async def _delete(self, message_ids: list[int]) -> None:
        for message_id in message_ids:
            try:
                await self.bot.delete_message(chat_id=self.chat_id, message_id=message_id)
            except Exception as exc:  # noqa: BLE001 - >48h old, or already gone
                log.debug("Could not delete digest message %s: %s", message_id, exc)

    async def _send_all(self, parts: list[str], today: str) -> None:
        sent: list[int] = []
        try:
            for text in parts:
                msg = await self.bot.send_message(
                    chat_id=self.chat_id,
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                sent.append(msg.message_id)
        except Exception as exc:  # noqa: BLE001
            log.error("Digest send failed: %s", exc)
        if sent:
            atomic_write_json(self._state_path(), {"date": today, "message_ids": sent})

    async def refresh(self) -> None:
        """Re-render and push the digest.

        Normally this is a single message edited in place. Once a watchlist
        grows past what fits in one Telegram message the digest is split, and
        the parts are edited in place just the same — only a change in the
        *number* of parts forces a resend.
        """
        try:
            await self.ensure_ready()
        except Exception as exc:  # noqa: BLE001
            log.error("Digest bot failed to initialize: %s", exc)
            return

        parts = render_digest_parts(self.store, self.config)
        today = datetime.now().astimezone().date().isoformat()
        state = self._load_state()
        stored = self._stored_ids(state)

        # New day: drop yesterday's digest so the chat holds only today's.
        if stored and state.get("date") != today:
            await self._delete(stored)
            stored = []

        if state.get("date") == today and stored:
            if len(stored) == len(parts):
                for message_id, text in zip(stored, parts):
                    try:
                        await self.bot.edit_message_text(
                            chat_id=self.chat_id,
                            message_id=message_id,
                            text=text,
                            parse_mode=ParseMode.HTML,
                            disable_web_page_preview=True,
                        )
                    except BadRequest as exc:
                        if "not modified" not in str(exc).lower():
                            log.warning("Digest edit failed: %s", exc)
                    except Exception as exc:  # noqa: BLE001
                        log.warning("Digest edit failed: %s", exc)
                return
            # The digest grew or shrank by a whole message — rebuild it.
            log.info("Digest split changed (%d -> %d parts) — resending",
                     len(stored), len(parts))
            await self._delete(stored)

        await self._send_all(parts, today)
