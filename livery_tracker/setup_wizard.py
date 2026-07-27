"""Interactive first-run terminal setup wizard.

Collects the BotFather token, auto-captures the owner's chat id, seeds the
airport list from a regional preset (or custom codes), and writes .env plus
the initial JSON state files.
"""

from __future__ import annotations

import time

import httpx

from . import airports as airport_db
from .config import Config, atomic_write_json, flights_file, save_credentials
from .web import USER_AGENT

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

PRESETS: dict[str, list[str]] = {
    "1": ["SFO", "SJC", "OAK"],
    "2": ["LAX", "BUR", "SNA", "SAN"],
    "3": ["JFK", "LGA", "EWR"],
}


def _api(token: str, method: str, **params) -> dict | None:
    try:
        resp = httpx.get(
            TELEGRAM_API.format(token=token, method=method),
            params=params,
            headers={"User-Agent": USER_AGENT},
            timeout=35,
        )
        body = resp.json()
        return body if body.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def _prompt_token() -> str:
    print("\nStep 1/4 — Telegram bot token")
    print("  In Telegram:")
    print("   1. Search for @BotFather (blue checkmark) and open it")
    print("   2. Send /newbot")
    print("   3. Pick any display name (e.g. 'My Livery Tracker')")
    print("   4. Pick a username ending in 'bot' (e.g. mylivery_bot)")
    print("   5. BotFather replies with a token like 1234567:AAxxxx... — paste it below")
    while True:
        token = input("  Bot token: ").strip()
        if not token:
            continue
        me = _api(token, "getMe")
        if me:
            print(f"  ✅ Connected to bot @{me['result'].get('username', '?')}")
            return token
        print("  ❌ Telegram rejected that token, try again.")


def _capture_chat_id(token: str) -> int:
    print("\nStep 2/4 — Link your account")
    print("  Open Telegram and send ANY message to your new bot now.")
    print("  Waiting for your message (up to 5 minutes)...")
    offset = None
    deadline = time.time() + 300
    while time.time() < deadline:
        body = _api(token, "getUpdates", timeout=25, **({"offset": offset} if offset else {}))
        for update in (body or {}).get("result", []):
            offset = update["update_id"] + 1
            message = update.get("message") or {}
            chat = message.get("chat") or {}
            if chat.get("id"):
                chat_id = chat["id"]
                name = chat.get("first_name") or chat.get("username") or "there"
                _api(
                    token,
                    "sendMessage",
                    chat_id=chat_id,
                    text=f"✅ Hi {name}! This chat is now linked to your Livery Tracker.",
                )
                print(f"  ✅ Linked chat id {chat_id} ({name})")
                return chat_id
        time.sleep(1)
    raise SystemExit("  ❌ No message received in 5 minutes — run setup again.")


def _prompt_digest_bot(chat_id: int) -> str:
    print("\nStep 3/4 — Digest bot")
    print("  A SECOND bot owns the once-a-day flight digest message, keeping your")
    print("  command chat completely clean. Back in @BotFather:")
    print("   1. Send /newbot again")
    print("   2. Name it something like 'My Livery Digest' (username e.g. mylivery_digest_bot)")
    print("   3. Paste the new token below")
    while True:
        token = input("  Digest bot token: ").strip()
        if not token:
            print("  ❌ The digest bot is required — create one with @BotFather.")
            continue
        me = _api(token, "getMe")
        if not me:
            print("  ❌ Telegram rejected that token, try again.")
            continue
        username = me["result"].get("username", "?")
        print(f"  ✅ Digest bot @{username} verified.")
        print(f"  Now send ANY message to @{username} so it can post to you.")
        print("  Waiting (up to 5 minutes)...")
        offset = None
        deadline = time.time() + 300
        while time.time() < deadline:
            body = _api(token, "getUpdates", timeout=25, **({"offset": offset} if offset else {}))
            for update in (body or {}).get("result", []):
                offset = update["update_id"] + 1
                chat = (update.get("message") or {}).get("chat") or {}
                if chat.get("id") == chat_id:
                    _api(
                        token,
                        "sendMessage",
                        chat_id=chat_id,
                        text="✅ This chat will receive your daily livery digest.",
                    )
                    print("  ✅ Digest bot linked.")
                    return token
            time.sleep(1)
        raise SystemExit("  ❌ No message received in 5 minutes — run setup again.")


def _pick_airports() -> dict[str, dict]:
    print("\nStep 4/4 — Target airports")
    print("  1) Bay Area, CA — SFO, SJC, OAK  (default)")
    print("  2) SoCal — LAX, BUR, SNA, SAN")
    print("  3) New York Metro — JFK, LGA, EWR")
    print("  4) Custom — enter your own IATA/ICAO codes")
    choice = input("  Choice [1]: ").strip() or "1"
    if choice in PRESETS:
        codes = PRESETS[choice]
    else:
        raw = input("  Enter comma-separated codes (e.g. ORD,MDW or EGLL,EGKK): ")
        codes = [c.strip().upper() for c in raw.split(",") if c.strip()]

    print("  Resolving airport coordinates (first run downloads the OurAirports database)...")
    airports: dict[str, dict] = {}
    for code in codes:
        airport = airport_db.lookup(code)
        if airport is None:
            print(f"  ⚠️  '{code}' not found in the airport database.")
            try:
                lat = float(input(f"     Manual latitude for {code}: ").strip())
                lon = float(input(f"     Manual longitude for {code}: ").strip())
            except ValueError:
                print(f"     Skipping {code}.")
                continue
            airports[code] = {"icao": code, "name": code, "lat": lat, "lon": lon}
        else:
            key = airport.iata or airport.icao
            airports[key] = {
                "icao": airport.icao,
                "name": airport.name,
                "lat": airport.lat,
                "lon": airport.lon,
            }
            print(f"  ✅ {key} ({airport.icao}) — {airport.name}")
    return airports


def run_wizard() -> None:
    print("=" * 60)
    print("  ✈️  AIRCRAFT LIVERY TRACKER — FIRST-RUN SETUP")
    print("=" * 60)

    token = _prompt_token()
    chat_id = _capture_chat_id(token)
    digest_token = _prompt_digest_bot(chat_id)
    airports = _pick_airports()

    save_credentials(token, chat_id, digest_token)
    config = Config.load()
    config.target_airports = airports
    config.save()
    if not flights_file().exists():
        atomic_write_json(flights_file(), [])

    print("\n🎉 Setup complete! Credentials saved to .env, airports saved.")
    print("   Add aircraft from Telegram with:  /add <tail>   (e.g. /add N265AK)")
    print("   The daily schedule harvest runs at 06:00 local time.\n")
