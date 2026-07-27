#!/usr/bin/env bash
# Keeps the tracker alive: restarts on exit code 42 (self-update) or crash.
cd "$(dirname "$0")"
while true; do
  .venv/bin/python -m livery_tracker
  code=$?
  if [ "$code" -eq 0 ]; then break; fi        # clean shutdown -> stay stopped
  if [ "$code" -eq 42 ]; then
    echo "Restarting after self-update..."
    continue
  fi
  echo "Tracker exited with code $code - restarting in 15s..."
  sleep 15
done
