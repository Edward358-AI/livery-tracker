"""Post-conclusion verification: our verdicts are cross-checked with the source.

Direct observations (a watched touchdown, a double-confirmed diversion) stand
even when the source disagrees. Weak inferences — LOST, signal-loss guesses —
defer to whatever the source can prove.
"""

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock

import livery_tracker.tracker as tracker
from livery_tracker.config import Config
from livery_tracker.flights import EventState, EventType, FlightEvent, FlightStore
from livery_tracker.tracker import _reconcile_conclusion

NOW = datetime.now(timezone.utc).replace(second=0, microsecond=0)


def concluded(status: EventState, note: str = "", ev_type=EventType.ARRIVAL) -> FlightEvent:
    origin, dest = ("SEA", "SFO") if ev_type == EventType.ARRIVAL else ("SFO", "SEA")
    return FlightEvent(
        id="v", tail="N265AK", livery="", type=ev_type, target_airport="SFO",
        scheduled_time=NOW - timedelta(minutes=30), route_origin=origin,
        route_destination=dest, flight_number="AS1052", status=status,
        status_note=note,
    )


def make_row(status_text: str, *, key: str = "arrival", scheduled=None,
             estimated=None, real=None, live=False) -> dict:
    times = {"scheduled": {}, "estimated": {}, "real": {}}
    times["scheduled"][key] = int((scheduled or NOW - timedelta(minutes=30)).timestamp())
    if estimated is not None:
        times["estimated"][key] = int(estimated.timestamp())
    if real is not None:
        times["real"][key] = int(real.timestamp())
    return {
        "identification": {"number": {"default": "AS1052"}},
        "airport": {
            "origin": {"code": {"iata": "SEA"}},
            "destination": {"code": {"iata": "SFO"}},
        },
        "time": times,
        "status": {"live": live, "generic": {"status": {"text": status_text}}},
    }


# -- weak conclusions defer to the source --------------------------------------

def test_lost_arrival_adopts_a_landing_the_source_can_prove():
    ev = concluded(EventState.LOST)
    landed_at = NOW - timedelta(minutes=20)
    action = _reconcile_conclusion(ev, [make_row("landed", real=landed_at)], NOW)
    assert action == "corrected"
    assert ev.status == EventState.LANDED
    assert "per source" in ev.status_note


def test_lost_leg_is_revived_when_the_source_still_expects_it():
    ev = concluded(EventState.LOST)
    later = NOW + timedelta(hours=2)
    action = _reconcile_conclusion(ev, [make_row("estimated", estimated=later)], NOW)
    assert action == "revived"
    assert ev.status == EventState.WAITING_LIVE
    assert ev.scheduled_time == later


def test_lost_leg_adopts_a_cancellation():
    ev = concluded(EventState.LOST)
    action = _reconcile_conclusion(ev, [make_row("canceled")], NOW)
    assert action == "corrected"
    assert ev.status == EventState.CANCELLED


def test_lost_stands_when_the_source_knows_nothing_either():
    ev = concluded(EventState.LOST)
    stale = NOW - timedelta(minutes=30)
    action = _reconcile_conclusion(ev, [make_row("estimated", estimated=stale)], NOW)
    assert action == "confirmed"
    assert ev.status == EventState.LOST


def test_signal_loss_landing_gets_the_sources_real_time():
    ev = concluded(EventState.LANDED, "~9:41 PM (signal lost on approach)")
    landed_at = NOW - timedelta(minutes=18)
    action = _reconcile_conclusion(ev, [make_row("landed", real=landed_at)], NOW)
    assert action == "corrected"
    assert ev.status == EventState.LANDED
    assert "confirmed by source" in ev.status_note


def test_signal_loss_landing_defers_when_the_source_reports_a_diversion():
    ev = concluded(EventState.LANDED, "~9:41 PM (signal lost on approach)")
    action = _reconcile_conclusion(ev, [make_row("diverted")], NOW)
    assert action == "corrected"
    assert ev.status == EventState.DIVERTED


# -- strong conclusions stand --------------------------------------------------

def test_watched_landing_stands_when_the_source_lags():
    ev = concluded(EventState.LANDED, "9:41 PM")
    action = _reconcile_conclusion(
        ev, [make_row("estimated", estimated=NOW + timedelta(hours=1))], NOW
    )
    assert action == "confirmed"
    assert ev.status == EventState.LANDED


def test_watched_landing_keeps_its_verdict_over_a_source_diversion_claim():
    ev = concluded(EventState.LANDED, "9:41 PM")
    action = _reconcile_conclusion(ev, [make_row("diverted")], NOW)
    assert action == "annotated"
    assert ev.status == EventState.LANDED
    assert "source disagrees" in ev.status_note


def test_confirmed_diversion_stands_against_the_source():
    ev = concluded(EventState.DIVERTED, "on ground near SMF, 86 NM from SFO")
    action = _reconcile_conclusion(ev, [make_row("landed", real=NOW)], NOW)
    assert action == "annotated"
    assert ev.status == EventState.DIVERTED
    assert "source disagrees" in ev.status_note


def test_departure_confirmed_by_a_live_row():
    ev = concluded(EventState.DEPARTED, "9:12 PM", ev_type=EventType.DEPARTURE)
    row = make_row("estimated", key="departure", live=True)
    row["airport"] = {
        "origin": {"code": {"iata": "SFO"}},
        "destination": {"code": {"iata": "SEA"}},
    }
    action = _reconcile_conclusion(ev, [row], NOW)
    assert action == "confirmed"


def test_a_live_row_does_not_confirm_an_arrival():
    """"Live" means en route — the opposite of having landed."""
    ev = concluded(EventState.LOST)
    later = NOW + timedelta(minutes=40)
    action = _reconcile_conclusion(
        ev, [make_row("estimated", estimated=later, live=True)], NOW
    )
    assert action == "revived", "still flying per source: reopen, don't confirm"


def test_no_matching_row_changes_nothing():
    ev = concluded(EventState.LOST)
    assert _reconcile_conclusion(ev, [], NOW) is None
    assert ev.status == EventState.LOST


# -- the job wiring ------------------------------------------------------------

class FakeDigest:
    async def refresh(self):
        pass


def test_job_corrects_a_lost_leg_and_records_history(monkeypatch):
    store = FlightStore()
    ev = concluded(EventState.LOST)
    store.upsert(ev)
    app = SimpleNamespace(
        bot_data={"store": store, "config": Config(), "digest": FakeDigest()},
        job_queue=MagicMock(),
    )
    app.job_queue.get_jobs_by_name.return_value = []
    landed_at = NOW - timedelta(minutes=20)
    monkeypatch.setattr(
        tracker.schedule_provider, "fetch_flight_list",
        lambda q, fetch_by="reg": [make_row("landed", real=landed_at)],
    )

    ctx = SimpleNamespace(application=app, job=SimpleNamespace(data=ev.id))
    asyncio.run(tracker.job_verify_conclusion(ctx))

    assert store.get("v").status == EventState.LANDED
    from livery_tracker.config import data_dir
    lines = (data_dir() / "history.jsonl").read_text(encoding="utf-8").splitlines()
    assert any('"LANDED"' in line for line in lines)
