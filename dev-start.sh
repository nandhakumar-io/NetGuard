#!/usr/bin/env bash
# dev-start.sh — starts NetGuard backend + frontend locally.
#                Infrastructure containers must already be running.
set -e

ROOT="$(cd "$(dirname "$0")" && pwd)"
PYTHON="$ROOT/backend/.venv_local/bin/python"
BACKEND_LOG="/tmp/netguard-backend.log"
FRONTEND_LOG="/tmp/netguard-frontend.log"
BACKEND_PID="/tmp/netguard-backend.pid"
FRONTEND_PID="/tmp/netguard-frontend.pid"

# ── stop any previous instances ────────────────────────────────────────────────
for pid_file in "$BACKEND_PID" "$FRONTEND_PID"; do
  if [[ -f "$pid_file" ]]; then
    pid=$(cat "$pid_file")
    kill "$pid" 2>/dev/null && echo "Stopped PID $pid" || true
    rm -f "$pid_file"
  fi
done

# ── backend ────────────────────────────────────────────────────────────────────
echo "▶  Starting backend on :8000  (log: $BACKEND_LOG)"
(
  cd "$ROOT/backend"
  "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload \
    >> "$BACKEND_LOG" 2>&1 &
  echo $! > "$BACKEND_PID"
)
sleep 1
echo "   Backend PID: $(cat $BACKEND_PID)"

# ── frontend ───────────────────────────────────────────────────────────────────
echo "▶  Starting frontend on :6001  (log: $FRONTEND_LOG)"
(
  cd "$ROOT/frontend"
  npm run dev >> "$FRONTEND_LOG" 2>&1 &
  echo $! > "$FRONTEND_PID"
)
sleep 1
echo "   Frontend PID: $(cat $FRONTEND_PID)"

echo ""
echo "✅  NetGuard is starting up."
echo "   Frontend : http://localhost:6001"
echo "   Backend  : http://localhost:8000"
echo "   Backend docs : http://localhost:8000/docs"
echo ""
echo "   Tail backend logs  : tail -f $BACKEND_LOG"
echo "   Tail frontend logs : tail -f $FRONTEND_LOG"
echo ""
echo "   To stop: bash $ROOT/dev-stop.sh"
