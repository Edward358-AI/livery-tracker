"""Tests for owner-only maintenance controls."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

from livery_tracker import bot
from livery_tracker.flights import EventType, FlightEvent


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs) -> None:
        self.replies.append(text)


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
