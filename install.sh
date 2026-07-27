#!/usr/bin/env bash
# Aircraft Livery Tracker — one-line installer / updater for Linux, macOS, Raspberry Pi.
#
#   curl -fsSL https://raw.githubusercontent.com/Edward358-AI/livery-tracker/main/install.sh | bash
#
# Installs to ~/.local/share/livery-tracker (override with LT_INSTALL_DIR),
# sets up a venv + dependencies, runs the first-time wizard, and registers a
# sudo-free autostart (systemd --user on Linux, launchd on macOS). Safe to
# re-run: upgrades code in place, never touches your data or bot tokens.
set -euo pipefail

REPO="Edward358-AI/livery-tracker"
INSTALL_DIR="${LT_INSTALL_DIR:-$HOME/.local/share/livery-tracker}"

echo ""
echo "============================================="
echo "   Aircraft Livery Tracker - installer"
echo "============================================="
echo "   Install folder: $INSTALL_DIR"
echo ""

# --- 1. Python 3.10+ ----------------------------------------------------------
PY="$(command -v python3 || true)"
ok_python() { "$1" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' 2>/dev/null; }
if [ -z "$PY" ] || ! ok_python "$PY"; then
  echo "Python 3.10+ is required but was not found."
  echo "  Debian/Ubuntu/Pi:  sudo apt install python3 python3-venv"
  echo "  macOS:             brew install python"
  echo "Then re-run this installer."
  exit 1
fi
echo "[1/5] Python: $PY"

# --- 2. Download the latest release (falls back to main) ----------------------
TAG="$(curl -fsSL -H 'User-Agent: LiveryTracker-Installer' \
  "https://api.github.com/repos/$REPO/releases/latest" 2>/dev/null \
  | "$PY" -c 'import sys, json
try: print(json.load(sys.stdin).get("tag_name", ""))
except Exception: print("")' || true)"
if [ -n "$TAG" ]; then
  URL="https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG"
else
  TAG="main"
  URL="https://codeload.github.com/$REPO/tar.gz/refs/heads/main"
fi
echo "[2/5] Downloading $TAG ..."
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT
curl -fsSL -H 'User-Agent: LiveryTracker-Installer' "$URL" | tar -xz -C "$TMP"
SRC="$(find "$TMP" -mindepth 1 -maxdepth 1 -type d | head -1)"

# --- 3. Copy code into place (data/ and .env are never touched) ---------------
mkdir -p "$INSTALL_DIR"
for item in livery_tracker requirements.txt runner.sh runner.ps1 install.sh \
            install.ps1 tracker.sh livery-tracker.service README.md LICENSE; do
  [ -e "$SRC/$item" ] || continue
  rm -rf "${INSTALL_DIR:?}/$item"
  cp -R "$SRC/$item" "$INSTALL_DIR/$item"
done
chmod +x "$INSTALL_DIR/runner.sh" "$INSTALL_DIR/tracker.sh" 2>/dev/null || true
echo "[3/5] Code installed."

# --- 4. Virtual environment + dependencies ------------------------------------
if [ ! -x "$INSTALL_DIR/.venv/bin/python" ]; then
  "$PY" -m venv "$INSTALL_DIR/.venv"
fi
"$INSTALL_DIR/.venv/bin/python" -m pip install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/python" -m pip install --quiet -r "$INSTALL_DIR/requirements.txt"
echo "[4/5] Dependencies ready."

# --- 5. First-run wizard + autostart ------------------------------------------
cd "$INSTALL_DIR"
if [ ! -f .env ] && [ ! -f data/.env ]; then
  echo ""
  echo "Time to set up your Telegram bots - the wizard will walk you through it."
  # </dev/tty so the wizard stays interactive under `curl | bash`
  "$INSTALL_DIR/.venv/bin/python" -m livery_tracker --setup < /dev/tty
fi

AUTOSTART="manual (start with: $INSTALL_DIR/runner.sh &)"
case "$(uname)" in
  Linux)
    if command -v systemctl >/dev/null 2>&1 && systemctl --user show-environment >/dev/null 2>&1; then
      mkdir -p "$HOME/.config/systemd/user"
      cat > "$HOME/.config/systemd/user/livery-tracker.service" <<EOF
[Unit]
Description=Aircraft Livery Tracker
After=network-online.target

[Service]
WorkingDirectory=$INSTALL_DIR
ExecStart=$INSTALL_DIR/.venv/bin/python -m livery_tracker
Restart=always
RestartSec=10

[Install]
WantedBy=default.target
EOF
      systemctl --user daemon-reload
      systemctl --user enable --now livery-tracker
      AUTOSTART="systemd user service (systemctl --user status livery-tracker)"
      if command -v loginctl >/dev/null 2>&1; then
        loginctl enable-linger "$USER" 2>/dev/null \
          || echo "   Tip: run 'sudo loginctl enable-linger $USER' so it keeps running after you log out."
      fi
    fi
    ;;
  Darwin)
    PLIST="$HOME/Library/LaunchAgents/com.liverytracker.plist"
    mkdir -p "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.liverytracker</string>
  <key>ProgramArguments</key><array>
    <string>/bin/bash</string><string>$INSTALL_DIR/runner.sh</string>
  </array>
  <key>WorkingDirectory</key><string>$INSTALL_DIR</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><dict><key>SuccessfulExit</key><false/></dict>
  <key>StandardOutPath</key><string>$INSTALL_DIR/tracker.log</string>
  <key>StandardErrorPath</key><string>$INSTALL_DIR/tracker.log</string>
</dict></plist>
EOF
    launchctl unload "$PLIST" 2>/dev/null || true
    launchctl load "$PLIST"
    AUTOSTART="launchd agent (starts at login)"
    ;;
esac

echo ""
echo "============================================="
echo "   Livery Tracker is installed and running!"
echo "============================================="
echo "   Version:    $TAG"
echo "   Autostart:  $AUTOSTART"
echo "   Updates:    automatic (daily check at 4 AM, or /update in Telegram)"
echo ""
echo "   Open Telegram and send /status to your bot to say hello."
echo "   Add aircraft with /add <tail>, airports with /addairport <code>."
echo ""
