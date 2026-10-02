#!/usr/bin/env bash
# Test profile validation + LLM retry behaviour.
set -euo pipefail
cd "$(dirname "$0")"
export CONSOLE_DB="$PWD/console_vtest.db"
rm -f console_vtest.db console_vtest.db-wal console_vtest.db-shm

.venv/bin/python -m uvicorn console.app:app --port 8792 --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 40); do curl -s http://127.0.0.1:8792/api/state >/dev/null 2>&1 && break; sleep 0.3; done
B="http://127.0.0.1:8792"

code(){ curl -s -o /dev/null -w '%{http_code}' "$@"; }
echo "== profile validation =="
printf "  bad niche     -> %s (expect 400)\n" "$(code -X POST $B/api/profile -H 'Content-Type: application/json' -d '{"niche":"finance"}')"
printf "  bad path      -> %s (expect 400)\n" "$(code -X POST $B/api/profile -H 'Content-Type: application/json' -d '{"path":"nonsense"}')"
printf "  unknown field -> %s (expect 400)\n" "$(code -X POST $B/api/profile -H 'Content-Type: application/json' -d '{"evil":"x"}')"
printf "  valid profile -> %s (expect 200)\n" "$(code -X POST $B/api/profile -H 'Content-Type: application/json' -d '{"path":"both","niche":"ai-ecom"}')"
curl -s $B/api/state | .venv/bin/python -c "import sys,json;d=json.load(sys.stdin);p=d['profile'];print('  saved -> niche=%s path=%s' % (p.get('niche'),p.get('path')))"

echo "== assertions =="
.venv/bin/python - <<'PY'
import json,urllib.request,urllib.error
B="http://127.0.0.1:8792"
def post(p,d):
    r=urllib.request.Request(B+p,data=json.dumps(d).encode(),headers={"Content-Type":"application/json"})
    try:
        return json.load(urllib.request.urlopen(r)), 200
    except urllib.error.HTTPError as e:
        return None, e.code
_,c=post("/api/profile",{"niche":"finance"}); assert c==400, c
_,c=post("/api/profile",{"path":"nonsense"}); assert c==400, c
_,c=post("/api/profile",{"evil":"x"}); assert c==400, c
r,c=post("/api/profile",{"path":"both","niche":"ai-ecom"}); assert c==200, c
st=json.load(urllib.request.urlopen(B+"/api/state"))
assert st["profile"]["niche"]=="ai-ecom"
print("  ALL PROFILE VALIDATION ASSERTIONS PASSED")
PY

echo "== LLM retry (bad host must retry then fail, not hang) =="
.venv/bin/python - <<'PY'
import time
from engine.content_engine import _post_json
t=time.time()
try:
    _post_json("http://127.0.0.1:59999/api/chat", {"x":1}, timeout=2, attempts=3)
    print("  FAIL: should have raised")
except Exception as e:
    el=time.time()-t
    print(f"  retried and failed in {el:.1f}s ({type(e).__name__})")
    assert el >= 2, "should have backed off between attempts"
    print("  PASS retry+backoff then clean failure")
PY
echo "PROFILE/RETRY TEST DONE"
