"""Telegram command handlers — the user-facing control dashboard."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from . import __version__
from . import aircraft as aircraft_db
from . import airports as airport_db
from . import tracker, updater
from .config import Config
from .digest import DEFAULT_GROUP_MODE, GROUP_MODES, SAFE_LIMIT, format_leg, telegram_length
from .resolver import resolve_aircraft
from .throttle import Cooldown

# Commands that fan out to the free APIs get per-user spacing so an impatient
# tap-tap-tap can't turn into a burst of scraping. Tuned to be invisible in
# normal use: /info is cheap enough to repeat, /refresh sweeps every tail.
COOLDOWNS = {
    "info": Cooldown(seconds=10),
    "add": Cooldown(seconds=10),
    "refresh": Cooldown(seconds=120),
}


async def _rate_limited(update: Update, name: str) -> bool:
    """True (and warns the user) when this command is still cooling down."""
    wait = COOLDOWNS[name].remaining(update.effective_chat.id)
    if wait <= 0:
        return False
    await update.message.reply_text(
        f"⏳ Easy there — /{name} again in {wait:.0f}s "
        "(keeps us polite to the free data sources)."
    )
    return True

log = logging.getLogger(__name__)

HELP_TEXT = """<b>✈️ Livery Tracker Commands</b>

<b>Fleet</b>
/add &lt;tail&gt; — watch a registration (livery auto-resolved)
/remove &lt;tail&gt; — stop watching
/watchlist — show watched aircraft
/info &lt;tail&gt; — full dossier: aircraft details, live position, schedule
/dropflight &lt;tail&gt; &lt;flight&gt; — remove one stale flight assignment

<b>Airports</b>
/airports — show target airports
/addairport &lt;code&gt; — add airport by IATA/ICAO code
/rmairport &lt;code&gt; — remove airport

<b>Digest layout</b>
/view — how the digest groups flights
/view type — arrivals vs departures (default)
/view airport — all traffic per airport
/view airline — one section per airline

<b>Tracking</b>
/refresh — re-run today's schedule harvest now
/status — tracker status
/version — running version + update check
/update — install the latest release now
/help — this message

Today's flights live in your digest bot's chat — one message per day,
updated in place, so this chat stays clean."""


def _config(context: ContextTypes.DEFAULT_TYPE) -> Config:
    return context.application.bot_data["config"]


MAX_LISTED_LEGS = 15  # keeps the reply inside Telegram's message limit


def _split_message(text: str, limit: int = SAFE_LIMIT) -> list[str]:
    """Split text into UTF-16-safe Telegram parts without losing characters."""
    if telegram_length(text) <= limit:
        return [text]

    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if telegram_length(line) > limit:
            if current:
                parts.append(current)
                current = ""
            chunk = ""
            chunk_length = 0
            for char in line:
                char_length = telegram_length(char)
                if chunk and chunk_length + char_length > limit:
                    parts.append(chunk)
                    chunk = ""
                    chunk_length = 0
                chunk += char
                chunk_length += char_length
            if chunk:
                parts.append(chunk)
            continue

        if current and telegram_length(current + line) > limit:
            parts.append(current)
            current = ""
        current += line
    if current:
        parts.append(current)
    return parts or [""]


async def _reply_parts(message, text: str, *, parse_mode=None, limit: int = SAFE_LIMIT, **kwargs) -> None:
    """Reply with one or more Telegram-safe messages."""
    for part in _split_message(text, limit):
        await message.reply_text(part, parse_mode=parse_mode, **kwargs)


def _watchlist_text(config: Config) -> str:
    """Render the full watchlist; transport limits are handled by _reply_parts."""
    header = "<b>👀 Watched aircraft</b>"
    lines = [header]
    for tail, info in sorted(config.watchlist.items()):
        livery = f' — "{info["livery"]}"' if info.get("livery") else ""
        lines.append(f"• <b>{tail}</b> ({info.get('airline', '?')}, {info.get('model', '?')}){livery}")
    return "\n".join(lines)


def _describe_new_legs(events) -> str:
    """List the legs a harvest just discovered, newest schedule first."""
    if not events:
        return ""
    shown = events[:MAX_LISTED_LEGS]
    lines = [""] + [format_leg(event) for event in shown]
    if len(events) > len(shown):
        lines.append(f"<i>…and {len(events) - len(shown)} more — see the digest.</i>")
    return "\n".join(lines)


async def _background_harvest(application, chat_id: int, tail: str | None = None) -> None:
    """Run a harvest off the handler path and report back when done."""
    try:
        if tail:
            new_legs, sources_ok = await tracker.harvest_single(application, tail)
            if new_legs:
                message = (
                    f"📋 Found {len(new_legs)} flight leg(s) for {tail}:"
                    + _describe_new_legs(new_legs)
                )
            else:
                message = f"📋 No flights for {tail} at your airports in the next 24h."
            if not sources_ok:
                message += (
                    "\n⚠️ Schedule sources were unreachable — ADS-B watch mode is "
                    "active and will pick this tail up from live traffic."
                )
        else:
            result = await tracker.run_harvest(application)
            if result.skipped:
                message = "⏳ A harvest is already running — this request was ignored."
            elif result.new_legs:
                message = (
                    f"📋 Harvest complete — {result.new_legs} new flight leg(s) "
                    f"({result.board_legs} from airport boards, "
                    f"{result.tail_legs} from the per-tail sweep):"
                    + _describe_new_legs(result.new_events)
                )
            else:
                message = "📋 Harvest complete — nothing new since the last sweep."
        await application.bot.send_message(
            chat_id, message, parse_mode=ParseMode.HTML, disable_web_page_preview=True
        )
    except Exception:  # noqa: BLE001
        log.exception("Background harvest failed")
        try:
            await application.bot.send_message(chat_id, "⚠️ Schedule harvest failed — check logs.")
        except Exception:  # noqa: BLE001
            pass


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(HELP_TEXT, parse_mode=ParseMode.HTML)


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /add <tail>  e.g. /add N265AK")
        return
    tail = context.args[0].upper()
    config = _config(context)
    if tail in config.watchlist:
        await update.message.reply_text(f"{tail} is already on the watchlist.")
        return
    if await _rate_limited(update, "add"):
        return
    await update.message.reply_text(f"🔎 Resolving {tail} via Planespotters/FR24...")
    info = await asyncio.to_thread(resolve_aircraft, tail)
    config.watchlist[tail] = {
        "airline": info["airline"],
        "model": info["model"],
        "livery": info["livery"],
        "thumbnail": info["thumbnail"],
        "added_at": datetime.now(timezone.utc).isoformat(),
    }
    config.save()
    aircraft_db.record_profile(tail, info)  # seed the /info dossier cache
    livery = f'\n• Livery: <b>{info["livery"]}</b>' if info["livery"] else ""
    caption = (
        f"✅ Now watching <b>{tail}</b>\n"
        f"• Airline: {info['airline']}\n"
        f"• Type: {info['model']}{livery}"
    )
    if info["thumbnail"]:
        try:
            await update.message.reply_photo(
                photo=info["thumbnail"], caption=caption, parse_mode=ParseMode.HTML
            )
        except Exception:  # noqa: BLE001 - thumbnail is nice-to-have
            await update.message.reply_text(caption, parse_mode=ParseMode.HTML)
    else:
        await update.message.reply_text(caption, parse_mode=ParseMode.HTML)
    # Pull today's schedule for the new tail right away so the digest reflects it.
    context.application.create_task(
        _background_harvest(context.application, update.effective_chat.id, tail=tail)
    )


async def cmd_info(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Full dossier for any registration — watched or not."""
    if not context.args:
        await update.message.reply_text("Usage: /info <tail>  e.g. /info N265AK")
        return
    if await _rate_limited(update, "info"):
        return
    tail = context.args[0].upper()
    refresh = len(context.args) > 1 and context.args[1].lower() in ("refresh", "-r")

    notice = await update.message.reply_text(f"🔎 Looking up {tail}...")
    report, thumbnail = await asyncio.to_thread(
        aircraft_db.build_report,
        tail,
        _config(context),
        context.application.bot_data["store"],
        refresh,
    )
    try:
        await notice.delete()
    except Exception:  # noqa: BLE001 - cosmetic only
        pass

    if thumbnail:
        try:
            await update.message.reply_photo(
                photo=thumbnail, caption=report, parse_mode=ParseMode.HTML
            )
            return
        except Exception:  # noqa: BLE001 - caption too long, or photo unreachable
            pass
    await update.message.reply_text(
        report, parse_mode=ParseMode.HTML, disable_web_page_preview=True
    )


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /remove <tail>")
        return
    tail = context.args[0].upper()
    config = _config(context)
    if config.watchlist.pop(tail, None) is None:
        await update.message.reply_text(f"{tail} is not on the watchlist.")
        return
    config.save()
    dropped = await tracker.purge_events(context.application, lambda ev: ev.tail == tail)
    note = f" ({dropped} pending leg(s) dropped from today's digest)" if dropped else ""
    await update.message.reply_text(f"🗑 Removed {tail} from the watchlist.{note}")


async def cmd_dropflight(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Remove one bad/stale flight assignment without unwatching its aircraft.

    This is intentionally a maintenance command rather than a normal dashboard
    control: it is useful when a public schedule source leaves a stale aircraft
    assignment behind after an operational swap.
    """
    if len(context.args) != 2:
        await update.message.reply_text("Usage: /dropflight <tail> <flight number>")
        return
    tail, flight_number = (arg.upper() for arg in context.args)
    dropped = await tracker.purge_events(
        context.application,
        lambda ev: ev.tail == tail and ev.flight_number.upper() == flight_number,
    )
    if dropped:
        await update.message.reply_text(
            f"Removed {dropped} stale leg(s) for {tail} {flight_number} from today's digest."
        )
    else:
        await update.message.reply_text(f"No current leg found for {tail} {flight_number}.")


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not config.watchlist:
        await update.message.reply_text("Watchlist is empty. Add a tail with /add <tail>.")
        return
    await _reply_parts(update.message, _watchlist_text(config), parse_mode=ParseMode.HTML)


async def cmd_airports(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not config.target_airports:
        await update.message.reply_text("No target airports. Add one with /addairport <code>.")
        return
    lines = ["<b>🛬 Target airports</b>"]
    for iata, info in sorted(config.target_airports.items()):
        lines.append(
            f"• <b>{iata}</b> / {info.get('icao', '?')} — {info.get('name', '?')} "
            f"({info.get('lat'):.4f}, {info.get('lon'):.4f})"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_addairport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /addairport <IATA or ICAO>  e.g. /addairport LAX")
        return
    code = context.args[0].upper()
    airport = await asyncio.to_thread(airport_db.lookup, code)
    if airport is None:
        await update.message.reply_text(
            f"❓ Couldn't find '{code}' in the airport database. "
            "Double-check the IATA/ICAO code."
        )
        return
    config = _config(context)
    key = airport.iata or airport.icao
    config.target_airports[key] = {
        "icao": airport.icao,
        "name": airport.name,
        "lat": airport.lat,
        "lon": airport.lon,
    }
    config.save()
    await update.message.reply_text(
        f"✅ Added <b>{key}</b> ({airport.icao}) — {airport.name}\n"
        f"📍 {airport.lat:.4f}, {airport.lon:.4f}\n"
        "🔄 Re-harvesting today's schedules for the new airport...",
        parse_mode=ParseMode.HTML,
    )
    context.application.create_task(
        _background_harvest(context.application, update.effective_chat.id)
    )


async def cmd_rmairport(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text("Usage: /rmairport <code>")
        return
    code = context.args[0].upper()
    config = _config(context)
    match = config.airport_for_code(code)
    if match is None:
        await update.message.reply_text(f"{code} is not a configured airport.")
        return
    key = match[0]
    config.target_airports.pop(key)
    config.save()
    dropped = await tracker.purge_events(
        context.application, lambda ev: ev.target_airport == key
    )
    note = f" ({dropped} pending leg(s) dropped from today's digest)" if dropped else ""
    await update.message.reply_text(f"🗑 Removed {key} from target airports.{note}")


async def cmd_view(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Change how the daily digest groups flights, and repaint it immediately."""
    config = _config(context)
    current = config.digest_group_by
    if current not in GROUP_MODES:  # hand-edited config
        current = DEFAULT_GROUP_MODE

    if not context.args:
        lines = [f"<b>📐 Digest layout: {current}</b> — {GROUP_MODES[current]}", ""]
        for mode, description in GROUP_MODES.items():
            marker = "▸" if mode == current else "  "
            lines.append(f"{marker} <code>/view {mode}</code> — {description}")
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)
        return

    mode = context.args[0].lower()
    if mode not in GROUP_MODES:
        await update.message.reply_text(
            f"Unknown layout '{mode}'. Choose: {', '.join(GROUP_MODES)}."
        )
        return
    if mode == current:
        await update.message.reply_text(f"Digest is already grouped by {mode}.")
        return

    config.digest_group_by = mode
    config.save()
    await context.application.bot_data["digest"].refresh()
    await update.message.reply_text(
        f"✅ Digest now grouped by <b>{mode}</b> — {GROUP_MODES[mode]}.\n"
        "Check your digest chat, it has been redrawn.",
        parse_mode=ParseMode.HTML,
    )


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    store = context.application.bot_data["store"]
    active = store.active()
    lines = [
        "<b>📊 Tracker status</b>",
        f"• Watched tails: {len(config.watchlist)}",
        f"• Target airports: {', '.join(sorted(config.target_airports)) or 'none'}",
        f"• Digest layout: {config.digest_group_by} (/view to change)",
        f"• Active flight legs today: {len(active)}",
    ]
    for ev in active:
        lines.append(
            f"  – {ev.tail} {ev.type.value.lower()} @ {ev.target_airport} [{ev.status.value}]"
        )
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def apply_update_and_restart(application, chat_id: int, available) -> None:
    """Install a release and ask the supervisor to restart us (exit code 42)."""
    await application.bot.send_message(chat_id, f"⬇️ Updating to {available.tag}...")
    ok = await asyncio.to_thread(updater.apply_update, available)
    if ok:
        await application.bot.send_message(
            chat_id, f"✅ Updated to {available.tag} — restarting now."
        )
        application.bot_data["exit_code"] = updater.RESTART_EXIT_CODE
        application.stop_running()
    else:
        await application.bot.send_message(
            chat_id, "❌ Update failed — kept the current version. Check the logs."
        )


async def cmd_version(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    lines = [f"🏷 Livery Tracker v{__version__}"]
    if not updater.auto_update_enabled():
        lines.append("Auto-update: off (git checkout, Docker, or LT_AUTO_UPDATE=0)")
    else:
        lines.append("Auto-update: on (checks daily at 4:00 AM)")
        available = await asyncio.to_thread(updater.check_for_update)
        lines.append(
            f"Newer release available: {available.tag} — send /update to install"
            if available else "You are on the latest release."
        )
    await update.message.reply_text("\n".join(lines))


async def cmd_update(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not updater.auto_update_enabled():
        await update.message.reply_text(
            "Updates are managed outside the bot for this install "
            "(git checkout, Docker, or LT_AUTO_UPDATE=0)."
        )
        return
    available = await asyncio.to_thread(updater.check_for_update)
    if available is None:
        await update.message.reply_text(f"Already up to date (v{__version__}).")
        return
    await apply_update_and_restart(
        context.application, update.effective_chat.id, available
    )


async def cmd_refresh(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    # A sweep now outlives the cooldown window, so check the harvest lock too.
    if tracker.harvest_in_progress():
        await update.message.reply_text(
            "⏳ A harvest is already running — sit tight, the digest updates itself."
        )
        return
    if await _rate_limited(update, "refresh"):
        return
    await update.message.reply_text(
        "🔄 Sweeping your airports first (quick), then every tail (slower).\n"
        "The digest updates as soon as the airport sweep lands."
    )
    context.application.create_task(
        _background_harvest(context.application, update.effective_chat.id)
    )


def register_handlers(application: Application, chat_id: int) -> None:
    """Attach all command handlers, restricted to the owner's chat."""
    owner = filters.Chat(chat_id)
    for command, handler in [
        ("start", cmd_help),
        ("help", cmd_help),
        ("add", cmd_add),
        ("info", cmd_info),
        ("query", cmd_info),  # alias
        ("remove", cmd_remove),
        ("dropflight", cmd_dropflight),
        ("watchlist", cmd_watchlist),
        ("airports", cmd_airports),
        ("addairport", cmd_addairport),
        ("rmairport", cmd_rmairport),
        ("status", cmd_status),
        ("view", cmd_view),
        ("refresh", cmd_refresh),
        ("version", cmd_version),
        ("update", cmd_update),
    ]:
        application.add_handler(CommandHandler(command, handler, filters=owner))
