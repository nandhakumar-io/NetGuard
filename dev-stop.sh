#!/usr/bin/env bash
# dev-stop.sh — stops backend + frontend local processes.
for name in backend frontend; do
  pid_file="/tmp/netguard-$name.pid"
  if [[ -f "$pid_file" ]]; then
    pid=$(cat "$pid_file")
    if kill "$pid" 2>/dev/null; then
      echo "✓ Stopped $name (PID $pid)"
    else
      echo "  $name was not running (PID $pid)"
    fi
    rm -f "$pid_file"
  else
    echo "  No PID file for $name"
  fi
done
