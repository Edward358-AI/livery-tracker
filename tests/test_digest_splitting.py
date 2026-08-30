"""The digest must stay sendable no matter how large the watchlist grows.

Telegram rejects a message over 4096 UTF-16 units. Before splitting existed,
an oversized digest failed to edit, failed again on the resend fallback, and
the error was swallowed — the digest silently stopped updating.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from livery_tracker.config import Config, data_dir
from livery_tracker.digest import (
    SAFE_LIMIT,
    TELEGRAM_LIMIT,
    DigestManager,
    render_digest,
    render_digest_parts,
    telegram_length,
)
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

NOW = datetime(2026, 7, 27, 20, 0, tzinfo=timezone.utc)


def make_config(count: int = 5, group_by: str = "type") -> Config:
    config = Config(digest_group_by=group_by)
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6, "lon": -122.4},
        "SJC": {"icao": "KSJC", "name": "San Jose", "lat": 37.4, "lon": -121.9},
    }
    config.watchlist = {f"N{i:03d}AA": {"airline": f"Airline {i % 7}", "livery": "Some Livery Name"}
                        for i in range(count)}
    return config


def store_with(n_legs: int) -> FlightStore:
    store = FlightStore()
    for i in range(n_legs):
        ev_type = EventType.ARRIVAL if i % 2 else EventType.DEPARTURE
        store.upsert(FlightEvent(
            id=f"leg{i}", tail=f"N{i:03d}AA", livery="Some Livery Name",
            type=ev_type, target_airport="SFO" if i % 2 else "SJC",
            scheduled_time=NOW + timedelta(minutes=i * 7),
            route_origin="SEA", route_destination="SFO",
            flight_number=f"AS{1000 + i}", status=EventState.WAITING_2H,
        ))
    return store


# -- length accounting ---------------------------------------------------------

def test_telegram_length_counts_emoji_as_two():
    assert telegram_length("abc") == 3
    assert telegram_length("✈️") > 1          # emoji cost more than one unit
    assert telegram_length("🛬🛫") == 4


# -- splitting behaviour -------------------------------------------------------

def test_small_digest_stays_a_single_message():
    parts = render_digest_parts(store_with(6), make_config(6))
    assert len(parts) == 1
    assert "continued below" not in parts[0]
    assert parts[0] == render_digest(store_with(6), make_config(6))


def test_large_digest_splits_and_every_part_fits():
    store, config = store_with(120), make_config(120)
    parts = render_digest_parts(store, config)

    assert len(parts) > 1, "a 120-leg digest must not be one message"
    for part in parts:
        assert telegram_length(part) <= TELEGRAM_LIMIT, "a part exceeds Telegram's limit"
        assert telegram_length(part) <= SAFE_LIMIT + 200


def test_split_parts_are_labelled_and_ordered():
    parts = render_digest_parts(store_with(120), make_config(120))
    assert "LIVERY DIGEST" in parts[0]
    assert "continued below" in parts[0]
    for part in parts[1:]:
        assert "(cont.)" in part
    assert "Updated" in parts[-1]           # footer only on the last part
    assert "continued below" not in parts[-1]


def test_no_legs_are_dropped_when_splitting():
    store, config = store_with(120), make_config(120)
    combined = "\n".join(render_digest_parts(store, config))
    for event in store.events.values():
        assert event.flight_number in combined, f"{event.flight_number} lost in the split"


def test_splitting_works_in_every_grouping_mode():
    for mode in ("type", "airport", "airline"):
        store, config = store_with(120), make_config(120, group_by=mode)
        parts = render_digest_parts(store, config)
        for part in parts:
            assert telegram_length(part) <= TELEGRAM_LIMIT, f"{mode} mode overflowed"
        combined = "\n".join(parts)
        for event in store.events.values():
            assert event.flight_number in combined


def test_limit_none_disables_splitting():
    parts = render_digest_parts(store_with(120), make_config(120), limit=None)
    assert len(parts) == 1


# -- manager behaviour ---------------------------------------------------------

class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []

    async def initialize(self):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=500 + len(self.sent))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append((message_id, text))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def test_multi_part_digest_is_edited_in_place_on_later_refreshes():
    bot = FakeBot()
    store, config = store_with(120), make_config(120)
    manager = DigestManager(bot, chat_id=1, store=store, config=config)

    asyncio.run(manager.refresh())
    sent_count = len(bot.sent)
    assert sent_count > 1

    asyncio.run(manager.refresh())
    assert len(bot.sent) == sent_count          # nothing re-sent
    assert len(bot.edited) == sent_count        # each part edited instead
    assert bot.deleted == []


def test_changing_part_count_rebuilds_the_digest():
    bot = FakeBot()
    config = make_config(120)
    small = store_with(6)
    manager = DigestManager(bot, chat_id=1, store=small, config=config)
    asyncio.run(manager.refresh())
    assert len(bot.sent) == 1
    first_id = 501

    # The watchlist balloons: one message can no longer hold the digest.
    manager.store = store_with(120)
    asyncio.run(manager.refresh())
    assert first_id in bot.deleted, "the stale single message should be removed"
    assert len(bot.sent) > 1


def test_legacy_single_id_state_is_understood():
    bot = FakeBot()
    store, config = store_with(6), make_config(6)
    today = datetime.now().astimezone().date().isoformat()
    (data_dir() / "digest_state.json").write_text(
        json.dumps({"date": today, "message_id": 42}), encoding="utf-8"
    )
    manager = DigestManager(bot, chat_id=1, store=store, config=config)
    asyncio.run(manager.refresh())

    assert bot.edited and bot.edited[0][0] == 42   # edited, not re-sent
    assert bot.sent == []


def test_failed_deletes_are_remembered_and_retried():
    """A delete that fails transiently must not orphan the message forever —
    the id is kept as a stray and swept on a later refresh, while today's
    digest keeps posting and editing untouched."""
    class FlakyDeleteBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.fail_deletes = True

        async def delete_message(self, chat_id, message_id):
            if self.fail_deletes:
                raise TimeoutError("network down")
            await super().delete_message(chat_id, message_id)

    bot = FlakyDeleteBot()
    yesterday = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    (data_dir() / "digest_state.json").write_text(
        json.dumps({"date": yesterday, "message_ids": [40, 41]}), encoding="utf-8"
    )
    manager = DigestManager(bot, chat_id=1, store=store_with(6), config=make_config(6))

    asyncio.run(manager.refresh())              # day change; deletes fail
    assert bot.sent, "today's digest must still go out"
    state = json.loads((data_dir() / "digest_state.json").read_text(encoding="utf-8"))
    assert state["stale_ids"] == [40, 41]
    assert 40 not in state["message_ids"]

    bot.fail_deletes = False
    asyncio.run(manager.refresh())              # later refresh sweeps the strays
    assert bot.deleted == [40, 41]
    state = json.loads((data_dir() / "digest_state.json").read_text(encoding="utf-8"))
    assert "stale_ids" not in state
    assert state["message_ids"] and not set(state["message_ids"]) & set(bot.deleted), \
        "today's messages must never be swept as strays"


def test_undeletable_messages_are_given_up_not_retried_forever():
    """Telegram refuses deletes past 48h — such ids are dropped (with a
    warning), not carried as strays for eternity."""
    from telegram.error import BadRequest

    class WalledBot(FakeBot):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        async def delete_message(self, chat_id, message_id):
            self.attempts += 1
            raise BadRequest("Message can't be deleted")

    bot = WalledBot()
    yesterday = (datetime.now().astimezone().date() - timedelta(days=1)).isoformat()
    (data_dir() / "digest_state.json").write_text(
        json.dumps({"date": yesterday, "message_ids": [40, 41]}), encoding="utf-8"
    )
    manager = DigestManager(bot, chat_id=1, store=store_with(6), config=make_config(6))

    asyncio.run(manager.refresh())
    assert bot.attempts == 2                    # one try per id
    state = json.loads((data_dir() / "digest_state.json").read_text(encoding="utf-8"))
    assert "stale_ids" not in state             # given up, not queued

    asyncio.run(manager.refresh())
    assert bot.attempts == 2                    # and never tried again


def test_live_leg_without_contact_still_shows_its_scheduled_time():
    """A dark live leg must show its ETD/ETA and say why it has no figures —
    a bare "live tracking active" hid the one thing the reader wanted (the
    N24988 gate-delay case)."""
    from datetime import datetime, timedelta, timezone

    from livery_tracker.digest import format_leg
    from livery_tracker.flights import EventState, EventType, FlightEvent

    ev = FlightEvent(
        id="x", tail="N24988", livery="The Future Is SAF",
        type=EventType.DEPARTURE, target_airport="SFO",
        scheduled_time=datetime.now(timezone.utc) + timedelta(minutes=30),
        route_origin="SFO", route_destination="ICN", flight_number="UA893",
        status=EventState.LIVE, status_note="delayed 240m",
    )
    line = format_leg(ev)
    assert "ETD" in line
    assert "no ADS-B contact yet" in line
    assert "delayed 240m" in line
