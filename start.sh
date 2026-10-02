#!/usr/bin/env bash
# One -command setup and launch. Works on macOS and Linux.
#
#   ./start.sh            # create venv, install deps, run both services
#   ./start.sh test       # run the test suite
#   ./start.sh console    # console only  (http://127.0.0.1:8790)
#   ./start.sh tracker    # tracker only  (http://127.0.0.1:8788)
set -euo pipefail
cd "$(dirname "$0")"

PY_BIN="${PYTHON:-python3}"
VENV=".venv"

ensure_venv() {
  if [ ! -d "$VENV" ]; then
    echo "== creating venv =="
    "$PY_BIN" -m venv "$VENV"
  fi
  # shellcheck disable=SC1091
  source "$VENV/bin/activate"
  echo "== installing dependencies =="
  python -m pip install --upgrade pip -q
  python -m pip install -r requirements.txt -q
}

case "${1:-run}" in
  test)
    ensure_venv
    echo "== running tests =="
    python -m pytest tests/ -q
    ;;
  console)
    ensure_venv
    echo "== console on http://127.0.0.1:8790 =="
    exec python -m uvicorn console.app:app --host 127.0.0.1 --port 8790
    ;;
  tracker)
    ensure_venv
    echo "== tracker on http://127.0.0.1:8788 =="
    exec python -m uvicorn tracker.app:app --host 127.0.0.1 --port 8788
    ;;
  run|*)
    ensure_venv
    echo
    echo "  console  ->  http://127.0.0.1:8790"
    echo "  tracker  ->  http://127.0.0.1:8788"
    echo
    python -m uvicorn tracker.app:app --host 127.0.0.1 --port 8788 &
    TRACKER_PID=$!
    trap 'kill $TRACKER_PID 2>/dev/null || true' EXIT INT TERM
    exec python -m uvicorn console.app:app --host 127.0.0.1 --port 8790
    ;;
esac
