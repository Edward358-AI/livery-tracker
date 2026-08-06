"""Configuration: .env credentials plus config_and_watch.json (airports + watchlist)."""

from __future__ import annotations

import json
import os
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = PROJECT_ROOT / ".env"
ATOMIC_REPLACE_ATTEMPTS = 3
ATOMIC_REPLACE_DELAY = 0.05


def data_dir() -> Path:
    d = Path(os.environ.get("LT_DATA_DIR", PROJECT_ROOT / "data"))
    d.mkdir(parents=True, exist_ok=True)
    return d


def _env_file_for_write() -> Path:
    """Where the wizard persists credentials.

    In Docker LT_DATA_DIR points at the mounted volume, so writing there keeps
    the token across container rebuilds; bare-metal installs use ./.env.
    """
    if os.environ.get("LT_DATA_DIR"):
        return data_dir() / ".env"
    return ENV_FILE


def config_file() -> Path:
    return data_dir() / "config_and_watch.json"


def flights_file() -> Path:
    return data_dir() / "flights_today.json"


def atomic_write_json(path: Path, payload: Any) -> None:
    """Write JSON via a temp file + os.replace so a crash never corrupts state."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        for attempt in range(ATOMIC_REPLACE_ATTEMPTS):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt + 1 == ATOMIC_REPLACE_ATTEMPTS:
                    raise
                time.sleep(ATOMIC_REPLACE_DELAY)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


@dataclass
class Credentials:
    bot_token: str
    chat_id: int
    digest_bot_token: str  # second bot that owns the daily digest (required)


def load_credentials() -> Credentials | None:
    """Read tokens/chat id from the environment (seeded from .env). None if incomplete."""
    load_dotenv(ENV_FILE)
    load_dotenv(_env_file_for_write())
    token = os.environ.get("BOT_TOKEN", "").strip()
    chat_id = os.environ.get("CHAT_ID", "").strip()
    digest_token = os.environ.get("DIGEST_BOT_TOKEN", "").strip()
    if not token or not chat_id or not digest_token:
        return None
    try:
        return Credentials(
            bot_token=token, chat_id=int(chat_id), digest_bot_token=digest_token
        )
    except ValueError:
        return None


def save_credentials(bot_token: str, chat_id: int, digest_bot_token: str) -> None:
    _env_file_for_write().write_text(
        f"BOT_TOKEN={bot_token}\nCHAT_ID={chat_id}\nDIGEST_BOT_TOKEN={digest_bot_token}\n",
        encoding="utf-8",
    )


def harvest_time() -> tuple[int, int]:
    """(hour, minute) for the daily schedule harvest, from HARVEST_TIME (default 06:00)."""
    raw = os.environ.get("HARVEST_TIME", "06:00")
    try:
        hour, minute = raw.split(":")
        return int(hour) % 24, int(minute) % 60
    except ValueError:
        return 6, 0


@dataclass
class Config:
    """In-memory view of config_and_watch.json."""

    target_airports: dict[str, dict[str, Any]] = field(default_factory=dict)
    watchlist: dict[str, dict[str, Any]] = field(default_factory=dict)
    digest_group_by: str = "airport"  # how the digest sections flights: airport|airline|type

    @classmethod
    def load(cls) -> "Config":
        path = config_file()
        if not path.exists():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            target_airports=raw.get("target_airports", {}),
            watchlist=raw.get("watchlist", {}),
            digest_group_by=raw.get("digest_group_by", "airport"),
        )

    def save(self) -> None:
        atomic_write_json(
            config_file(),
            {
                "target_airports": self.target_airports,
                "watchlist": self.watchlist,
                "digest_group_by": self.digest_group_by,
            },
        )

    # -- helpers -----------------------------------------------------------

    def airport_codes(self) -> set[str]:
        """Every IATA and ICAO code among the configured target airports, uppercased."""
        codes: set[str] = set()
        for iata, info in self.target_airports.items():
            codes.add(iata.upper())
            icao = (info.get("icao") or "").upper()
            if icao:
                codes.add(icao)
        return codes

    def airport_for_code(self, code: str) -> tuple[str, dict[str, Any]] | None:
        """Match an IATA or ICAO code against configured airports."""
        code = code.upper()
        for iata, info in self.target_airports.items():
            if code == iata.upper() or code == (info.get("icao") or "").upper():
                return iata, info
        return None
