"""
Minimal self-hosted affiliate click tracker (FastAPI, no Docker, SQLite).

Endpoints
  GET /r/{token}      redirect tracker: logs click, 302s to offer with sub1={click_id}
  GET /pb             postback receiver: subid|clickid|cid, status, payout|p, txid
  GET /px.gif         1x1 pixel fallback
  GET /healthz        liveness
  GET /api/report     JSON report (clicks/conversions/revenue by source,creative,...)
  GET /api/offers     list offers
  POST /api/offers    create offer
  GET /api/campaigns  list campaigns
  POST /api/campaigns create campaign (returns /r/{token} link)
  POST /api/import    import network CSV (reconciliation)
"""
import csv
import io
import json
import os
import re
import time
from typing import Optional

from fastapi import FastAPI, Request, Response, HTTPException
from fastapi.responses import RedirectResponse, JSONResponse

from .db import get_db, init_db, now_ms
from .ulid import ulid

app = FastAPI(title="affiliate-tracker", version="0.1.0")

BASE_URL = os.environ.get("TRACKER_BASE_URL", "http://127.0.0.1:8788")

# --- bot filter (cheap, no DB read) -------------------------------------------------
BOT_RE = re.compile(
    r"(bot|crawler|spider|slurp|bingpreview|facebookexternalhit|whatsapp|telegrambot|"
    r"headlesschrome|phantomjs|python-requests|curl/|wget/|go-http-client|"
    r"semrush|ahrefs|mj12|dotbot|petalbot|bytespider|gptbot|claudebot|ccbot|"
    r"lighthouse|pagespeed|pingdom|uptimerobot)",
    re.I,
)

# --- postback param aliases (accept any network's naming) ---------------------------
CLICKID_ALIASES = ("subid", "sub_id", "clickid", "click_id", "cid", "c", "s1", "aff_sub", "tid")
PAYOUT_ALIASES = ("payout", "p", "amount", "revenue", "commission")

DEFAULT_STATUS_MAP = {
    "sale": "sale", "billed": "sale", "approved": "sale", "complete": "sale",
    "lead": "lead", "new": "lead", "signup": "lead",
    "registered": "registration", "registration": "registration",
    "deposit": "deposit", "ftd": "deposit",
    "rejected": "rejected", "cancel": "rejected", "cancelled": "rejected",
    "chargeback": "rejected", "refund": "rejected", "trash": "rejected",
}

CURRENCY_TO_GBP = {  # static fallback rates; override with live rates if desired
    "GBP": 1.0, "USD": 0.79, "EUR": 0.85, "AUD": 0.52, "CAD": 0.58,
}


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    if fwd:
        return fwd.split(",")[0].strip()
    return request.client.host if request.client else ""


def device_of(ua: str) -> str:
    u = ua.lower()
    if "ipad" in u or "tablet" in u:
        return "tablet"
    if "mobi" in u or "android" in u or "iphone" in u:
        return "mobile"
    return "desktop"


def os_of(ua: str) -> str:
    u = ua.lower()
    for k, v in (("iphone", "iOS"), ("ipad", "iPadOS"), ("android", "Android"),
                 ("mac os", "macOS"), ("windows", "Windows"), ("linux", "Linux")):
        if k in u:
            return v
    return "unknown"


def browser_of(ua: str) -> str:
    u = ua.lower()
    for k, v in (("edg", "Edge"), ("chrome", "Chrome"), ("safari", "Safari"),
                 ("firefox", "Firefox")):
        if k in u:
            return v
    return "unknown"


def to_gbp(amount, cur):
    """Convert to GBP. Unknown currency raises (never silently assume 1:1 — that
    would corrupt financial data)."""
    try:
        amt = float(amount or 0)
    except (TypeError, ValueError):
        raise ValueError("invalid payout amount")
    c = (cur or "GBP").upper()
    if c not in CURRENCY_TO_GBP:
        raise ValueError(f"unsupported currency: {c}")
    return round(amt * CURRENCY_TO_GBP[c], 4)


def valid_http_url(u: str) -> bool:
    """Only allow http/https absolute URLs (blocks javascript:, data:, file:, etc.)."""
    if not isinstance(u, str) or len(u) > 2000:
        return False
    return re.match(r"^https?://[a-z0-9.-]+(?::\d+)?(?:/\S*)?$", u, re.I) is not None


# --- simple in-memory rate limiter (per IP, token bucket) -------------------------
_RL: dict = {}
_RATE_MAX = int(os.environ.get("TRACKER_RATE_MAX", "120"))   # requests
_RATE_WIN = float(os.environ.get("TRACKER_RATE_WIN", "60"))  # seconds


def rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _RL.get(ip, []) if now - t < _RATE_WIN]
    hits.append(now)
    _RL[ip] = hits
    if len(_RL) > 10000:  # bound memory
        for k in list(_RL)[:5000]:
            _RL.pop(k, None)
    return len(hits) > _RATE_MAX


def map_status(status: str, status_map: dict) -> str:
    s = (status or "").strip().lower()
    if not s:
        return "unknown"
    if s in status_map:
        return status_map[s]
    return DEFAULT_STATUS_MAP.get(s, "other")


@app.on_event("startup")
def _startup():
    init_db()


@app.get("/healthz")
def healthz():
    return {"ok": True, "ts": now_ms()}


@app.get("/r/{token}")
def redirect(token: str, request: Request):
    conn = get_db()
    try:
        camp = conn.execute(
            "SELECT * FROM campaigns WHERE token=?", (token,)
        ).fetchone()
        if not camp:
            raise HTTPException(status_code=404, detail="unknown campaign token")
        offer = conn.execute(
            "SELECT * FROM offers WHERE id=?", (camp["offer_id"],)
        ).fetchone()
        if not offer:
            raise HTTPException(status_code=500, detail="campaign has no offer")

        q = request.query_params
        ua = request.headers.get("user-agent", "")
        ip = client_ip(request)
        if rate_limited(ip):
            raise HTTPException(status_code=429, detail="rate limited")
        click_id = ulid()
        ua_hash = __import__("hashlib").sha1(f"{ip}|{ua}".encode()).hexdigest()[:20]

        is_bot = 1 if BOT_RE.search(ua) else 0
        # refresh/spam guard: same ua_hash + campaign within 5s
        recent = conn.execute(
            "SELECT 1 FROM clicks WHERE ua_hash=? AND campaign_id=? AND ts>? LIMIT 1",
            (ua_hash, camp["id"], now_ms() - 5000),
        ).fetchone()
        is_dupe = 1 if recent else 0

        creative = q.get("creative") or q.get("utm_content") or camp["creative_id"]
        source = q.get("utm_source") or camp["source"]
        ext_click = q.get("gclid") or q.get("fbclid") or q.get("ttclid")

        conn.execute(
            """INSERT INTO clicks(click_id,ts,campaign_id,source,creative_id,placement,
                 keyword,ip,ua,ua_hash,referrer,country,device_type,os,browser,
                 offer_id,target_url,ext_click_id,is_bot,is_dupe)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (click_id, now_ms(), camp["id"], source, creative, q.get("placement"),
             q.get("keyword"), ip, ua, ua_hash, request.headers.get("referer"),
             None, device_of(ua), os_of(ua), browser_of(ua), offer["id"],
             offer["offer_url"], ext_click, is_bot, is_dupe),
        )
        conn.commit()

        sep = "&" if "?" in offer["offer_url"] else "?"
        # Per-network sub-id param. ClickBank uses `tid`, not `sub1` — hardcoding sub1
        # meant a ClickBank postback could never match a click, silently losing every
        # conversion. Extend this map for new networks rather than guessing.
        net = (offer["network"] or "").lower()
        SUBID_PARAM = {"clickbank": "tid", "amazon": "ascsubtag"}.get(net, "sub1")
        base = offer["offer_url"]

        # Substitute any {clickid}/{subid}/{tid} placeholder the user put in the offer
        # URL. Without this the literal "{clickid}" was forwarded to the network and the
        # real click id only ever landed in the appended param.
        def _sub_url(u, cid):
            out = u
            for ph in ("{clickid}", "{click_id}", "{subid}", "{sub_id}", "{tid}"):
                out = out.replace(ph, cid)
            return out

        if SUBID_PARAM == "tid":
            base = _sub_url(base, click_id)
            # only append tid if the URL didn't already contain a substituted one
            tid_part = "" if "tid=" in base else f"&{SUBID_PARAM}={click_id}"
            dest = (f"{base}{tid_part}"
                    f"&sub1={click_id}&sub2={creative or ''}&sub3={q.get('placement','')}")
        else:
            dest = (f"{_sub_url(base, click_id)}{sep}sub1={click_id}"
                    f"&sub2={creative or ''}&sub3={q.get('placement','')}")
        return RedirectResponse(dest, status_code=302)
    finally:
        conn.close()


def _handle_postback(request: Request):
    conn = get_db()
    try:
        q = dict(request.query_params)
        ip = client_ip(request)
        raw = str(request.query_params)

        def pick(names):
            for n in names:
                if n in q and q[n] != "":
                    return q[n]
            return None

        click_id = pick(CLICKID_ALIASES)
        status = q.get("status") or q.get("s") or q.get("event")
        reason = None

        if not click_id:
            reason = "no_click_id"
        else:
            click = conn.execute(
                "SELECT * FROM clicks WHERE click_id=?", (click_id,)
            ).fetchone()
            if not click:
                reason = "click_not_found"
            else:
                offer = conn.execute(
                    "SELECT * FROM offers WHERE id=?", (click["offer_id"],)
                ).fetchone()
                smap = {}
                if offer and offer["status_map_json"]:
                    try:
                        smap = json.loads(offer["status_map_json"])
                    except Exception:
                        smap = {}
                ct = map_status(status, smap)
                try:
                    payout_gbp = to_gbp(pick(PAYOUT_ALIASES), q.get("currency"))
                    raw_payout = float(pick(PAYOUT_ALIASES) or 0)
                except ValueError:
                    payout_gbp, raw_payout = 0.0, 0.0
                    ct = "status:" + ct if not ct.startswith("status:") else ct
                conn.execute(
                    """INSERT OR IGNORE INTO conversions(click_id,txid,ts,received_ts,
                         status,conv_type,payout,payout_cur,payout_gbp,offer_id,
                         network,raw_query,ip)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (click_id, q.get("txid") or q.get("transaction_id") or "",
                     now_ms(), now_ms(), status, ct,
                     raw_payout, q.get("currency"),
                     payout_gbp, click["offer_id"],
                     offer["network"] if offer else None, raw, ip),
                )
                conn.commit()

        conn.execute(
            "INSERT INTO postback_events(ts,remote_ip,query,matched,reason) VALUES(?,?,?,?,?)",
            (now_ms(), ip, raw, 0 if reason else 1, reason or "ok"),
        )
        conn.commit()
        return Response("OK", media_type="text/plain")
    finally:
        conn.close()


@app.get("/pb")
def postback(request: Request):
    return _handle_postback(request)


@app.get("/px.gif")
def pixel(request: Request):
    _handle_postback(request)
    gif = (b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff!"
           b"\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
           b"\x00\x02\x02D\x01\x00;")
    return Response(content=gif, media_type="image/gif")


@app.get("/api/report")
def report(groupby: str = "creative_id", frm: Optional[int] = None,
           to: Optional[int] = None):
    allowed = {"source", "campaign_id", "creative_id", "placement", "keyword"}
    if groupby not in allowed:
        raise HTTPException(400, f"groupby must be one of {sorted(allowed)}")
    conn = get_db()
    try:
        where, args = ["c.is_bot=0"], []
        if frm:
            where.append("c.ts>=?"); args.append(frm)
        if to:
            where.append("c.ts<=?"); args.append(to)
        w = " AND ".join(where)
        rows = conn.execute(
            f"""SELECT COALESCE(c.{groupby},'(none)') AS grp,
                       COUNT(DISTINCT c.click_id) AS clicks,
                       COUNT(v.id) AS conversions,
                       COALESCE(SUM(v.payout),0) AS revenue
                FROM clicks c
                LEFT JOIN conversions v
                  ON v.click_id=c.click_id AND v.conv_type NOT IN ('rejected')
                WHERE {w}
                GROUP BY grp
                ORDER BY revenue DESC, clicks DESC""",
            args,
        ).fetchall()
        out = []
        for r in rows:
            clicks = r["clicks"] or 0
            conv = r["conversions"] or 0
            rev = r["revenue"] or 0
            out.append({
                "group": r["grp"],
                "clicks": clicks,
                "conversions": conv,
                "revenue_gbp": round(rev, 2),
                "cvr_pct": round(100 * conv / clicks, 2) if clicks else 0,
                "epc_gbp": round(rev / clicks, 4) if clicks else 0,
            })
        return {"groupby": groupby, "rows": out}
    finally:
        conn.close()


@app.get("/api/offers")
def list_offers():
    conn = get_db()
    try:
        rows = conn.execute("SELECT * FROM offers ORDER BY id").fetchall()
        return {"offers": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/offers")
def create_offer(payload: dict):
    name = payload.get("name")
    url = payload.get("offer_url")
    if not isinstance(name, str) or not name.strip() or len(name) > 200:
        raise HTTPException(400, "name is required (max 200 chars)")
    if not valid_http_url(url or ""):
        raise HTTPException(400, "offer_url must be an absolute http(s) URL")
    smap = payload.get("status_map", {})
    if not isinstance(smap, dict):
        raise HTTPException(400, "status_map must be an object")
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO offers(name,network,offer_url,default_payout,currency,
                 status_map_json,created_ts) VALUES(?,?,?,?,?,?,?)""",
            (name.strip(), payload.get("network"), url,
             float(payload.get("default_payout", 0) or 0),
             (payload.get("currency") or "GBP").upper(),
             json.dumps(smap), now_ms()),
        )
        conn.commit()
        return {"ok": True, "offer_id": cur.lastrowid}
    finally:
        conn.close()


@app.get("/api/campaigns")
def list_campaigns():
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT c.*, o.name AS offer_name,
                      ? || '/r/' || c.token AS link
               FROM campaigns c LEFT JOIN offers o ON o.id=c.offer_id
               ORDER BY c.id""", (BASE_URL,)).fetchall()
        return {"campaigns": [dict(r) for r in rows]}
    finally:
        conn.close()


@app.post("/api/campaigns")
def create_campaign(payload: dict):
    conn = get_db()
    try:
        token = payload.get("token") or ulid()[:10]
        cur = conn.execute(
            """INSERT INTO campaigns(token,name,source,offer_id,creative_id,created_ts)
               VALUES(?,?,?,?,?,?)""",
            (token, payload["name"], payload.get("source"),
             payload.get("offer_id"), payload.get("creative_id"), now_ms()),
        )
        conn.commit()
        return {"ok": True, "campaign_id": cur.lastrowid,
                "link": f"{BASE_URL}/r/{token}"}
    finally:
        conn.close()


@app.post("/api/import")
async def import_csv(request: Request):
    """Import a network CSV report for reconciliation.
    Expects at least: click_id (or subid), status, payout, optional txid.
    Per-row isolation: a malformed row is skipped and counted, never aborting the batch.
    """
    body = (await request.body()).decode("utf-8", "replace")
    reader = csv.DictReader(io.StringIO(body))
    conn = get_db()
    inserted = 0
    skipped = 0
    try:
        for row in reader:
            low = {k.lower().strip(): v for k, v in row.items() if k}
            cid = (low.get("click_id") or low.get("subid") or low.get("sub_id") or "").strip()
            if not cid:
                skipped += 1
                continue
            status = low.get("status", "")
            cur = (low.get("currency") or "GBP").upper()
            try:
                payout = float(low.get("payout") or 0)
                gbp = to_gbp(payout, cur)
            except ValueError:
                # unknown currency / bad number: still record the conversion, but
                # don't silently write a wrong GBP figure
                payout, gbp = 0.0, 0.0
                skipped += 1
            conn.execute(
                """INSERT OR IGNORE INTO conversions(click_id,txid,ts,received_ts,
                     status,conv_type,payout,payout_cur,payout_gbp,raw_query)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (cid, low.get("txid", ""), now_ms(), now_ms(), status,
                 map_status(status, {}), payout, cur, gbp, json.dumps(low)),
            )
            inserted += 1
        conn.commit()
        return {"ok": True, "imported": inserted, "skipped": skipped}
    finally:
        conn.close()
