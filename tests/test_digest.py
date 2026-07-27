import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

from livery_tracker.config import Config, data_dir
from livery_tracker.digest import DigestManager, fmt_local, render_digest
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore


def make_event(idx: str, ev_type: EventType, status: EventState, **overrides) -> FlightEvent:
    when = datetime(2026, 7, 26, 22, 45, tzinfo=timezone.utc)
    defaults = dict(
        id=idx,
        tail="N265AK",
        livery="More to Love",
        type=ev_type,
        target_airport="SFO",
        scheduled_time=when,
        route_origin="SEA",
        route_destination="SFO",
        flight_number="AS1234",
        status=status,
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


def make_config() -> Config:
    config = Config()
    config.target_airports["SFO"] = {"icao": "KSFO", "name": "San Francisco", "lat": 37.6, "lon": -122.4}
    config.watchlist["N265AK"] = {"airline": "Alaska", "model": "B739", "livery": "More to Love"}
    return config


def test_fmt_local_uses_abbreviated_zone():
    stamp = fmt_local(datetime(2026, 7, 26, 22, 45, tzinfo=timezone.utc))
    tz = stamp.rsplit(" ", 1)[-1]
    assert " " not in tz and 2 <= len(tz) <= 6
    assert not stamp.startswith("0")


def test_render_empty_digest():
    text = render_digest(FlightStore(), make_config())
    assert "LIVERY DIGEST" in text
    assert "No watched aircraft scheduled" in text
    assert "Watching 1 aircraft at SFO" in text


def test_render_sections_and_states():
    store = FlightStore()
    store.upsert(make_event("a1", EventType.ARRIVAL, EventState.WAITING_2H))
    live = make_event("a2", EventType.ARRIVAL, EventState.LIVE, tail="N560AS", livery="Salmon")
    live.last_telemetry = {"lat": 37.0, "lon": -122.0, "alt": 12400, "gs": 310.0, "dist_nm": 48.2}
    store.upsert(live)
    landed = make_event("a3", EventType.ARRIVAL, EventState.LANDED, tail="N711HK", livery="")
    landed.status_note = "4:06 PM PDT"
    store.upsert(landed)
    store.upsert(make_event("d1", EventType.DEPARTURE, EventState.WAITING_LIVE,
                            route_origin="SFO", route_destination="RDU"))

    text = render_digest(store, make_config())
    assert "🛬 <b>Arrivals</b>" in text
    assert "🛫 <b>Departures</b>" in text
    assert "🟡 <b>N265AK</b>" in text                       # scheduled arrival
    assert "12,400 ft · 310 kts · 48 NM out" in text        # live telemetry line
    assert "✅ <b>N711HK</b> — SEA➔SFO AS1234 @ SFO, landed 4:06 PM PDT" in text
    assert "SFO➔RDU" in text                                # departure leg present
    assert "Updated" in text


def test_render_lost_state():
    store = FlightStore()
    store.upsert(make_event("x", EventType.ARRIVAL, EventState.LOST))
    text = render_digest(store, make_config())
    assert "⚠️" in text and "tracking lost" in text


def test_flight_between_two_watched_airports_renders_as_one_line():
    dep_time = datetime(2026, 7, 26, 16, 0, tzinfo=timezone.utc)
    arr_time = datetime(2026, 7, 26, 17, 30, tzinfo=timezone.utc)
    store = FlightStore()
    dep = make_event("dep", EventType.DEPARTURE, EventState.DEPARTED,
                     target_airport="SFO", route_origin="SFO", route_destination="LAX",
                     flight_number="AS1052", scheduled_time=dep_time)
    dep.status_note = "9:04 AM PDT"
    arr = make_event("arr", EventType.ARRIVAL, EventState.LIVE,
                     target_airport="LAX", route_origin="SFO", route_destination="LAX",
                     flight_number="AS1052", scheduled_time=arr_time)
    arr.last_telemetry = {"lat": 34.5, "lon": -119.0, "alt": 21000, "gs": 415.0, "dist_nm": 62.0}
    store.upsert(dep)
    store.upsert(arr)

    text = render_digest(store, make_config())
    assert "🔁 <b>Between your airports</b>" in text
    assert "🚨 <b>N265AK</b>" in text                    # arrival phase is the live one
    assert "departed 9:04 AM PDT → 21,000 ft · 415 kts · 62 NM out" in text
    assert text.count("N265AK") == 1                     # merged: not repeated in Arr/Dep sections
    assert "🛬 <b>Arrivals</b>" not in text
    assert "🛫 <b>Departures</b>" not in text


def test_unrelated_legs_do_not_merge():
    store = FlightStore()
    store.upsert(make_event("a", EventType.ARRIVAL, EventState.WAITING_2H))  # SEA->SFO
    store.upsert(make_event("d", EventType.DEPARTURE, EventState.WAITING_2H,
                            route_origin="SFO", route_destination="RDU"))
    text = render_digest(store, make_config())
    assert "🔁" not in text
    assert "🛬 <b>Arrivals</b>" in text and "🛫 <b>Departures</b>" in text


class FakeBot:
    def __init__(self):
        self.sent, self.edited, self.deleted = [], [], []

    async def initialize(self):
        pass

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        return SimpleNamespace(message_id=100 + len(self.sent))

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edited.append((message_id, text))

    async def delete_message(self, chat_id, message_id):
        self.deleted.append(message_id)


def test_manager_deletes_yesterdays_digest_then_edits_todays():
    bot = FakeBot()
    manager = DigestManager(bot, chat_id=1, store=FlightStore(), config=make_config())

    # Pretend yesterday's digest (id 55) is still up.
    (data_dir() / "digest_state.json").write_text(
        json.dumps({"date": "2020-01-01", "message_id": 55}), encoding="utf-8"
    )

    asyncio.run(manager.refresh())          # rollover: delete old, send new
    assert bot.deleted == [55]
    assert len(bot.sent) == 1 and bot.edited == []

    asyncio.run(manager.refresh())          # same day: edit in place, no new sends
    assert len(bot.sent) == 1
    assert len(bot.edited) == 1 and bot.edited[0][0] == 101
    assert bot.deleted == [55]
