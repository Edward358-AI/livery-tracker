import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Point LT_DATA_DIR at a temp dir so tests never touch real state."""
    monkeypatch.setenv("LT_DATA_DIR", str(tmp_path / "data"))
    yield tmp_path / "data"
