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
    BotCommand("remove", "Stop watching a tail"),
    BotCommand("watchlist", "Show watched aircraft"),
    BotCommand("airports", "Show target airports"),
    BotCommand("addairport", "Add a target airport"),
    BotCommand("rmairport", "Remove a target airport"),
    BotCommand("status", "Tracker status"),
    BotCommand("refresh", "Run schedule harvest now"),
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
    log.info("Daily harvest scheduled for %02d:%02d local time", hour, minute)
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
            return await tracker.run_harvest(application)
        finally:
            await application.bot_data["digest"].aclose()
