"""The daily digest: one Telegram message per day, always edited in place.

Optionally posted by a second, dedicated bot (DIGEST_BOT_TOKEN) so the command
chat stays free of flight traffic entirely.
"""

from __future__ import annotations

import json
import logging
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
    EventState.LIVE: "🚨",
    EventState.LANDED: "✅",
    EventState.DEPARTED: "🛫",
    EventState.DIVERTED: "↪️",
    EventState.CANCELLED: "❌",
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
        return " · ".join(bits) or "live tracking active"
    if event.status == EventState.LANDED:
        return f"landed {event.status_note}".strip()
    if event.status == EventState.DEPARTED:
        return f"departed {event.status_note}".strip()
    if event.status == EventState.DIVERTED:
        return f"diverted — {event.status_note}" if event.status_note else "diverted"
    if event.status == EventState.CANCELLED:
        return "cancelled"
    if event.status == EventState.LOST:
        return f"tracking lost{f' ({event.status_note})' if event.status_note else ''}"
    detail = f"{when_label} {fmt_local(event.scheduled_time)}"
    if event.status == EventState.WAITING_LIVE and event.status_note:
        detail += f" ({event.status_note})"
    return detail


def _leg_line(event: FlightEvent) -> str:
    livery = f' "{event.livery}"' if event.livery else ""
    flight = f" {event.flight_number}" if event.flight_number else ""
    route = f"{event.route_origin}➔{event.route_destination}{flight}"
    emoji = STATE_EMOJI.get(event.status, "•")
    return f"{emoji} <b>{event.tail}</b>{livery} — {route} @ {event.target_airport}, {_leg_detail(event)}"


def _merged_line(dep: FlightEvent, arr: FlightEvent) -> str:
    """One line for a flight between two watched airports (two legs under the hood)."""
    livery = f' "{dep.livery or arr.livery}"' if (dep.livery or arr.livery) else ""
    flight = f" {dep.flight_number}" if dep.flight_number else ""
    route = f"{dep.route_origin}➔{dep.route_destination}{flight}"
    # Show the emoji of the phase currently in progress; once airborne, the arrival's.
    current = dep if not dep.status.terminal else arr
    emoji = STATE_EMOJI.get(current.status, "•")
    return f"{emoji} <b>{dep.tail}</b>{livery} — {route}, {_leg_detail(dep)} → {_leg_detail(arr)}"


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


def render_digest(store: FlightStore, config: Config, now: datetime | None = None) -> str:
    now = now or datetime.now().astimezone()
    events = sorted(store.events.values(), key=lambda e: e.scheduled_time)
    pairs, arrivals, departures = _pair_legs(events)

    airports = ", ".join(sorted(config.target_airports)) or "no airports configured"
    lines = [
        f"✈️ <b>LIVERY DIGEST — {now.strftime('%a %b %d')}</b>",
        f"<i>Watching {len(config.watchlist)} aircraft at {airports}</i>",
        "",
    ]
    if not events:
        lines.append("No watched aircraft scheduled at your target airports today.")
    else:
        if pairs:
            lines.append("🔁 <b>Between your airports</b>")
            lines.extend(_merged_line(dep, arr) for dep, arr in pairs)
            lines.append("")
        if arrivals:
            lines.append("🛬 <b>Arrivals</b>")
            lines.extend(_leg_line(e) for e in arrivals)
            lines.append("")
        if departures:
            lines.append("🛫 <b>Departures</b>")
            lines.extend(_leg_line(e) for e in departures)
            lines.append("")
    lines.append(f"<i>Updated {fmt_local(now)}</i>")
    return "\n".join(lines)


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

    async def refresh(self) -> None:
        """Re-render and push the digest: edit today's message, or send a fresh one."""
        try:
            await self.ensure_ready()
        except Exception as exc:  # noqa: BLE001
            log.error("Digest bot failed to initialize: %s", exc)
            return
        text = render_digest(self.store, self.config)
        today = datetime.now().astimezone().date().isoformat()
        state = self._load_state()

        # New day: delete yesterday's digest so the chat only ever holds one message.
        if state.get("message_id") and state.get("date") != today:
            try:
                await self.bot.delete_message(chat_id=self.chat_id, message_id=state["message_id"])
            except Exception as exc:  # noqa: BLE001 - >48h old or already gone
                log.debug("Could not delete old digest message: %s", exc)

        if state.get("date") == today and state.get("message_id"):
            try:
                await self.bot.edit_message_text(
                    chat_id=self.chat_id,
                    message_id=state["message_id"],
                    text=text,
                    parse_mode=ParseMode.HTML,
                    disable_web_page_preview=True,
                )
                return
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return
                log.warning("Digest edit failed (%s) — sending a new message", exc)
            except Exception as exc:  # noqa: BLE001
                log.warning("Digest edit failed: %s", exc)
                return

        try:
            msg = await self.bot.send_message(
                chat_id=self.chat_id,
                text=text,
                parse_mode=ParseMode.HTML,
                disable_web_page_preview=True,
            )
            atomic_write_json(self._state_path(), {"date": today, "message_id": msg.message_id})
        except Exception as exc:  # noqa: BLE001
            log.error("Digest send failed: %s", exc)
