"""/rebuild: discard schedule state and re-harvest, keeping everything else."""

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import livery_tracker.adsb as adsb
import livery_tracker.schedule_provider as sp
import livery_tracker.tracker as tracker
from livery_tracker.config import Config, data_dir
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore

NOW = datetime.now(timezone.utc) + timedelta(hours=2)


class FakeDigest:
    def __init__(self):
        self.refreshes = 0

    async def refresh(self):
        self.refreshes += 1


class FakeApp:
    def __init__(self, store, config):
        self.bot_data = {"store": store, "config": config, "digest": FakeDigest(),
                         "chat_id": 1}
        self.job_queue = MagicMock()
        self.job_queue.get_jobs_by_name.return_value = []
        self.bot = MagicMock()


def make_config() -> Config:
    config = Config()
    config.target_airports = {
        "SFO": {"icao": "KSFO", "name": "San Francisco", "lat": 37.6, "lon": -122.4}
    }
    config.watchlist = {"N265AK": {"airline": "Alaska", "livery": "Retro"}}
    return config


def stale_leg(idx: str, status=EventState.WAITING_2H) -> FlightEvent:
    return FlightEvent(
        id=idx, tail="N265AK", livery="Retro", type=EventType.ARRIVAL,
        target_airport="SFO", scheduled_time=NOW, route_origin="SEA",
        route_destination="SFO", flight_number="AS1234", status=status,
    )


# -- cache clearing ------------------------------------------------------------

def test_clear_caches_drops_disk_and_memo_entries():
    sp.cache_rows("N265AK", [{"x": 1}])
    sp.cache_rows("N8658A", [{"x": 2}])
    sp._LIST_MEMO.set(("N265AK", "reg"), [{"x": 1}])
    assert sp.load_cached_rows("N265AK") is not None

    dropped = sp.clear_caches()

    assert dropped == 2
    assert sp.load_cached_rows("N265AK") is None
    assert not (data_dir() / "schedule_cache.json").exists()
    from livery_tracker.throttle import MISS
    assert sp._LIST_MEMO.get(("N265AK", "reg")) is MISS


def test_clear_caches_is_safe_when_nothing_is_cached():
    sp.clear_caches()
    assert sp.clear_caches() == 0


def test_adsb_clear_caches_empties_both_memos():
    adsb._TELEMETRY_MEMO.set("N265AK", "position")
    adsb._ROUTE_MEMO.set("ASA1234", ("SEA", "SFO", "AS1234"))
    adsb.clear_caches()
    from livery_tracker.throttle import MISS
    assert adsb._TELEMETRY_MEMO.get("N265AK") is MISS
    assert adsb._ROUTE_MEMO.get("ASA1234") is MISS


# -- rebuild -------------------------------------------------------------------

def rebuild_with(monkeypatch, store, config, harvested=None):
    app = FakeApp(store, config)
    monkeypatch.setattr(
        sp, "harvest_airport_boards", lambda cfg, now=None: (harvested or [], True)
    )
    monkeypatch.setattr(
        sp, "harvest_tail", lambda tail, livery, cfg: ([], True)
    )
    monkeypatch.setattr(tracker, "heal_unknown_metadata", _noop_heal)
    return app, asyncio.run(tracker.rebuild_schedule(app))


async def _noop_heal(config):
    return 0


def test_rebuild_discards_derived_legs_but_keeps_observed_ones(monkeypatch):
    """Rebuild rebuilds the future: derived verdicts (swapped/lost/cancelled)
    and pending legs are re-derived, but a conclusion we directly observed
    (landed/departed/diverted) is history, not schedule state."""
    store, config = FlightStore(), make_config()
    store.upsert(stale_leg("observed-diverted", EventState.DIVERTED))
    store.upsert(stale_leg("bad-swapped", EventState.SWAPPED))
    store.upsert(stale_leg("pending"))
    sp.cache_rows("N265AK", [{"stale": True}])

    fresh = stale_leg("fresh")
    fresh.flight_number = "AS2222"  # a different flight — the kept observed
    fresh.scheduled_time = NOW + timedelta(hours=4)  # leg must not block it
    app, result = rebuild_with(monkeypatch, store, config, harvested=[fresh])

    assert result.discarded_legs == 2
    assert result.skipped is False
    assert set(store.events) == {"observed-diverted", "fresh"}, \
        "derived junk goes, observed history stays"
    assert sp.load_cached_rows("N265AK") is None


def test_rebuild_keeps_watchlist_airports_and_layout(monkeypatch):
    """A rebuild is about schedule state, not the user's configuration."""
    store, config = FlightStore(), make_config()
    config.digest_group_by = "airline"
    store.upsert(stale_leg("x"))

    app, _ = rebuild_with(monkeypatch, store, config)

    assert config.watchlist == {"N265AK": {"airline": "Alaska", "livery": "Retro"}}
    assert "SFO" in config.target_airports
    assert config.digest_group_by == "airline"


def test_rebuild_cancels_jobs_for_discarded_legs(monkeypatch):
    store, config = FlightStore(), make_config()
    store.upsert(stale_leg("pending"))
    app, _ = rebuild_with(monkeypatch, store, config)
    assert app.job_queue.get_jobs_by_name.called, "old timers must be torn down"


def test_rebuild_is_refused_while_a_harvest_runs():
    async def scenario():
        async with tracker._harvest_lock:
            return await tracker.rebuild_schedule(
                FakeApp(FlightStore(), make_config())
            )

    result = asyncio.run(scenario())
    assert result.skipped is True
    assert result.discarded_legs == 0


def test_rebuild_leaves_aircraft_dossier_cache_alone(monkeypatch):
    """Build years and photos are expensive to rebuild and are not schedules."""
    path = data_dir() / "aircraft_cache.json"
    path.write_text(json.dumps({"N265AK": {"year": 2016}}), encoding="utf-8")

    store, config = FlightStore(), make_config()
    rebuild_with(monkeypatch, store, config)

    assert json.loads(path.read_text(encoding="utf-8"))["N265AK"]["year"] == 2016
