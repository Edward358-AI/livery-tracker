#!/usr/bin/env bash
# Livery Tracker control script for Linux/macOS (non-Docker installs).
#   ./tracker.sh setup | start | stop | status | logs | install-service
set -euo pipefail

cd "$(dirname "$0")"
VENV=".venv"
PY="$VENV/bin/python"
PIDFILE="tracker.pid"
LOGFILE="tracker.log"
SERVICE_NAME="livery-tracker"

ensure_venv() {
  if [ ! -x "$PY" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV"
    "$PY" -m pip install --quiet --upgrade pip
  fi
  "$PY" -m pip install --quiet -r requirements.txt
}

is_running() {
  [ -f "$PIDFILE" ] && kill -0 "$(cat "$PIDFILE")" 2>/dev/null
}

case "${1:-}" in
  setup)
    ensure_venv
    "$PY" -m livery_tracker --setup
    ;;
  start)
    if is_running; then
      echo "Already running (pid $(cat "$PIDFILE"))."
      exit 0
    fi
    ensure_venv
    nohup "$PY" -m livery_tracker >> "$LOGFILE" 2>&1 &
    echo $! > "$PIDFILE"
    echo "Started (pid $(cat "$PIDFILE")). Logs: ./tracker.sh logs"
    ;;
  stop)
    if is_running; then
      kill "$(cat "$PIDFILE")"
      rm -f "$PIDFILE"
      echo "Stopped."
    else
      rm -f "$PIDFILE"
      echo "Not running."
    fi
    ;;
  status)
    if is_running; then
      echo "RUNNING (pid $(cat "$PIDFILE"))"
    else
      echo "STOPPED"
    fi
    if [ -f data/config_and_watch.json ]; then
      "$PY" - <<'EOF'
import json
cfg = json.load(open("data/config_and_watch.json"))
print(f"Watched tails:   {len(cfg.get('watchlist', {}))}")
print(f"Target airports: {', '.join(sorted(cfg.get('target_airports', {}))) or 'none'}")
try:
    flights = json.load(open("data/flights_today.json"))
    active = [f for f in flights if f.get("status") not in ("LANDED", "DEPARTED", "LOST")]
    print(f"Active legs:     {len(active)} of {len(flights)} today")
except FileNotFoundError:
    pass
EOF
    fi
    ;;
  logs)
    touch "$LOGFILE"
    tail -f "$LOGFILE"
    ;;
  install-service)
    if [ "$(uname)" != "Linux" ]; then
      echo "systemd install is Linux-only."; exit 1
    fi
    ensure_venv
    sed -e "s|__WORKDIR__|$(pwd)|g" -e "s|__USER__|$(whoami)|g" \
      "$SERVICE_NAME.service" | sudo tee "/etc/systemd/system/$SERVICE_NAME.service" > /dev/null
    sudo systemctl daemon-reload
    sudo systemctl enable --now "$SERVICE_NAME"
    echo "Installed and started systemd service '$SERVICE_NAME'."
    echo "Follow logs with: journalctl -u $SERVICE_NAME -f"
    ;;
  *)
    echo "Usage: $0 {setup|start|stop|status|logs|install-service}"
    exit 1
    ;;
esac
