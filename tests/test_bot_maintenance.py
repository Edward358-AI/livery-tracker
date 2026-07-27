"""Tests for owner-only maintenance controls."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from livery_tracker import bot
from livery_tracker.config import Config
from livery_tracker.digest import TELEGRAM_LIMIT, telegram_length
from livery_tracker.flights import EventType, FlightEvent
from livery_tracker.tracker import HarvestResult


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []
        self.reply_objects: list[SimpleNamespace] = []
        self.photos: list[dict] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)
        reply = SimpleNamespace(deleted=False)

        async def delete():
            reply.deleted = True

        reply.delete = delete
        self.reply_objects.append(reply)
        return reply

    async def reply_photo(self, **kwargs) -> None:
        self.photos.append(kwargs)


def test_dropflight_removes_only_the_requested_tail_and_flight(monkeypatch):
    message = FakeMessage()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(
        args=["N8619F", "WN4244"], application=object()
    )
    captured = {}

    async def fake_purge(_app, predicate):
        captured["matches_target"] = predicate(
            FlightEvent(
                "a", "N8619F", "", EventType.DEPARTURE, "OAK",
                datetime.now(timezone.utc), flight_number="WN4244",
            )
        )
        captured["keeps_other_flight"] = not predicate(
            FlightEvent(
                "b", "N8619F", "", EventType.DEPARTURE, "OAK",
                datetime.now(timezone.utc), flight_number="WN3043",
            )
        )
        return 1

    monkeypatch.setattr(bot.tracker, "purge_events", fake_purge)
    asyncio.run(bot.cmd_dropflight(update, context))

    assert captured == {"matches_target": True, "keeps_other_flight": True}
    assert "Removed 1 stale leg" in message.replies[0]


def test_dropflight_requires_tail_and_flight_number():
    message = FakeMessage()
    asyncio.run(bot.cmd_dropflight(SimpleNamespace(message=message), SimpleNamespace(args=[])))
    assert message.replies == ["Usage: /dropflight <tail> <flight number>"]


def test_help_documents_dropflight_maintenance_command():
    assert "/dropflight &lt;tail&gt; &lt;flight&gt;" in bot.HELP_TEXT


def test_watchlist_splits_large_fleet_into_telegram_safe_messages():
    config = Config()
    config.watchlist = {
        f"N{i:05d}AA": {
            "airline": "Example Airlines International",
            "model": "Boeing 737-900ER",
            "livery": "A Very Long Special Commemorative Livery Name",
        }
        for i in range(77)
    }
    message = FakeMessage()
    update = SimpleNamespace(message=message)
    context = SimpleNamespace(application=SimpleNamespace(bot_data={"config": config}))

    asyncio.run(bot.cmd_watchlist(update, context))

    assert len(message.replies) > 1
    assert all(telegram_length(part) <= TELEGRAM_LIMIT for part in message.replies)
    combined = "\n".join(message.replies)
    assert all(tail in combined for tail in config.watchlist)


def test_split_message_preserves_large_line_list_within_telegram_limit():
    text = "\n".join(f"• N{i:05d}AA — " + "x" * 80 for i in range(77))

    parts = bot._split_message(text)

    assert len(parts) > 1
    assert all(telegram_length(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(part.replace("\n", "") for part in parts) == text.replace("\n", "")


def test_split_message_breaks_one_pathological_line():
    text = "x" * 5000

    parts = bot._split_message(text)

    assert all(telegram_length(part) <= TELEGRAM_LIMIT for part in parts)
    assert "".join(parts) == text


def test_airports_and_status_split_large_dynamic_responses():
    config = Config()
    config.target_airports = {
        f"A{i:03d}": {
            "icao": f"K{i:03d}",
            "name": "Airport " + "x" * 80,
            "lat": float(i),
            "lon": float(i),
        }
        for i in range(100)
    }
    events = [
        FlightEvent(
            id=f"event-{i}", tail=f"N{i:05d}AA", livery="",
            type=EventType.ARRIVAL, target_airport=f"A{i:03d}",
            scheduled_time=datetime.now(timezone.utc), flight_number=f"XX{i:04d}",
        )
        for i in range(100)
    ]
    application = SimpleNamespace(
        bot_data={"config": config, "store": SimpleNamespace(active=lambda: events)}
    )
    context = SimpleNamespace(application=application)

    airports_message = FakeMessage()
    asyncio.run(bot.cmd_airports(SimpleNamespace(message=airports_message), context))
    status_message = FakeMessage()
    asyncio.run(bot.cmd_status(SimpleNamespace(message=status_message), context))

    for messages, marker in ((airports_message.replies, "A099"), (status_message.replies, "N00099AA")):
        assert len(messages) > 1
        assert all(telegram_length(part) <= TELEGRAM_LIMIT for part in messages)
        assert marker in "\n".join(messages)


def test_info_photo_caption_splits_overflow_into_text_replies(monkeypatch):
    report = "x" * 5000
    message = FakeMessage()
    application = SimpleNamespace(bot_data={"config": Config(), "store": object()})
    context = SimpleNamespace(args=["N265AK"], application=application)

    monkeypatch.setattr(bot.aircraft_db, "build_report", lambda *_args: (report, "https://photo.example/x.jpg"))
    async def no_limit(*_args):
        return False
    monkeypatch.setattr(bot, "_rate_limited", no_limit)

    asyncio.run(bot.cmd_info(SimpleNamespace(message=message), context))

    assert telegram_length(message.photos[0]["caption"]) <= 1024
    assert message.photos[0]["caption"] + "".join(message.replies[1:]) == report
    assert message.reply_objects[0].deleted


def test_background_harvest_splits_long_result(monkeypatch):
    events = [
        FlightEvent(
            id=f"event-{i}", tail=f"N{i:05d}AA", livery="x" * 300,
            type=EventType.ARRIVAL, target_airport="SFO",
            scheduled_time=datetime.now(timezone.utc), route_origin="SEA",
            route_destination="SFO", flight_number=f"XX{i:04d}",
        )
        for i in range(15)
    ]
    sent: list[str] = []
    async def send_message(_chat_id, text, **_kwargs):
        sent.append(text)
    application = SimpleNamespace(bot=SimpleNamespace(send_message=send_message))
    async def fake_harvest(_application):
        return HarvestResult(board_events=events)
    monkeypatch.setattr(bot.tracker, "run_harvest", fake_harvest)

    asyncio.run(bot._background_harvest(application, 1))

    assert len(sent) > 1
    assert all(telegram_length(part) <= TELEGRAM_LIMIT for part in sent)
    assert "XX0014" in "\n".join(sent)
