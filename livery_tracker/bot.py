"""Telegram command handlers — the user-facing control dashboard."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes, filters

from . import __version__
from . import airports as airport_db
from . import tracker, updater
from .config import Config
from .resolver import resolve_aircraft

log = logging.getLogger(__name__)

HELP_TEXT = """<b>✈️ Livery Tracker Commands</b>

<b>Fleet</b>
/add &lt;tail&gt; — watch a registration (livery auto-resolved)
/remove &lt;tail&gt; — stop watching
/watchlist — show watched aircraft

<b>Airports</b>
/airports — show target airports
/addairport &lt;code&gt; — add airport by IATA/ICAO code
/rmairport &lt;code&gt; — remove airport

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


async def _background_harvest(application, chat_id: int, tail: str | None = None) -> None:
    """Run a harvest off the handler path and report back when done."""
    try:
        if tail:
            count, sources_ok = await tracker.harvest_single(application, tail)
        else:
            count = await tracker.run_harvest(application)
            sources_ok = True  # run_harvest sends its own source-failure alert
        message = f"📋 Digest updated — {count} new flight leg(s) found."
        if not sources_ok:
            message += (
                "\n⚠️ Schedule sources were unreachable — ADS-B watch mode is active "
                "and will pick this tail up from live traffic."
            )
        await application.bot.send_message(chat_id, message)
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


async def cmd_watchlist(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    if not config.watchlist:
        await update.message.reply_text("Watchlist is empty. Add a tail with /add <tail>.")
        return
    lines = ["<b>👀 Watched aircraft</b>"]
    for tail, info in sorted(config.watchlist.items()):
        livery = f' — "{info["livery"]}"' if info.get("livery") else ""
        lines.append(f"• <b>{tail}</b> ({info.get('airline', '?')}, {info.get('model', '?')}){livery}")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


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


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    config = _config(context)
    store = context.application.bot_data["store"]
    active = store.active()
    lines = [
        "<b>📊 Tracker status</b>",
        f"• Watched tails: {len(config.watchlist)}",
        f"• Target airports: {', '.join(sorted(config.target_airports)) or 'none'}",
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
    await update.message.reply_text("🔄 Running schedule harvest now...")
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
        ("remove", cmd_remove),
        ("watchlist", cmd_watchlist),
        ("airports", cmd_airports),
        ("addairport", cmd_addairport),
        ("rmairport", cmd_rmairport),
        ("status", cmd_status),
        ("refresh", cmd_refresh),
        ("version", cmd_version),
        ("update", cmd_update),
    ]:
        application.add_handler(CommandHandler(command, handler, filters=owner))
