"""The flight journal: every real change recorded with its cause and evidence.

Coverage is automatic (the store diffs at upsert/remove), labeling is by
ContextVar, routine LIVE telemetry churn is invisible, and a restart's
rehydration journals nothing.
"""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.adsb import Telemetry
from livery_tracker.config import Config, data_dir
from livery_tracker.flights import (
    EventState,
    EventType,
    FlightEvent,
    FlightStore,
    rotate_journal,
    set_journal_context,
)
from livery_tracker.schedule_provider import LegRefresh

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def journal_rows() -> list[dict]:
    path = data_dir() / "journal.jsonl"
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def make_leg(**overrides) -> FlightEvent:
    defaults = dict(
        id="leg", tail="N265AK", livery="", type=EventType.DEPARTURE,
        target_airport="SFO", scheduled_time=NOW + timedelta(hours=2),
        route_origin="SFO", route_destination="SEA", flight_number="AS656",
        status=EventState.WAITING_2H,
    )
    defaults.update(overrides)
    return FlightEvent(**defaults)


# -- store-level diffing --------------------------------------------------------

def test_first_upsert_journals_a_created_row():
    set_journal_context("harvest")
    store = FlightStore()
    store.upsert(make_leg())

    rows = journal_rows()
    assert len(rows) == 1
    assert rows[0]["change"] == {"created": True, "status": "WAITING_2H"}
    assert rows[0]["trigger"] == "harvest"
    assert rows[0]["tail"] == "N265AK" and rows[0]["flight"] == "AS656"
    assert "ts" in rows[0]


def test_status_and_time_changes_journal_old_and_new():
    store = FlightStore()
    leg = make_leg()
    store.upsert(leg)

    leg.status = EventState.LIVE
    leg.scheduled_time = leg.scheduled_time + timedelta(minutes=30)
    set_journal_context("live_start")
    store.upsert(leg)

    row = journal_rows()[-1]
    assert row["change"]["status"] == ["WAITING_2H", "LIVE"]
    assert "time" in row["change"]
    assert row["trigger"] == "live_start"


def test_telemetry_only_updates_journal_nothing():
    store = FlightStore()
    leg = make_leg(status=EventState.LIVE)
    store.upsert(leg)
    before = len(journal_rows())

    leg.last_telemetry = {"lat": 37.0, "lon": -122.0, "alt": 30000}
    store.upsert(leg)

    assert len(journal_rows()) == before


def test_live_note_churn_is_suppressed_but_meaningful_notes_journal():
    store = FlightStore()
    live = make_leg(status=EventState.LIVE)
    store.upsert(live)
    waiting = make_leg(id="w", status=EventState.WAITING_LIVE, flight_number="AS657")
    store.upsert(waiting)
    before = len(journal_rows())

    live.status_note = "running 12m late — 40 NM out"
    store.upsert(live)
    assert len(journal_rows()) == before, "per-poll lateness ticks must not journal"

    waiting.status_note = "delayed 30m"
    store.upsert(waiting)
    row = journal_rows()[-1]
    assert row["change"]["note"] == ["", "delayed 30m"]


def test_removal_journals_with_final_status():
    store = FlightStore()
    store.upsert(make_leg(status=EventState.SWAPPED))
    set_journal_context("command.dropflight")

    store.remove_where(lambda ev: ev.id == "leg")

    row = journal_rows()[-1]
    assert row["change"] == {"removed": True, "status": "SWAPPED"}
    assert row["trigger"] == "command.dropflight"


def test_rehydration_journals_nothing():
    store = FlightStore()
    store.upsert(make_leg())
    before = len(journal_rows())

    fresh = FlightStore()  # simulates a restart reading flights_today.json
    assert len(journal_rows()) == before
    # ...and the reloaded store diffs against the loaded state, not zero:
    leg = fresh.get("leg")
    leg.status = EventState.WAITING_LIVE
    fresh.upsert(leg)
    assert journal_rows()[-1]["change"]["status"] == ["WAITING_2H", "WAITING_LIVE"]


# -- rotation -------------------------------------------------------------------

def test_rotation_archives_by_date_and_prunes_old_files():
    store = FlightStore()
    store.upsert(make_leg())
    stale = data_dir() / "journal-20200101.jsonl"
    stale.write_text("{}\n", encoding="utf-8")

    rotate_journal(now=NOW)

    assert not (data_dir() / "journal.jsonl").exists()
    stamp = (NOW - timedelta(days=1)).strftime("%Y%m%d")
    archived = data_dir() / f"journal-{stamp}.jsonl"
    assert archived.exists() and "created" in archived.read_text(encoding="utf-8")
    assert not stale.exists(), "archives older than the keep window are pruned"


# -- end-to-end accuracy through real jobs --------------------------------------

class FakeDigest:
    async def refresh(self):
        pass


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest()}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []


def sfo_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6198, "lon": -122.3748},
    }
    config.watchlist = {"N265AK": {"airline": "Alaska", "livery": ""}}
    return config


def context_for(app, event):
    return SimpleNamespace(
        application=app,
        job=SimpleNamespace(data=event.id, schedule_removal=MagicMock()),
    )


def test_board_discovery_writes_under_its_own_tag(monkeypatch):
    """Job contexts leak between jobs more than designed for: without its own
    tag, board discovery's writes were journaled under whatever label another
    job left behind (observed as a poll-tagged schedule change)."""
    store = FlightStore()
    app = FakeApp(store, sfo_config())
    set_journal_context("poll.detection")  # a stale tag left by another job
    monkeypatch.setattr(
        tracker.schedule_provider, "harvest_airport_boards",
        lambda cfg, now=None: ([make_leg()], True),
    )

    asyncio.run(tracker.run_board_discovery(app))

    row = journal_rows()[-1]
    assert row["change"]["created"] is True
    assert row["trigger"] == "board_discovery"


def test_sync_withdrawal_is_journaled_with_trigger_and_evidence(monkeypatch):
    store = FlightStore()
    store.upsert(make_leg())
    app = FakeApp(store, sfo_config())
    monkeypatch.setattr(
        tracker.schedule_provider, "fetch_flight_list", lambda q, fetch_by="reg": []
    )
    monkeypatch.setattr(tracker.schedule_provider, "cache_rows", lambda reg, r: None)
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, ev: LegRefresh(None, swapped=True),
    )
    monkeypatch.setattr(tracker, "fetch_telemetry", lambda reg: None)

    asyncio.run(tracker.run_schedule_sync(app))

    row = journal_rows()[-1]
    assert row["change"]["status"] == ["WAITING_2H", "SWAPPED"]
    assert row["trigger"] == "hourly_sync.withdrawn"
    assert row["evidence"]["refresh"]["swapped"] is True


def test_poll_mismatch_hold_is_journaled_with_the_observed_callsign(monkeypatch):
    store = FlightStore()
    leg = make_leg(status=EventState.LIVE, scheduled_time=NOW + timedelta(minutes=50))
    store.upsert(leg)
    app = FakeApp(store, sfo_config())
    monkeypatch.setattr(
        tracker.schedule_provider, "refresh_leg_time",
        lambda reg, event: LegRefresh(event.scheduled_time),
    )
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=37.6198, lon=-122.3748, alt_ft=0, on_ground=True,
                              gs_kts=0.0, baro_rate=None, callsign="ASA725", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    row = journal_rows()[-1]
    assert row["change"]["status"] == ["LIVE", "TURNAROUND_DELAY"]
    assert row["trigger"] == "poll.callsign_mismatch"
    assert row["evidence"]["telemetry"]["callsign"] == "ASA725"
    assert row["evidence"]["telemetry"]["on_ground"] is True


def test_departure_detection_is_journaled_with_telemetry(monkeypatch):
    store = FlightStore()
    leg = make_leg(status=EventState.LIVE, scheduled_time=NOW - timedelta(minutes=10))
    store.upsert(leg)
    app = FakeApp(store, sfo_config())
    monkeypatch.setattr(
        tracker, "fetch_telemetry",
        lambda reg: Telemetry(lat=37.9, lon=-122.6, alt_ft=12_000, on_ground=False,
                              gs_kts=320.0, baro_rate=1500, callsign="ASA656", source="test"),
    )

    asyncio.run(tracker.job_poll(context_for(app, leg)))

    row = journal_rows()[-1]
    assert row["change"]["status"] == ["LIVE", "DEPARTED"]
    assert row["trigger"] == "poll.detection"
    assert row["evidence"]["telemetry"]["alt_ft"] == 12_000
