#!/usr/bin/env bash
# End-to-end test of the affiliate tracker: redirect → click logged → postback → report.
set -euo pipefail
cd "$(dirname "$0")"
PORT=8788
export TRACKER_DB="$PWD/tracker_test.db"
export TRACKER_BASE_URL="http://127.0.0.1:$PORT"
rm -f "$TRACKER_DB" "$TRACKER_DB-wal" "$TRACKER_DB-shm"

echo "== init + seed =="
.venv/bin/python -m tracker.cli init
.venv/bin/python -m tracker.cli seed | tee /tmp/tracker_seed.txt
TOKEN=$(grep -o 'r/[A-Za-z0-9]*' /tmp/tracker_seed.txt | head -1 | cut -d/ -f2)
echo "token=$TOKEN"

echo "== start server =="
.venv/bin/python -m uvicorn tracker.app:app --port $PORT --log-level warning &
SRV=$!
trap 'kill $SRV 2>/dev/null || true' EXIT
for i in $(seq 1 30); do
  curl -s "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1 && break
  sleep 0.3
done
curl -s "http://127.0.0.1:$PORT/healthz" || { echo "SERVER FAILED"; exit 1; }
echo

echo "== 3 clicks (one with creative tags), browser UA =="
UA="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
for i in 1 2 3; do
  curl -s -o /dev/null -A "$UA" -D - "http://127.0.0.1:$PORT/r/$TOKEN?creative=hook-0$i&utm_source=tiktok" \
    | grep -i '^location:' | sed 's/^/  LOCATION /'
done

echo "== extract click_ids from DB =="
CLICK_ID=$(sqlite3 "$TRACKER_DB" "SELECT click_id FROM clicks ORDER BY ts LIMIT 1;")
echo "click_id=$CLICK_ID"

echo "== postback: sale, payout 25 GBP =="
curl -s "http://127.0.0.1:$PORT/pb?subid=$CLICK_ID&status=sale&payout=25&currency=GBP&txid=TX1"; echo
echo "== postback replay (must be idempotent, no double count) =="
curl -s "http://127.0.0.1:$PORT/pb?subid=$CLICK_ID&status=sale&payout=25&currency=GBP&txid=TX1"; echo
echo "== postback with alias 'clickid' + 'p' =="
CLICK2=$(sqlite3 "$TRACKER_DB" "SELECT click_id FROM clicks ORDER BY ts LIMIT 1 OFFSET 1;")
curl -s "http://127.0.0.1:$PORT/pb?clickid=$CLICK2&status=lead&p=10&currency=USD"; echo

echo "== bot should be filtered (not counted) =="
curl -s -o /dev/null -A "Googlebot/2.1" "http://127.0.0.1:$PORT/r/$TOKEN?creative=hook-01"

echo
echo "== ClickBank: sub-id must ride in 'tid', placeholder must be substituted =="
CB_OFFER_ID=$(curl -s -X POST "http://127.0.0.1:$PORT/api/offers" -H 'Content-Type: application/json' \
  -d '{"name":"CB regression","network":"clickbank","offer_url":"https://hop.clickbank.net/?affiliate=russTEST&vendor=v&tid={clickid}"}' \
  | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['offer_id'])")
CB_LINK=$(curl -s -X POST "http://127.0.0.1:$PORT/api/campaigns" -H 'Content-Type: application/json' \
  -d "{\"offer_id\":$CB_OFFER_ID,\"name\":\"cb-regression\",\"channel\":\"youtube\"}" \
  | .venv/bin/python -c "import sys,json;print(json.load(sys.stdin)['link'])")
CB_LOC=$(curl -s -D - -o /dev/null "$CB_LINK" -A "$UA" | grep -i '^location:' | sed 's/^[Ll]ocation: //' | tr -d '\r\n')
echo "  LOCATION $CB_LOC"
CB_TID=$(echo "$CB_LOC" | sed 's/.*tid=\([^&]*\).*/\1/')
echo "  tid=$CB_TID"
# the literal placeholder must NOT survive, and a tid must be present
case "$CB_LOC" in
  *"{clickid}"*|*"%7Bclickid%7D"*) echo "FAIL: placeholder not substituted"; exit 1 ;;
esac
case "$CB_LOC" in
  *"tid=$CB_TID"*) : ;;
  *) echo "FAIL: no tid on the ClickBank URL"; exit 1 ;;
esac
# a ClickBank postback sends 'tid' — it must match the click
curl -s "http://127.0.0.1:$PORT/pb?tid=$CB_TID&status=CONVERSION&payout=47.50&txid=CBREG1"; echo

echo "== assertions =="
.venv/bin/python - <<'PY'
import os, sqlite3
db = os.environ["TRACKER_DB"]
c = sqlite3.connect(db); c.row_factory = sqlite3.Row
total = c.execute("SELECT COUNT(*) n FROM clicks").fetchone()["n"]
bots  = c.execute("SELECT COUNT(*) n FROM clicks WHERE is_bot=1").fetchone()["n"]
conv  = c.execute("SELECT COUNT(*) n, COALESCE(SUM(payout_gbp),0) r FROM conversions").fetchone()
rej   = c.execute("SELECT COUNT(*) n FROM conversions WHERE conv_type='rejected'").fetchone()["n"]
pb    = c.execute("SELECT COUNT(*) n FROM postback_events").fetchone()["n"]
print(f"clicks={total} bots={bots} conversions={conv['n']} revenue_gbp={conv['r']:.2f} postbacks_logged={pb}")
assert total == 5, f"expected 5 clicks logged (4 + 1 ClickBank), got {total}"
assert bots == 1, f"expected 1 bot click, got {bots}"
assert conv["n"] == 3, f"expected 3 conversions (2 idempotent + 1 ClickBank), got {conv['n']}"
assert abs(conv["r"] - 80.4) < 0.6, f"expected ~80.40 GBP (25 + 7.90 + 47.50), got {conv['r']}"
assert pb == 4, f"expected 4 postback events logged, got {pb}"
print("ALL ASSERTIONS PASSED")
PY

echo "== report by creative =="
curl -s "http://127.0.0.1:$PORT/api/report?groupby=creative_id" | .venv/bin/python -m json.tool
echo "== report by source =="
curl -s "http://127.0.0.1:$PORT/api/report?groupby=source" | .venv/bin/python -m json.tool
echo "ALL TESTS PASSED"
