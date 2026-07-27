"""Application wiring: builds the bot(s), registers jobs, runs the event loop."""

from __future__ import annotations

import logging
from datetime import datetime, time as dt_time

from telegram import Bot, BotCommand
from telegram.ext import Application

from . import bot as bot_module
from . import tracker
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
    BotCommand("help", "Show help"),
]


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
    log.info("Daily harvest scheduled for %02d:%02d local time", hour, minute)
    return application


def run(creds: Credentials) -> None:
    """Run the bot until interrupted (long polling)."""
    application = build_application(creds)
    application.run_polling(allowed_updates=["message"])


async def harvest_once(creds: Credentials) -> int:
    """One-shot schedule harvest (for --harvest-now / testing)."""
    application = build_application(creds)
    async with application:
        try:
            return await tracker.run_harvest(application)
        finally:
            await application.bot_data["digest"].aclose()
