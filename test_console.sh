#!/usr/bin/env bash
# End-to-end test of the Operator Console.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8790
export CONSOLE_DB="$PWD/console_test.db"
rm -f console_test.db console_test.db-wal console_test.db-shm

.venv/bin/python -m uvicorn console.app:app --port $PORT --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 40); do
  curl -s "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1 && break
  sleep 0.3
done
B="http://127.0.0.1:$PORT"

echo "=== 1. initial state ==="
curl -s "$B/api/state" | .venv/bin/python -c "
import sys,json; d=json.load(sys.stdin)
print(' tasks:', len(d['tasks']), '| progress:', d['progress'])
print(' guardrails:', [g['id'] for g in d['guardrails']])
"

echo "=== 2. save profile ==="
curl -s -X POST "$B/api/profile" -H "Content-Type: application/json" \
  -d '{"path":"both","niche":"ai-ecom","has_email_list":false,"uses_link_shortener":false,"has_disclosure":false}' \
  | .venv/bin/python -c "import sys,json;print(' saved:',json.load(sys.stdin)['ok'])"

echo "=== 3. guardrails fire on no-email / no-disclosure ==="
curl -s "$B/api/guardrails" | .venv/bin/python -c "
import sys,json
for g in json.load(sys.stdin)['guardrails']: print(f\"  [{g['level']}] {g['message'][:66]}\")
"

echo "=== 4. FCA finance trap ==="
curl -s -X POST "$B/api/profile" -H "Content-Type: application/json" -d '{"niche":"finance"}' >/dev/null
curl -s "$B/api/guardrails" | .venv/bin/python -c "
import sys,json
d=[g for g in json.load(sys.stdin)['guardrails'] if g['id']=='finance-risk']
print('  FCA trap fires:', bool(d))
"
curl -s -X POST "$B/api/profile" -H "Content-Type: application/json" -d '{"niche":"ai-ecom"}' >/dev/null

echo "=== 5. task toggle + metrics ==="
curl -s -X POST "$B/api/tasks/w1-niche/done" -H "Content-Type: application/json" -d '{"done":true}' >/dev/null
curl -s -X POST "$B/api/metrics" -H "Content-Type: application/json" -d '{"kind":"clicks","value":42}' >/dev/null
curl -s -X POST "$B/api/metrics" -H "Content-Type: application/json" -d '{"kind":"content","value":35}' >/dev/null
curl -s "$B/api/state" | .venv/bin/python -c "
import sys,json; d=json.load(sys.stdin)
print('  progress:', d['progress'])
print('  metrics:', {k:v['total'] for k,v in d['metrics'].items()})
print('  quitting-window fires:', any(g['id']=='quitting-window' for g in d['guardrails']))
"

echo "=== 6. assertions ==="
.venv/bin/python - <<'PY'
import json,urllib.request
def get(p): return json.load(urllib.request.urlopen("http://127.0.0.1:8790"+p))
s=get("/api/state")
assert s["progress"]["done"]>=1
assert s["metrics"]["clicks"]["total"]==42
assert s["metrics"]["content"]["total"]==35
assert any(g["id"]=="quitting-window" for g in s["guardrails"])
assert any(g["id"]=="no-email" for g in s["guardrails"])
assert len(s["tasks"])>20
print("  ALL CONSOLE ASSERTIONS PASSED")
PY

echo "=== 7. endpoints ==="
for ep in /api/tasks /api/metrics /api/guardrails /api/events / /api/copilot; do
  if [ "$ep" = "/api/copilot" ]; then
    code=$(curl -s -o /dev/null -w "%{http_code}" -X POST "$B$ep" -H "Content-Type: application/json" -d '{"q":"what next"}')
  else
    code=$(curl -s -o /dev/null -w "%{http_code}" "$B$ep")
  fi
  echo "  $ep -> $code"
done
echo "CONSOLE TEST DONE"
