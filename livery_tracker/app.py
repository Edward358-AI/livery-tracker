"""Application wiring: builds the bot(s), registers jobs, runs the event loop."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, time as dt_time

from telegram import Bot, BotCommand
from telegram.ext import Application, ContextTypes

from . import bot as bot_module
from . import tracker, updater
from .config import Config, Credentials, harvest_time
from .digest import DigestManager
from .flights import FlightStore

log = logging.getLogger(__name__)

BOT_COMMANDS = [
    BotCommand("add", "Watch a tail number"),
    BotCommand("info", "Full dossier for a registration"),
    BotCommand("remove", "Stop watching a tail"),
    BotCommand("watchlist", "Show watched aircraft"),
    BotCommand("export", "Watchlist as plain text (import-ready)"),
    BotCommand("airports", "Show target airports"),
    BotCommand("addairport", "Add a target airport"),
    BotCommand("rmairport", "Remove a target airport"),
    BotCommand("status", "Tracker status"),
    BotCommand("view", "Change how the digest is grouped"),
    BotCommand("refresh", "Run schedule harvest now"),
    # /rebuild and /dropflight stay out of the tap menu on purpose: they are
    # recovery tools, documented in /help, and work when typed.
    BotCommand("version", "Version + update check"),
    BotCommand("update", "Install the latest release"),
    BotCommand("help", "Show help"),
]


async def job_update_check(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Daily self-update: if a newer release exists, install it and restart."""
    application = context.application
    if not updater.auto_update_enabled():
        return
    available = await asyncio.to_thread(updater.check_for_update)
    if available is None:
        return
    log.info("Auto-update: %s is available", available.tag)
    await bot_module.apply_update_and_restart(
        application, application.bot_data["chat_id"], available
    )


async def _post_init(application: Application) -> None:
    await application.bot.set_my_commands(BOT_COMMANDS)
    await application.bot_data["digest"].ensure_ready()
    tracker.rehydrate(application)
    # Repaint from disk on startup: state can change while we are stopped
    # (an upgrade, a manual edit), and the digest should never lag reality.
    await application.bot_data["digest"].refresh()
    log.info("Livery Tracker is up.")


async def _post_shutdown(application: Application) -> None:
    await application.bot_data["digest"].aclose()


def build_application(creds: Credentials) -> Application:
    application = (
        Application.builder()
        .token(creds.bot_token)
        .post_init(_post_init)
        .post_shutdown(_post_shutdown)
        .build()
    )

    config = Config.load()
    store = FlightStore()
    application.bot_data["config"] = config
    application.bot_data["store"] = store
    application.bot_data["chat_id"] = creds.chat_id

    application.bot_data["digest"] = DigestManager(
        Bot(creds.digest_bot_token), creds.chat_id, store, config, owns_bot=True
    )

    bot_module.register_handlers(application, creds.chat_id)

    hour, minute = harvest_time()
    local_tz = datetime.now().astimezone().tzinfo
    application.job_queue.run_daily(
        tracker.job_daily_harvest,
        time=dt_time(hour=hour, minute=minute, tzinfo=local_tz),
        name="daily_harvest",
    )
    application.job_queue.run_daily(
        job_update_check,
        time=dt_time(hour=4, minute=0, tzinfo=local_tz),
        name="update_check",
    )
    # The mirror layer: hourly reconciliation of pending legs against the
    # source, a 15-minute hot pass for legs entering their final two hours,
    # and a cheap boards-only sweep for newly scheduled legs. All share the
    # harvest lock, so they skip themselves when a harvest runs.
    application.job_queue.run_repeating(
        tracker.job_hourly_sync, interval=tracker.SYNC_INTERVAL, first=900,
        name="hourly_sync",
    )
    application.job_queue.run_repeating(
        tracker.job_hot_sync, interval=tracker.HOT_SYNC_INTERVAL, first=300,
        name="hot_sync",
    )
    application.job_queue.run_repeating(
        tracker.job_board_discovery, interval=tracker.DISCOVERY_INTERVAL, first=3600,
        name="board_discovery",
    )
    log.info(
        "Daily harvest scheduled for %02d:%02d local time; hourly sync, "
        "15-minute hot sync, and 3-hourly board discovery armed",
        hour, minute,
    )
    return application


def run(creds: Credentials) -> int:
    """Run the bot until interrupted. Returns the process exit code —
    RESTART_EXIT_CODE (42) means "please restart me" after a self-update."""
    application = build_application(creds)
    application.run_polling(allowed_updates=["message"])
    return application.bot_data.get("exit_code", 0)


async def harvest_once(creds: Credentials) -> int:
    """One-shot schedule harvest (for --harvest-now / testing)."""
    application = build_application(creds)
    async with application:
        try:
            result = await tracker.run_harvest(application)
            return result.new_legs
        finally:
            await application.bot_data["digest"].aclose()


async def rebuild_once(creds: Credentials) -> tuple[int, int]:
    """One-shot rebuild (for --rebuild). Returns (discarded, re-harvested)."""
    application = build_application(creds)
    async with application:
        try:
            result = await tracker.rebuild_schedule(application)
            return result.discarded_legs, result.new_legs
        finally:
            await application.bot_data["digest"].aclose()
