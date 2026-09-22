import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point LT_DATA_DIR at a temp dir so tests never touch real state."""
    monkeypatch.setenv("LT_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"


@pytest.fixture(autouse=True)
def no_network_lookups(monkeypatch):
    """Keep tests off the network for the incidental lookups.

    The airport index starts empty (lookup/nearest return None and callers
    fall back gracefully) instead of downloading the OurAirports CSV, and the
    post-landing FR24 touchdown fetch returns nothing. A test exercising
    either directly re-patches what it needs.
    """
    from livery_tracker import adsb, airports

    monkeypatch.setattr(airports, "_index", {})
    monkeypatch.setattr(adsb, "fr24_touchdown_time", lambda reg: None)
