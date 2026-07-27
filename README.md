# ✈️ Aircraft Livery Tracker

Get a live Telegram message whenever one of your favorite special-livery aircraft is scheduled
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

## System Prequisites

### Everyone

| Requirement | Why |
|---|---|
| A **Telegram account** | You'll create two free bots with [@BotFather](https://t.me/BotFather) during setup — the wizard walks you through it |
| A machine that **stays on** | Live tracking only happens while the tracker is running. A always-on desktop, home server, or Raspberry Pi is ideal; a laptop that sleeps will miss flights |
| **Internet access** | Everything is fetched live — no offline mode |
| ~**300 MB free disk** | Python packages, plus a ~9 MB airport database cached on first run |

No paid accounts, API keys, or subscriptions — ever.

### Windows

**Nothing to pre-install.** The one-line installer detects Python and offers to
install it for you via `winget` (built into Windows 10 1809+ and Windows 11).

If you're on an older Windows without `winget`, install
[Python 3.10 or newer](https://www.python.org/downloads/) first and tick
**"Add python.exe to PATH"** in the installer.

### Linux / macOS / Raspberry Pi

You need **Python 3.10+** (with `venv`), plus `curl` and `tar` — the latter two
are already on virtually every system.

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt update && sudo apt install -y python3 python3-venv curl

# Fedora
sudo dnf install -y python3 curl

# macOS (Python 3 also comes with Xcode Command Line Tools)
brew install python
```

> On Debian-based systems `python3-venv` is a **separate package** from
> `python3` — the installer will fail without it, so don't skip it.

### Only for the advanced paths

| Path | Extra requirement |
|---|---|
| Git checkout | [Git](https://git-scm.com/downloads) |
| Docker | [Docker Desktop](https://www.docker.com/products/docker-desktop/) or Docker Engine |
| Windows always-on service | [NSSM](https://nssm.cc) (`winget install NSSM.NSSM`) + admin rights |

### Installed for you automatically

You never install these by hand — the installer puts them in an isolated virtual
environment, so nothing touches your system Python:

| Package | Used for |
|---|---|
| `python-telegram-bot[job-queue]` | Bot commands, message edits, and all scheduling |
| `curl_cffi` | Chrome-impersonating requests for schedule harvesting |
| `httpx` | Plain HTTP for the ADS-B and airport APIs |
| `beautifulsoup4` | HTML parsing for the FlightAware fallback |
| `python-dotenv` | Reading your `.env` credentials |

## Quick start — one line, no git, no Docker

**Windows** (paste into PowerShell):

```powershell
iwr -useb https://raw.githubusercontent.com/Edward358-AI/livery-tracker/main/install.ps1 | iex
```

**Linux / macOS / Raspberry Pi** (paste into a terminal):

```bash
curl -fsSL https://raw.githubusercontent.com/Edward358-AI/livery-tracker/main/install.sh | bash
```

The installer finds (or installs) Python, downloads the latest release, walks you
through the Telegram setup wizard, and registers the tracker to start
automatically at login — no admin rights needed. Then it **keeps itself
updated**: every night at 4 AM it checks for a new release and installs it,
telling you in Telegram when it does. `/version` shows what you're running,
`/update` upgrades on the spot. You never touch git.

Re-running the install line upgrades an existing install in place — your
watchlist, airports, and bot tokens are never touched.

<details>
<summary><b>Advanced installs — git checkout, Docker, system services</b></summary>

### Git checkout (developers)

```bash
git clone https://github.com/Edward358-AI/livery-tracker.git
cd livery-tracker
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt      # (bin/pip on Unix)
.venv/Scripts/python -m livery_tracker --setup
.venv/Scripts/python -m livery_tracker
```

Auto-update disables itself in a git checkout (you pull instead), in Docker
(rebuild the image), and wherever `LT_AUTO_UPDATE=0` is set.

### Docker

```bash
docker compose build
docker compose run --rm livery-tracker python -m livery_tracker --setup
docker compose up -d
```

### System services (always-on boxes)

- **Linux**: the one-line installer already sets up a sudo-free `systemd --user`
  unit; for a system-wide unit on a headless box use `./tracker.sh install-service`.
- **Windows always-on** (runs with nobody logged in): install
  [NSSM](https://nssm.cc) (`winget install NSSM.NSSM`), then from an **admin**
  PowerShell run `install-service.ps1` from the install folder. Uninstall with
  `install-service.ps1 -Uninstall`. Logs go to `tracker.log`.
- `tracker.sh setup | start | stop | status | logs` also works for manual
  background running on Linux/macOS.

### Publishing a release (repo owner)

Friends' trackers only ever auto-install **published releases**, never raw
commits — cutting a release is the "this is stable" gate:

```powershell
.\release.ps1 1.2.0
```

That bumps `__version__`, runs the tests, commits, tags, pushes, and creates the
GitHub release. Everyone's tracker picks it up within a day.

</details>

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
| `/version` | Running version + whether a newer release exists |
| `/update` | Install the latest release now and restart |

## Reading the digest

The digest bot keeps exactly **one message per day**, edited in place as flights
progress (yesterday's digest is deleted each morning). Example:

```
✈️ LIVERY DIGEST — Sun Jul 26
Watching 5 aircraft at OAK, SFO, SJC

🔁 Between your airports
🚨 N265AK "West Coast Wonders" — SFO➔LAX AS1052, departed 9:04 AM PDT → 21,000 ft · 415 kts · 62 NM out

🛬 Arrivals
🚨 N8658A — DEN➔SFO WN4670 @ SFO, 5,025 ft · 235 kts · 17 NM out
🟡 N8658A — LAS➔SJC WN1242 @ SJC, ETA 1:45 PM PDT

🛫 Departures
🕒 N642FR "Hugh the Manatee" — SFO➔LAX F92858 @ SFO, ETD 8:36 PM PDT

Updated 7:57 PM PDT
```

### Status emojis (per flight leg)

| Emoji | State | Meaning |
|---|---|---|
| 🟡 | Scheduled | Harvested; waiting for the T-2h schedule re-check |
| 🕒 | Confirmed | Schedule re-checked at T-2h (shows `(delayed Xm)` if it moved) |
| 🚨 | Live | Polling ADS-B every 2 min — shows altitude · speed · distance |
| ✅ | Landed | Touched down at your airport (or concluded from signal loss on approach) |
| 🛫 | Departed | Climbed through 10,000 ft or left 15 NM (or concluded after going dark airborne) |
| ↪️ | Diverted | Confirmed on the ground 30+ NM away — names the nearest airport |
| ❌ | Cancelled | The airline cancelled the flight (caught at the T-2h re-check) |
| ⚠️ | Lost | Never appeared on ADS-B by 30 min past its time (after a delay re-check) |

### Sections

- **🛬 Arrivals / 🛫 Departures** — legs touching one of your airports.
- **🔁 Between your airports** — a flight connecting *two* watched airports is shown
  as one merged line, `departure phase → arrival phase` (it's still two
  independently tracked legs under the hood).

### Notes you may see on a leg

- `(signal lost on approach)` / `(signal lost after takeoff)` — the aircraft went
  dark near the ground (common — receiver coverage thins at low altitude) and the
  outcome was inferred from its last known position and vertical rate.
- `delayed 47m` — the schedule re-check found the flight running late.
- `found via ADS-B watch` — the leg was discovered from live traffic during a
  schedule-source outage, not from a schedule.

## Files on disk (`data/`)

| File | What it holds |
|---|---|
| `config_and_watch.json` | Target airports + watched aircraft |
| `flights_today.json` | Today's tracked legs and their states (crash recovery) |
| `digest_state.json` | Today's digest message id (for in-place edits) |
| `schedule_cache.json` | Last good FR24 schedule per tail (12h outage buffer) |
| `history.jsonl` | Every concluded leg with final telemetry — audit/tuning log |
| `airports.csv` | Cached OurAirports database (~9 MB, refreshed every 90 days) |

## Configuration knobs (`.env`)

| Variable | Default | Meaning |
|---|---|---|
| `BOT_TOKEN` | — | Telegram bot token (commands) |
| `CHAT_ID` | — | Your Telegram chat id (wizard fills this) |
| `DIGEST_BOT_TOKEN` | — | Second bot that owns the daily digest message |
| `HARVEST_TIME` | `06:00` | Local time of the daily schedule harvest |
| `LT_DATA_DIR` | `./data` | Where runtime state lives |
| `LT_AUTO_UPDATE` | `1` | Set to `0` to disable the nightly self-update check |

## Notes on data sources & resilience

- Schedule harvesting impersonates a real Chrome TLS fingerprint (`curl_cffi`) and
  spaces lookups ~3s apart to be polite to the free endpoints.
- Live polling only runs in a short window around each flight (T-45m for arrivals,
  T-15m for departures), once every 120 seconds.
- **Telemetry coverage**: positions come from adsb.fi, then adsb.lol, then FR24's
  satellite-backed feed as a last resort — so oceanic and remote flights (where
  community receivers can't reach) are still visible.
- **Delays**: harvest uses live estimated times; the T-2h re-check updates them; and
  a flight that hasn't shown up 30 min past its time gets one more schedule check —
  if it moved later, the leg waits for the new time instead of being marked lost.
- **Cancellations** are detected at the T-2h schedule re-check (❌ in the digest),
  with a by-flight-number fallback since FR24 unassigns tails from cancelled flights.
- **Diversions**: an arrival confirmed on the ground 30+ NM from your airport on two
  consecutive polls is marked ↪️ *diverted*, with the nearest sizeable airport named.
- **Signal loss**: ADS-B coverage is patchy near the ground, so a plane last seen low
  and close that goes dark is concluded ✅ *landed (signal lost on approach)*; one
  that never shows up at all is ⚠️ *lost* 30 min past schedule, and every live leg
  has a 3-hour hard stop.
- **Source outages**: schedules are cached for 12h to ride out intra-day FR24
  failures. If every schedule source fails, you get a warning from the command bot
  and **ADS-B watch mode** takes over — the tracker polls your tails' live positions
  every 15 minutes, resolves routes from callsigns via the free adsbdb.com API, and
  synthesizes legs on the fly for anything touching your airports. Because community
  route databases can be stale, a claimed *origin* is only trusted when the aircraft
  is actually observed near that airport; destinations self-verify via live tracking.
- Every concluded leg is appended to `data/history.jsonl` with its final telemetry,
  so you can audit the inference decisions against reality and tune thresholds.

## License

GPL-3.0 — see [LICENSE](LICENSE).
