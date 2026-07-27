# ✈️ Aircraft Livery Tracker

Get a Telegram ping whenever one of your favorite special-livery aircraft is scheduled
to arrive at or depart from *your* airports — with live ADS-B tracking as it happens.

**100% free.** No FlightAware AeroAPI subscription, no paid keys. Schedules come from
public flight-tracking pages, aircraft/livery metadata from the Planespotters and FR24
public endpoints, live positions from the community ADS-B networks
([adsb.fi](https://adsb.fi) / [adsb.lol](https://adsb.lol)), and airport coordinates
from [OurAirports](https://ourairports.com).

## How it works

1. **You manage everything from Telegram** — `/add N265AK` watches a tail (airline,
   type, livery, and photo are resolved automatically), `/addairport LAX` adds a
   target airport anywhere in the world (coordinates auto-geocoded). Adding a tail
   or airport immediately re-harvests today's schedules.
2. **Every morning at 06:00 (server-local time)** the tracker harvests each watched
   tail's schedule and posts **one digest message** listing every matching
   arrival/departure leg — and deletes yesterday's digest, so the digest chat only
   ever contains a single message.
3. **That single digest is edited in place** as the day unfolds — schedule re-checks
   at T-2h, live altitude/speed/distance every 2 minutes near the scheduled time,
   and final ✅ Landed / 🛫 Departed statuses per leg.
4. **Two bots, two clean chats** — a command bot for managing the fleet/airports and
   a dedicated digest bot that only carries the daily digest. If a flight connects
   two of your airports (say SFO ➔ LAX), it appears as two independent legs: a
   departure tracked against SFO and an arrival tracked against LAX.
5. **Crash-safe** — all state lives in `data/*.json`; on restart the tracker resumes
   every pending flight exactly where it left off.

## Quick start

### Option A — Docker

Prereqs: [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine.

```bash
git clone https://github.com/Edward358-AI/livery-tracker.git
cd livery-tracker
docker compose build
docker compose run --rm livery-tracker python -m livery_tracker --setup   # interactive wizard
docker compose up -d
```

The container auto-restarts with your machine (`restart: unless-stopped`).
Logs: `docker compose logs -f`

### Option B — Linux / macOS / Raspberry Pi (no Docker)

```bash
git clone https://github.com/Edward358-AI/livery-tracker.git
cd livery-tracker
./tracker.sh setup    # creates venv, installs deps, runs the wizard
./tracker.sh start    # runs in the background
```

Also available: `./tracker.sh stop | status | logs`, and on a 24/7 Linux box
`./tracker.sh install-service` installs a systemd unit that starts on boot.

### Option C — Windows (no Docker needed)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
.venv\Scripts\python -m livery_tracker --setup   # interactive wizard
.venv\Scripts\python -m livery_tracker           # run in this terminal
```

To run it 24/7 as a real Windows service (auto-start on boot, auto-restart on
crash), install [NSSM](https://nssm.cc) and use the bundled installer from an
**admin** PowerShell:

```powershell
winget install NSSM.NSSM     # then open a NEW admin PowerShell
powershell -ExecutionPolicy Bypass -File install-service.ps1
```

Uninstall with `install-service.ps1 -Uninstall`. Logs go to `tracker.log`.

## The setup wizard

On first run you'll be asked for:

1. **Command bot token** — create a bot in Telegram with
   [@BotFather](https://t.me/BotFather) (`/newbot`) and paste the token.
2. **Your chat** — send any message to your new bot; the wizard links it automatically.
3. **Digest bot** — create a *second* bot the same way and paste its token; message it
   once so it can post to you. It will own the daily digest message.
4. **Airports** — pick a preset (Bay Area / SoCal / NY Metro) or type any IATA/ICAO
   codes on Earth (`ORD,MDW`, `EGLL,EGKK`, ...). Coordinates resolve automatically.

## Telegram commands

| Command | What it does |
|---|---|
| `/add <tail>` | Watch a registration — livery/photo auto-resolved, schedule harvested right away |
| `/remove <tail>` | Stop watching (pending legs drop out of the digest) |
| `/watchlist` | Show watched aircraft |
| `/addairport <code>` | Add a target airport by IATA/ICAO code — triggers a full re-harvest |
| `/rmairport <code>` | Remove a target airport (its legs drop out of the digest) |
| `/airports` | List target airports |
| `/refresh` | Re-run today's schedule harvest right now |
| `/status` | Watchlist size, airports, active flight legs |

## Configuration knobs (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token (commands) |
| `CHAT_ID` | — | Your Telegram chat id (wizard fills this) |
| `DIGEST_BOT_TOKEN` | — | Second bot that owns the daily digest message |
| `HARVEST_TIME` | `06:00` | Local time of the daily schedule harvest |
| `LT_DATA_DIR` | `./data` | Where runtime state lives |

## Notes on data sources

- Schedule harvesting impersonates a real Chrome TLS fingerprint (`curl_cffi`) and
  spaces lookups ~3s apart to be polite to the free endpoints.
- If an aircraft's transponder goes dark, the leg is marked ⚠️ *Tracking Lost* 30
  minutes past its scheduled time instead of polling forever.
- Live polling only runs in a short window around each flight (T-45m for arrivals,
  T-15m for departures), once every 120 seconds.

## License

GPL-3.0 — see [LICENSE](LICENSE).
