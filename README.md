# ✈️ Aircraft Livery Tracker

Get a live Telegram message whenever one of your favorite special-livery aircraft is scheduled
to arrive at or depart from *your* airports — with live ADS-B tracking as it happens.

**100% free.** No paid flight-data APIs, no keys. Schedules come from
public flight-tracking pages, aircraft/livery metadata from the Planespotters, and live positions come from the community ADS-B networks
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
3. **That single digest is edited in place** as the day unfolds — an hourly sync
   mirrors every pending leg against the source (times, delays, cancellations,
   withdrawn legs), live altitude/speed/distance every 2 minutes from T-1h, final
   ✅ Landed / 🛫 Departed statuses per leg, and a cross-check of every conclusion
   against the source ~25 minutes later.
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
| `beautifulsoup4` | HTML parsing for a fallback schedule source |
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
| `/info <tail>` | Full dossier for any aircraft (watched or not) — see below. `/query` also works |
| `/remove <tail>` | Stop watching (pending legs drop out of the digest) |
| `/dropflight <tail> <flight>` | Remove a stale flight assignment for one aircraft without unwatching it |
| `/watchlist` | Show watched aircraft |
| `/addairport <code>` | Add a target airport by IATA/ICAO code — triggers a full re-harvest |
| `/rmairport <code>` | Remove a target airport (its legs drop out of the digest) |
| `/airports` | List target airports |
| `/refresh` | Re-run today's schedule harvest right now |
| `/rebuild` | Last-resort recovery: re-derive today's schedule from the sources. Keeps your watchlist, airports, aircraft details **and every landing/departure/diversion the tracker directly observed** — a rebuild rebuilds the future, it doesn't rewrite the past (a wrong observed record is `/dropflight`'s job). Like `/dropflight`, deliberately not in the tap menu — type it. 10-min cooldown |
| `/status` | Watchlist size, airports, digest layout, active flight legs |
| `/view` | Show the digest layout; `/view type\|airport\|airline` changes it |
| `/version` | Running version + whether a newer release exists |
| `/update` | Install the latest release now and restart |

### The three refresh tiers — `/refresh` vs `/rebuild` vs the daily harvest

| | What it does | What survives | When |
|---|---|---|---|
| **`/refresh`** | Re-runs the two-phase harvest (airport boards first, then per-tail) to find new legs and update drifted times | **Everything** — existing legs, live tracking, holds and conclusions are untouched; purely additive | On demand (2-min cooldown; skipped if a harvest is already running) |
| **Daily harvest** | Exactly `/refresh` plus the day-boundary chores: purge legs ≥12 h past their time, collapse duplicates, rotate the journal, delete yesterday's digest message and start today's | Everything from today | Automatic, 06:00 local (configurable via `HARVEST_TIME`) |
| **`/rebuild`** | The distrust button: throws away all schedule state and the schedule caches, then re-derives the whole day from the sources | Only what the tracker *witnessed* (landed / departed / diverted legs) plus your watchlist, airports, aircraft dossiers, history and journal | Last resort, typed only (not in the tap menu); 10-min cooldown |

**What each config change triggers:**

- `/add <tail>` — a targeted harvest of just that tail; its legs appear within seconds.
- `/addairport <code>` — a **full re-harvest**, because a new airport changes what every
  watched tail's schedule means (legs previously filtered out now match).
- `/rmairport <code>` — no harvest: every leg targeting that airport is purged
  immediately and the digest redrawn; the watchlist is untouched.
- `/remove <tail>` — mirror image: the tail's legs purge; airports untouched.
- `/dropflight <tail> <flight>` — the scalpel: one flight's legs removed, everything
  else intact.

All of these leave an audit trail in the journal (`command.remove`,
`command.rmairport`, …).

### The automatic background machinery

Everything below runs on its own — the manual commands above are only for
telling the tracker about your fleet or overriding it when a source got
something wrong.

| Cadence | Job | What it does |
|---|---|---|
| 06:00 daily | **Harvest** | Builds the new day: purges yesterday, sweeps your airports' boards, then every watched tail |
| Hourly | **Mirror sync** | Reconciles every pending leg with the source: adopts times/delays verbatim, applies cancellations, withdraws legs no longer listed; a failed fetch marks legs *unverified*, never drops them |
| Every 15 min | **Hot sync** | The same reconciliation, but only for tails with a leg due within 2 h — so a late cancellation or swap can't hide in the hourly gap |
| Every 3 h | **Board discovery** | A cheap boards-only sweep that catches newly scheduled legs between harvests |
| T-1h per leg | **Live start** | Hands the leg to ADS-B (after a physics check that the source's ETD is even possible) |
| Every 2 min per live leg | **Poll** | Live position, landing/departure/diversion detection, delay annotations, callsign and position sanity checks — anomalies trigger extra source re-reads automatically |
| ~25 min after each conclusion | **Verification** | Cross-checks the verdict against the source: direct observations stand, weak inferences defer |
| Every 15 min *(outages only)* | **ADS-B watch** | If every schedule source fails, legs are synthesized from live traffic until the sources recover |
| 04:00 daily | **Update check** | Installs a newer published release if one exists and restarts |

The guiding rule for request budget: **attention follows anomaly**. Quietly
pending legs cost one read an hour; a leg that looks wrong (dark past its
time, wrong callsign, impossible ETD) is automatically re-checked against the
source every few minutes until it looks right again.

## Looking up an aircraft — `/info`

`/info N559AS` returns everything known about a registration, whether or not
it's on your watchlist: the aircraft itself, where it is this second, anything
the tracker is already following today, and its next 24 hours.

```
🔎 N559AS — Alaska Airlines
“Xáat Kwáani”

📋 Aircraft
• Type: Boeing 737-890 (B738)
• Built: 2006 (20 years old)
• Mode S / ICAO hex: A720EB
• Registered in: United States
• Photo on Planespotters

📡 Right now
• ✈️ Airborne at 38,000 ft · 438 kts
• Nearest airport: NGF (35 NM away)
• Flying AS237: SEA ➔ LIH

📅 Upcoming
⭐ • LIH➔SEA AS213 — Mon 1:28 AM PDT
• SEA➔BOS AS306 — Mon 8:23 AM PDT
⭐ touches one of your airports
```

Aircraft facts are cached in `data/aircraft_cache.json` and **accumulate over
time**: the build year, for example, is only published while an aircraft is
transmitting, so the first `/info` run that catches it airborne records the
year permanently. Position and schedules are always fetched live. Add
`/info <tail> refresh` to force a re-fetch of the cached facts.

## Reading the digest

The digest bot keeps **one message per day**, edited in place as flights
progress (yesterday's digest is deleted each morning). If your watchlist grows
past what a single Telegram message can hold (~4096 characters, roughly 45
legs), the digest automatically continues into additional messages — each still
edited in place, so it never stops updating no matter how many aircraft you
watch. Example:

```
✈️ LIVERY DIGEST — Sun Jul 26
Watching 5 aircraft at OAK, SFO, SJC

🔁 Between your airports
🚨 N265AK "West Coast Wonders" — SFO➔LAX AS1052, departed 9:04 AM PDT → ETA 10:31 AM PDT — 21,000 ft · 415 kts · 62 NM out

🛬 Arrivals
🚨 N8658A — DEN➔SFO WN4670 @ SFO, ETA 1:02 PM PDT — 5,025 ft · 235 kts · 17 NM out
🟡 N8658A — LAS➔SJC WN1242 @ SJC, ETA 1:45 PM PDT

🛫 Departures
🕒 N642FR "Hugh the Manatee" — SFO➔LAX F92858 @ SFO, ETD 8:36 PM PDT

Updated 7:57 PM PDT
```

### Status emojis (per flight leg)

| Emoji | State | Meaning |
|---|---|---|
| 🟡 / 🕒 | Scheduled | Pending; mirrored against the source every hour (shows `delayed Xm` from the source's own figures) |
| ⚠️ | Awaiting turnaround / conflict | The source's ETD is physically impossible (inbound landed later, or the aircraft is visibly at another airport); held without guessing a new time |
| 🚨 | Live | Polling ADS-B every 2 min from T-1h — shows the expected time, then altitude · speed · distance |
| ✅ | Landed | Touched down at your airport (or concluded from signal loss on approach) |
| 🛫 | Departed | Climbed through 10,000 ft or left 15 NM (or concluded after going dark airborne) |
| ↪️ | Diverted | Confirmed on the ground 30+ NM away — names the nearest airport |
| ❌ | Cancelled | The airline cancelled the flight (caught by the hourly sync) |
| 🔀 | Swapped / withdrawn | The source no longer lists this leg for this aircraft |
| ⚠️ | Lost | Never appeared on ADS-B by 3 h past its time, and the source offered no explanation — then re-checked against the source afterwards |

Every ✅/🛫/↪️/⚠️ conclusion is verified against the source ~25 minutes later:
direct observations stand even if the source disagrees; weak inferences (a lost
signal, a presumed landing) adopt whatever the source can prove.

### Sections — pick your layout with `/view`

The digest can group the day three ways. Whatever you pick sticks (it's saved in
`config_and_watch.json`) and the digest is redrawn immediately.

**`/view type`** — the default: arrivals and departures.

- **🛬 Arrivals / 🛫 Departures** — legs touching one of your airports.
- **🔁 Between your airports** — a flight connecting *two* watched airports is shown
  as one merged line, `departure phase → arrival phase` (it's still two
  independently tracked legs under the hood).

**`/view airport`** — one section per airport, showing **all traffic in and out**
of it in time order. Best when you care about "what's happening at SFO today".
Two-airport flights deliberately appear under *both* airports here, since they're
a real departure at one end and a real arrival at the other.

```
🛬🛫 SFO — San Francisco
✅ N8658A — DEN➔SFO WN4670, landed 8:03 PM PDT
🟡 N642FR "Hugh the Manatee" — SFO➔SJC F9100, ETD 11:43 PM PDT
🚨 N265AK "Xáat Kwáani" — SEA➔SFO AS1234, ETA 12:58 PM PDT — 12,400 ft · 310 kts · 48 NM out
```

**`/view airline`** — one section per airline (resolved automatically when you
`/add` a tail), each in time order. Best for fleet-watching a single carrier.

```
🏢 Alaska Airlines
🚨 N265AK "Xáat Kwáani" — SEA➔SFO AS1234 @ SFO, ETA 12:58 PM PDT — 12,400 ft · 310 kts · 48 NM out
🟡 N596AS "Tiana's Bayou Adventure" — SJC➔SEA AS1311 @ SJC, ETD Mon 1:03 AM PDT
```

### Notes you may see on a leg

- `delayed 47m` — the sync mirrored the source's own delay figure (it clears
  again if the airline recovers).
- `ETD 10:40 AM PDT — polling, no ADS-B contact yet` — the leg is inside its
  live window and being polled every 2 minutes, but the aircraft's transponder
  hasn't been picked up (usually parked at a gate). The scheduled time keeps
  updating from the source until the first position arrives. Once it does, the
  same line carries the live figures after the time: `ETA 1:02 PM PDT — 5,025 ft
  · 235 kts · 17 NM out`.
- `~10:51 AM (per source)` — the outcome was adopted from the source's record
  rather than watched live (e.g. the aircraft was already wearing its next
  callsign, or out of receiver coverage). The `~` always marks an adopted time.
- `(signal lost on approach)` / `(signal lost after takeoff)` — the aircraft went
  dark near the ground (common — receiver coverage thins at low altitude) and the
  outcome was inferred from its last known position and vertical rate.
- `aircraft still operating SWA3982 — awaiting rotation` — the tail is visibly on
  (or fresh off) an earlier flight of its own rotation; the leg is held until the
  expected flight is seen, never swapped off on callsign evidence alone.
- `aircraft seen on the ground near DFW — awaiting schedule update` — the
  transponder places the aircraft somewhere that makes this departure impossible;
  held until the schedule catches up.
- `no ADS-B contact 47m past ETD — likely delayed` / `running 25m late` / `running
  32m late — still on the ground` — obvious-delay annotations from live position
  (or the lack of one) while the source still says on time. An overdue departure —
  parked in view or dark alike — also re-checks the source every few minutes, so a
  late-published delay or cancellation is caught even mid-live-tracking, and a
  delay that leaves the live window stands polling down until the new T-1h.
- `unverified — source unreachable` — the last sync could not reach the source;
  the leg is kept, flagged, and re-verified next pass.
- `(confirmed by source)` / `(source disagrees)` — what the ~25-minute
  post-conclusion cross-check found.
- `found via ADS-B watch` — the leg was discovered from live traffic during a
  schedule-source outage, not from a schedule.

## Files on disk (`data/`)

| File | What it holds |
|---|---|
| `config_and_watch.json` | Target airports, watched aircraft, digest layout |
| `flights_today.json` | Today's tracked legs and their states (crash recovery) |
| `digest_state.json` | Today's digest message id (for in-place edits) |
| `schedule_cache.json` | Cached schedule per tail (12h outage buffer) |
| `aircraft_cache.json` | Accumulated aircraft facts for `/info` (type, hex, build year) |
| `history.jsonl` | Every concluded leg with final telemetry — audit/tuning log |
| `journal.jsonl` | Every state transition with its cause and evidence — the debugging log (rotated daily to `journal-YYYYMMDD.jsonl`, 7 days kept) |
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

- **Two-phase harvest**: first a sweep of your airports' arrival/departure boards
  (~20 requests, under a minute) which publishes the digest straight away, then a
  slower per-tail sweep that fills in anything the boards omit — a board entry has
  no registration until the airline assigns a tail. A flight found by both phases
  updates in place rather than appearing twice. Only one harvest runs at a time;
  a `/refresh` during one is ignored.
- Schedule harvesting impersonates a real Chrome TLS fingerprint (`curl_cffi`), and
  a single process-wide minimum interval keeps every request ≥2s apart.
- **Staying under the radar**: every source is rate-limited and memoised, since
  these are free community services. Schedule lookups are spaced ≥2s apart
  process-wide and cached 5 min; live positions cache 45s and callsign routes 1h;
  aircraft facts cache 30 days and the airport database 90 days. On top of that,
  `/info` and `/add` have a 10s per-user cooldown and `/refresh` a 2-minute one,
  so repeat taps cost nothing — a second `/info` on the same tail makes **zero**
  network calls. Failures are never cached, so an outage still retries.
  The hourly sync only fetches tails that actually have pending legs, new-leg
  discovery between harvests is a cheap boards-only sweep every 3 h, and the
  watchlist is capped at 256 aircraft, so the request budget stays polite no
  matter how the fleet grows.
- Live polling only runs from T-1h until each leg concludes, once every 120 seconds.
- **Telemetry coverage**: positions come from adsb.fi, then adsb.lol, and then other public ADSB sources if the former are unavailable.
- **The hourly mirror sync**: every tail that still has pending legs is re-read once
  an hour and reconciled with the source — times and delay figures are adopted
  verbatim, cancellations become ❌, and a leg the source no longer lists is
  withdrawn (🔀). A failed fetch marks legs *unverified* instead of dropping them.
  Legs being tracked live belong to ADS-B and are never touched by the sync — but
  only once ADS-B has actually seen them: a leg that went live while the aircraft
  sits dark at a gate stays in the mirror's custody, so a delay published after
  T-1h still reaches the digest (and a big one hands the leg back to waiting).
  On top of the hourly floor, a **15-minute hot pass** covers just the tails with a
  leg due within 2 hours, so a late cancellation or swap can't hide in the gap.
- **Completed-awareness**: whenever the tracker consults the source about a leg it
  can't observe (a mismatched callsign, a held conflict, an aircraft that never
  appeared on ADS-B), it also reads whether the source records the flight as
  already flown — and if so concludes ✅/🛫 *per source* with the actual time,
  instead of holding or marking the leg lost. Live observation still always wins
  when it exists.
- **Delays**: delay figures always come from the source's own scheduled-vs-estimated
  pair. A flight that hasn't shown up 30 min past its time gets extra schedule
  checks — if it moved later, the leg waits for the new time; if the source offers
  no explanation, the digest says *likely delayed* rather than inventing an outcome.
- **Position sanity**: if a departure is due within 2 h but the aircraft's
  transponder shows it on the ground 50+ NM away, the leg is held (⚠️) until the
  schedule catches up or the real flight is seen airborne — a dark transponder is
  never treated as evidence.
- **Cancellations** are detected by the hourly sync (❌ in the digest),
  with a by-flight-number fallback.
- **Diversions**: an arrival confirmed on the ground 30+ NM from your airport on two
  consecutive polls is marked ↪️ *diverted*, with the nearest sizeable airport named.
- **Signal loss**: ADS-B coverage is patchy near the ground, so a plane last seen low
  and close that goes dark is concluded ✅ *landed (signal lost on approach)*; one
  that never shows up at all is annotated *likely delayed* and only marked ⚠️ *lost*
  at the 3-hour hard stop every live leg has.
- **Conclusion verification**: ~25 minutes after any ✅/🛫/↪️/⚠️ verdict, the leg is
  compared with the source's row. Direct observations stand even when the source
  disagrees (a confirmed diversion the source doesn't show stays a diversion); weak
  inferences defer — a *lost* leg the source says landed becomes ✅ with the source's
  real time, and one the source still expects is reopened for tracking.
- **Source outages**: schedules are cached for 12h to ride out potential failures in the data. If every schedule source fails, you get a warning from the command bot
  and **ADS-B watch mode** takes over — the tracker polls your tails' live positions
  every 15 minutes, resolves routes from callsigns via the free adsbdb.com API, and
  synthesizes legs on the fly for anything touching your airports. Because community
  route databases can be stale, a claimed *origin* is only trusted when the aircraft
  is actually observed near that airport; destinations self-verify via live tracking.
- Every concluded leg is appended to `data/history.jsonl` with its final telemetry,
  so you can audit the inference decisions against reality and tune thresholds.
- **The flight journal**: every state transition, schedule change, creation and
  removal of a leg is appended to `data/journal.jsonl` — with the code path that
  caused it (`poll.callsign_mismatch`, `hourly_sync.withdrawn`, `verify.corrected`,
  …) and the evidence it acted on (the telemetry seen, the source's answer). When
  a digest line looks wrong hours later, `grep <tail> data/journal*.jsonl` shows
  the exact decision and what the sources were saying at that moment. Routine
  2-minute telemetry updates are not journaled — only real changes — and the file
  rotates daily, keeping a week of archives.

## License

GPL-3.0 — see [LICENSE](LICENSE).
