#!/usr/bin/env bash
# Test the console's new engine/jobs endpoints + UI render.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8791
export CONSOLE_DB="$PWD/console_test2.db"
rm -f console_test2.db console_test2.db-wal console_test2.db-shm

.venv/bin/python -m uvicorn console.app:app --port $PORT --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 40); do curl -s "http://127.0.0.1:$PORT/api/state" >/dev/null 2>&1 && break; sleep 0.3; done
B="http://127.0.0.1:$PORT"

echo "=== UI renders (macOS shell markers) ==="
curl -s "$B/" | grep -o -E "titlebar|traffic|lights|navitem|Content Studio|Service Kit|Co-pilot" | sort -u | head
echo "  html bytes: $(curl -s "$B/" | wc -c)"

echo "=== new endpoints ==="
for ep in "/api/models" "/api/content-packs" "/api/service-kits" "/api/jobs"; do
  printf "  %-22s -> %s\n" "$ep" "$(curl -s -o /dev/null -w '%{http_code}' "$B$ep")"
done

echo "=== LIVE program search (openaffiliate.dev) ==="
curl -s --max-time 25 "$B/api/programs?q=AI%20video&limit=5" | .venv/bin/python -c "
import sys,json
d=json.load(sys.stdin)
print('  count:',d['count'])
for p in d['programs'][:5]:
    print(f\"  {p['name']:18s} {str(p['commission_rate']):6s} {str(p['commission_type']):10s} cookie {p['cookie_days']}d score {p['score']}\")
" 2>&1 | head -10

echo "=== doc read (existing pack) ==="
curl -s "$B/api/doc?base=content_out&sub=how-to-run-stable-diffusion-locally-on-an-apple-silicon-mac&file=script.md" \
  | .venv/bin/python -c "import sys,json;print('  chars:',len(json.load(sys.stdin)['content']))"

echo "=== path traversal blocked ==="
code=$(curl -s -o /dev/null -w '%{http_code}' "$B/api/doc?base=content_out&sub=..&file=../MASTERPLAN.md")
echo "  traversal -> $code (expect 404)"

echo "=== job start (service kit, then cancel check) ==="
JID=$(curl -s -X POST "$B/api/jobs/service-kit" -H "Content-Type: application/json" \
  -d '{"service":"TEST KIT smoke","niche":"test","price_range":"£10-£20"}' \
  | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['job_id'])")
echo "  job_id=$JID"
sleep 3
curl -s "$B/api/jobs/$JID" | .venv/bin/python -c "import sys,json;j=json.load(sys.stdin);print('  status:',j['status'],'| kind:',j['kind'])"

echo "=== bad input rejected ==="
printf "  missing fields -> %s\n" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/api/jobs/content" -H 'Content-Type: application/json' -d '{}')"
printf "  bad audit dir  -> %s\n" "$(curl -s -o /dev/null -w '%{http_code}' -X POST "$B/api/jobs/audit" -H 'Content-Type: application/json' -d '{}')"
echo "CONSOLE V2 TEST DONE"
