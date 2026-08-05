"""Bulk import from a .txt of registrations, and the matching /export."""

import asyncio
from types import SimpleNamespace

from livery_tracker import bot
from livery_tracker.config import Config
from livery_tracker.digest import SAFE_LIMIT, telegram_length


class FakeMessage:
    def __init__(self):
        self.replies: list[str] = []

    async def reply_text(self, text: str, **_kwargs):
        self.replies.append(text)
        return SimpleNamespace()


class FakeBot:
    def __init__(self):
        self.sent: list[str] = []

    async def send_message(self, chat_id, text, **_kwargs):
        self.sent.append(text)


def make_config(watching=()) -> Config:
    config = Config()
    config.target_airports = {"SFO": {"icao": "KSFO", "name": "SF", "lat": 37.6, "lon": -122.4}}
    for tail in watching:
        config.watchlist[tail] = {"airline": "X", "model": "Y", "livery": ""}
    return config


# -- parsing --------------------------------------------------------------------

def test_parse_import_lines_dedupes_and_skips_junk():
    text = """
# my spotting list
n537as
N537AS
VT-ALN
this is not a registration
B2002

"""
    regs, ignored = bot._parse_import_lines(text)
    assert regs == ["N537AS", "VT-ALN", "B2002"]
    assert ignored == 1, "prose lines are counted, comments and blanks are free"


def test_export_output_reimports_cleanly():
    regs, ignored = bot._parse_import_lines("# 2 registrations exported 2026-08-05\nN1\nN2\n")
    assert regs == ["N1", "N2"] and ignored == 0


# -- resolution verdicts --------------------------------------------------------

def test_import_resolved_requires_some_evidence():
    assert not bot._import_resolved(
        {"airline": "Unknown airline", "model": "Unknown type", "thumbnail": ""}
    )
    assert bot._import_resolved(
        {"airline": "Alaska Airlines", "model": "Unknown type", "thumbnail": ""}
    )
    assert bot._import_resolved(
        {"airline": "Unknown airline", "model": "Unknown type", "thumbnail": "http://x"}
    )


def test_import_desc_prefers_the_type_code():
    info = {"type_code": "B38M", "model": "Boeing 737 MAX 8",
            "airline": "Southwest Airlines", "livery": "Liberty One"}
    assert bot._import_desc(info) == 'B38M, Southwest Airlines, "Liberty One"'
    info["type_code"] = ""
    assert bot._import_desc(info).startswith("Boeing 737 MAX 8")


# -- the sequential import ------------------------------------------------------

def import_app(config):
    app = SimpleNamespace(bot=FakeBot(), bot_data={"config": config})
    return app


def test_background_import_reports_each_tail(monkeypatch):
    config = make_config(watching=["N100"])
    app = import_app(config)
    monkeypatch.setattr(bot, "IMPORT_SPACING_S", 0)

    def fake_resolve(tail):
        if tail == "N101":
            return {"airline": "Unknown airline", "model": "Unknown type",
                    "livery": "", "thumbnail": "", "type_code": ""}
        return {"airline": "ABC Airlines", "model": "Boeing 737 MAX 8",
                "livery": "", "thumbnail": "", "type_code": "B38M"}

    async def fake_harvest(application, tail):
        return (["leg", "leg"] if tail == "N102" else [], True)

    monkeypatch.setattr(bot, "resolve_aircraft", fake_resolve)
    monkeypatch.setattr(bot.tracker, "harvest_single", fake_harvest)
    monkeypatch.setattr(bot.aircraft_db, "record_profile", lambda tail, info: None)

    asyncio.run(bot._background_import(app, 1, ["N100", "N101", "N102", "N103"]))

    sent = app.bot.sent
    assert "➖ N100 is already on the watchlist." in sent[0]
    assert "❌ Could not resolve N101 — not added." in sent[1]
    assert "✅ Now watching N102 (B38M, ABC Airlines). 2 leg(s) found" in sent[2]
    assert "✅ Now watching N103 (B38M, ABC Airlines). No flights at your airports" in sent[3]
    assert "1 already watched" in sent[4] and "2 added" in sent[4] and "1 unresolved" in sent[4]
    assert "N102" in config.watchlist and "N103" in config.watchlist
    assert "N101" not in config.watchlist, "unresolved entries are skipped"


def test_background_import_stops_at_the_watchlist_cap(monkeypatch):
    config = make_config()
    app = import_app(config)
    monkeypatch.setattr(bot, "IMPORT_SPACING_S", 0)
    monkeypatch.setattr(bot, "MAX_WATCHLIST", 1)
    monkeypatch.setattr(
        bot, "resolve_aircraft",
        lambda tail: {"airline": "ABC", "model": "M", "livery": "",
                      "thumbnail": "", "type_code": "B738"},
    )

    async def fake_harvest(application, tail):
        return ([], True)

    monkeypatch.setattr(bot.tracker, "harvest_single", fake_harvest)
    monkeypatch.setattr(bot.aircraft_db, "record_profile", lambda tail, info: None)

    asyncio.run(bot._background_import(app, 1, ["N200", "N201", "N202"]))

    assert len(config.watchlist) == 1
    assert any("watchlist is full" in line for line in app.bot.sent)
    assert any("2 registration(s) not imported" in line for line in app.bot.sent)


# -- /export --------------------------------------------------------------------

def export_context(config):
    return SimpleNamespace(application=SimpleNamespace(bot_data={"config": config}))


def test_export_lists_registrations_one_per_line():
    config = make_config(watching=["N300", "N100", "VT-ALN"])
    message = FakeMessage()
    update = SimpleNamespace(message=message)

    asyncio.run(bot.cmd_export(update, export_context(config)))

    assert len(message.replies) == 1
    lines = message.replies[0].splitlines()
    assert lines[0].startswith("# 3 registrations exported")
    assert lines[1:] == ["N100", "N300", "VT-ALN"], "sorted, plain, import-ready"


def test_export_splits_a_huge_watchlist_safely():
    config = make_config(watching=[f"N{i:05d}AB" for i in range(2500)])
    message = FakeMessage()
    update = SimpleNamespace(message=message)

    asyncio.run(bot.cmd_export(update, export_context(config)))

    assert len(message.replies) > 1, "must split rather than exceed the limit"
    for part in message.replies:
        assert telegram_length(part) <= SAFE_LIMIT
    rejoined = "\n".join(message.replies)
    assert "N02499AB" in rejoined and "N00000AB" in rejoined
