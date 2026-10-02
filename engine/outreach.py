"""
Outreach engine: turn the Service Kit into actual paying work.

The Service Kit describes the offer. This tracks the people you SEND it to, and
generates compliant, non-spammy messages that don't get you banned from the
communities in 06-where-to-find-clients.md.

Design decisions that matter:

1. **UK PECR compliance is structural, not optional — and the rule is
   counter-intuitive.** Per ICO guidance (verified 2026-10-02):
     - **Corporate subscribers** (limited companies, LLPs): unsolicited email is
       LAWFUL without consent, provided you identify yourself, give a valid
       address, and honour objections.
     - **Individual subscribers** (sole traders, ordinary partnerships): prior
       CONSENT or a soft opt-in is REQUIRED. Cold email is unlawful.
   This inverts the naive assumption. An earlier version of this file had it
   backwards — it blocked companies (allowed) and permitted sole traders
   (not allowed), steering the user toward the riskier channel while appearing
   to protect them. Etsy sellers are overwhelmingly sole traders, so for this
   lead pool the email channel is largely CLOSED — reach them via their own
   site's contact form or social DM instead.
   The generator therefore BLOCKS email to an individual subscriber, and allows
   it to a corporate subscriber only with the identification elements present.

2. **No spray-and-pray.** Every platform in the kit bans bulk DMs. So there is a
   per-day cap per channel, a suppression list (one "no" = permanently suppressed),
   and a warm-up tracker — the communities explicitly require you to help first.

3. **Nothing is auto-sent.** This generates drafts and records what you sent, by
   hand. There is no send API for cold outreach. That is deliberate: bulk-sending is
   what gets accounts banned and, for email, is a PECR breach.

Usage:
  python -m engine.outreach --add-lead "Jane,Copper Lane Jewellery,etsy,https://etsy.com/shop/copperlane"
  python -m engine.outreach --import-leads leads.csv
  python -m engine.outreach --list
  python -m engine.outreach --draft 3 --channel email
  python -m engine.outreach --log 3 --channel email
  python -m engine.outreach --due
  python -m engine.outreach --stats
"""
import argparse
import csv
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

DB_PATH = os.environ.get("OUTREACH_DB",
                         str(Path(__file__).resolve().parent.parent / "outreach.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  name TEXT,
  business TEXT,
  channel TEXT NOT NULL,
  handle TEXT,
  niche TEXT,
  product_count INTEGER,
  notes TEXT,
  is_limited_company INTEGER DEFAULT 0,
  source TEXT,
  created_ts INTEGER
);

CREATE TABLE IF NOT EXISTS touches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  lead_id INTEGER NOT NULL REFERENCES leads(id),
  channel TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT,
  sent_ts INTEGER,
  status TEXT DEFAULT 'sent',
  created_ts INTEGER
);
CREATE INDEX IF NOT EXISTS ix_touches_lead ON touches(lead_id);
CREATE INDEX IF NOT EXISTS ix_touches_ts ON touches(sent_ts);

CREATE TABLE IF NOT EXISTS suppression (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT,
  handle TEXT,
  reason TEXT,
  added_ts INTEGER,
  UNIQUE(channel, handle)
);

CREATE TABLE IF NOT EXISTS warmup (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  channel TEXT NOT NULL,
  community TEXT,
  helpful_posts INTEGER DEFAULT 0,
  last_ts INTEGER,
  UNIQUE(channel, community)
);

CREATE TABLE IF NOT EXISTS settings (
  k TEXT PRIMARY KEY, v TEXT
);
"""

# Per-channel daily caps. Deliberately low: these are the levels at which a new
# account does not get flagged. Exceeding them is how accounts die.
DAILY_CAPS = {"email": 15, "reddit": 5, "facebook": 10, "discord": 8,
              "instagram": 10, "etsy": 10, "default": 8}

WARMUP_REQUIRED = {"reddit": 5, "facebook": 3, "discord": 4, "default": 2}

FOLLOWUP_DAYS = [3, 7, 14]
MAX_TOUCHES = 3


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
    finally:
        conn.close()


def now_s():
    return int(time.time())


def setting(k, default=None):
    conn = get_db()
    try:
        r = conn.execute("SELECT v FROM settings WHERE k=?", (k,)).fetchone()
        return r["v"] if r else default
    finally:
        conn.close()


def set_setting(k, v):
    conn = get_db()
    try:
        conn.execute("INSERT INTO settings(k,v) VALUES(?,?) "
                     "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, str(v)))
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------- suppression

def suppress(channel, handle, reason="opt-out"):
    """One 'no' means never again. Legal requirement for email; survival elsewhere."""
    conn = get_db()
    try:
        conn.execute("INSERT OR REPLACE INTO suppression(channel,handle,reason,added_ts) "
                     "VALUES(?,?,?,?)", (channel, handle, reason, now_s()))
        conn.commit()
    finally:
        conn.close()


def is_suppressed(channel, handle):
    if not handle:
        return False
    conn = get_db()
    try:
        r = conn.execute("SELECT 1 FROM suppression WHERE channel=? AND handle=?",
                         (channel, handle)).fetchone()
        return bool(r)
    finally:
        conn.close()


# ---------------------------------------------------------------- leads

def add_lead(name="", business="", channel="etsy", handle="", niche="", notes="",
             product_count=None, is_limited_company=False, source=""):
    conn = get_db()
    try:
        if handle and is_suppressed(channel, handle):
            return None
        # Dedupe on handle: a finder run across several niches re-finds the same shop
        # repeatedly (one shop showed up 5 times), which would inflate the pipeline and
        # make you contact the same person twice.
        if handle:
            dup = conn.execute("SELECT id FROM leads WHERE channel=? AND handle=? LIMIT 1",
                               (channel, handle)).fetchone()
            if dup:
                return None
        cur = conn.execute(
            "INSERT INTO leads(name,business,channel,handle,niche,product_count,notes,"
            "is_limited_company,source,created_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (name, business, channel, handle, niche, product_count, notes,
             1 if is_limited_company else 0, source, now_s()))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def import_leads(path):
    """CSV columns: name,business,channel,handle,niche,product_count,is_limited_company,source"""
    added = skipped = 0
    with open(path, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            ch = (row.get("channel") or "etsy").strip()
            h = (row.get("handle") or "").strip()
            ltd = str(row.get("is_limited_company", "")).strip().lower() in ("1", "true", "yes", "y")
            pc = row.get("product_count")
            try:
                pc = int(pc) if pc not in (None, "") else None
            except ValueError:
                pc = None
            lid = add_lead(
                name=(row.get("name") or "").strip(),
                business=(row.get("business") or "").strip(),
                channel=ch, handle=h,
                niche=(row.get("niche") or "").strip(),
                product_count=pc, is_limited_company=ltd,
                source=(row.get("source") or "csv").strip())
            if lid:
                added += 1
            else:
                skipped += 1
    return added, skipped


def list_leads(channel=None, limit=100):
    conn = get_db()
    try:
        q = ("SELECT l.*, "
             "(SELECT COUNT(*) FROM touches t WHERE t.lead_id=l.id) AS touches, "
             "(SELECT MAX(sent_ts) FROM touches t WHERE t.lead_id=l.id) AS last_ts, "
             "(SELECT status FROM touches t WHERE t.lead_id=l.id "
             " ORDER BY id DESC LIMIT 1) AS last_status "
             "FROM leads l")
        args = []
        if channel:
            q += " WHERE l.channel=?"
            args.append(channel)
        q += " ORDER BY l.id DESC LIMIT ?"
        args.append(limit)
        return [dict(r) for r in conn.execute(q, args).fetchall()]
    finally:
        conn.close()


def get_lead(lid):
    conn = get_db()
    try:
        r = conn.execute("SELECT * FROM leads WHERE id=?", (lid,)).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def lead_touches(lid):
    conn = get_db()
    try:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM touches WHERE lead_id=? ORDER BY id", (lid,)).fetchall()]
    finally:
        conn.close()


def log_touch(lead_id, channel, kind, body="", status="sent"):
    conn = get_db()
    try:
        conn.execute("INSERT INTO touches(lead_id,channel,kind,body,sent_ts,status,created_ts)"
                     " VALUES(?,?,?,?,?,?,?)",
                     (lead_id, channel, kind, body, now_s(), status, now_s()))
        conn.commit()
    finally:
        conn.close()
    if status in ("declined", "opt-out", "suppressed"):
        lead = get_lead(lead_id)
        if lead and lead.get("handle"):
            suppress(channel, lead["handle"], status)


def touches_today(channel):
    conn = get_db()
    try:
        midnight = int(datetime.now(timezone.utc).replace(
            hour=0, minute=0, second=0, microsecond=0).timestamp())
        r = conn.execute("SELECT COUNT(*) c FROM touches WHERE channel=? AND sent_ts>=?",
                         (channel, midnight)).fetchone()
        return r["c"]
    finally:
        conn.close()


def can_send(channel):
    """Cap check. Returns (ok, message)."""
    cap = DAILY_CAPS.get(channel, DAILY_CAPS["default"])
    n = touches_today(channel)
    if n >= cap:
        return False, f"DAILY CAP REACHED for {channel} ({n}/{cap})"
    return True, f"{n}/{cap} sent today"


# ---------------------------------------------------------------- warm-up

def warmup_count(channel, community):
    conn = get_db()
    try:
        r = conn.execute("SELECT helpful_posts FROM warmup WHERE channel=? AND community=?",
                         (channel, community)).fetchone()
        return r["helpful_posts"] if r else 0
    finally:
        conn.close()


def log_warmup(channel, community, n=1):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO warmup(channel,community,helpful_posts,last_ts) VALUES(?,?,?,?) "
            "ON CONFLICT(channel,community) DO UPDATE SET "
            "helpful_posts=helpful_posts+excluded.helpful_posts, last_ts=excluded.last_ts",
            (channel, community, n, now_s()))
        conn.commit()
    finally:
        conn.close()
    return warmup_count(channel, community)


def warmup_ok(channel, community):
    need = WARMUP_REQUIRED.get(channel, WARMUP_REQUIRED["default"])
    have = warmup_count(channel, community)
    return have >= need, have, need


# ---------------------------------------------------------------- messages

def draft(lead, channel, kind="first", sender=None):
    """Compose a message. Returns (text, [problems]).

    Refuses (returns problems) when a required compliance element is missing, rather
    than emitting a message that gets you fined or banned."""
    sender = sender or {
        "name": setting("sender_name", ""),
        "business": setting("sender_business", ""),
        "address": setting("sender_address", ""),
        "site": setting("sender_site", ""),
        "portfolio": setting("sender_portfolio", ""),
    }
    problems = []
    who = lead.get("name") or "there"
    biz = lead.get("business") or "your shop"
    niche = lead.get("niche") or "products"
    n = lead.get("product_count")

    if channel == "email":
        if not sender["name"]:
            problems.append("sender_name not set (PECR requires identifying yourself)")
        if not sender["address"]:
            problems.append("sender_address not set (PECR requires a real postal address)")
        # The counter-intuitive part. ICO: consent is required for INDIVIDUAL
        # subscribers (sole traders / ordinary partnerships); corporate subscribers
        # (limited companies) may be cold-emailed without consent. We have no
        # consent for anyone, so sole traders are blocked, companies are not.
        if not lead.get("is_limited_company"):
            problems.append(
                "lead is an INDIVIDUAL subscriber (sole trader / ordinary partnership). "
                "ICO requires PRIOR CONSENT or a soft opt-in to cold-email them, and you "
                "have neither. Do NOT send. Reach them via their own site's contact form "
                "or a social DM instead — that is where this lead pool is reachable.")

    body = {
        "email": (
            f"Subject: {biz} — {n or 'a few'} product photos, done without a photoshoot\n\n"
            f"Hi {who},\n\n"
            f"I found {biz} while looking at {'UK ' if niche else ''}{niche} sellers.\n\n"
            f"I make AI-generated product images and short marketing videos for small "
            f"sellers. Instead of a studio, you send me the product; I return 10-30 "
            f"polished images plus a 15-30 second video you can use on your listings and "
            f"socials. No samples to post, no studio booking.\n\n"
            f"Cost is £75-£400 depending on how many products. I can do two or three "
            f"images of your first product free so you can judge the quality before "
            f"paying anything.\n\n"
            f"{'Portfolio: ' + sender['portfolio'] + chr(10) if sender['portfolio'] else ''}"
            f"Want a free sample on one of your products? Just reply and I'll do one.\n\n"
            f"Best,\n{sender['name']}\n{sender['business']}\n{sender['address']}\n"
            + (f"{sender['site']}\n" if sender['site'] else "")
            + "\nIf you'd rather not hear from me again, reply 'no' and I'll remove "
              "your details immediately."
        ),
        "etsy": (
            f"Hi {who} — I came across {biz} and the {niche} pieces caught my eye. I make "
            f"AI product photos and short videos for small sellers and I'm looking for a "
            f"couple of UK shops to work with at a discount in exchange for feedback. "
            f"Would you be open to me doing a free sample image of one of your products so "
            f"you can see the quality? No strings, and no obligation to buy anything."
        ),
        "reddit": (
            f"This is genuinely useful rather than a pitch, so take it or leave it: on "
            f"{niche} shots the single biggest improvement is bouncing light from below "
            f"with a white foam board — costs about £3 and kills the hard shadows that "
            f"make pieces look flat. That alone is most of the difference people are "
            f"paying studios for.\n\n"
            f"(I do AI product photos and short videos for small sellers — happy to show a "
            f"free sample on one of your products if it's ever useful. But the foam board "
            f"tip will do you fine on its own.)"
        ),
        "facebook": (
            f"Hi {who}! Your {niche} listings caught my eye. Quick tip that costs nothing: "
            f"shoot on a window-sill in indirect daylight with a plain sheet of paper behind "
            f"the product — it fixes the yellow cast most sellers get.\n\n"
            f"I make AI product images and short marketing videos for small sellers "
            f"(£75-£400 a set). If you'd like to see what your own product would look like, "
            f"send me one photo and I'll do a free sample — no obligation at all."
        ),
        "discord": (
            f"Hey {who} — happy to give a free critique of one product photo if you post "
            f"it, no pitch. I do AI product photography and short videos for small sellers, "
            f"so I spot the usual issues fast (mostly lighting and white balance)."
        ),
        "instagram": (
            f"Hi {who}! I came across {biz} while looking at UK {niche} sellers. I make AI "
            f"product photos and short videos for small sellers and I'm doing a few free "
            f"samples for UK shops this month. Want me to do one of yours so you can see "
            f"whether it's useful?"
        ),
    }.get(channel)
    if body is None:
        problems.append(f"no template for channel '{channel}'")

    if kind == "followup" and body:
        body = (
            f"Hi {who} — I sent a note a few days ago about product photos for {biz} and "
            f"didn't want it to get lost. Short version: I make AI product images and "
            f"short videos for small sellers, and I'll do a free sample on one of your "
            f"products so you can judge the quality with zero risk. If it's not for you, "
            f"just say and I'll leave you alone."
            + (f"\n\n{sender['name']}" if channel == "email" else "")
        )
    elif kind == "warmup":
        body = (f"[WARM-UP ONLY — do NOT pitch. Contribute something genuinely helpful "
                f"to the community and log it. Communities in the kit require several "
                f"helpful posts before any mention of your service.]")

    return body or "", problems


def due_followups():
    """Leads with no reply who are due the next chase."""
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT l.*, "
            "(SELECT COUNT(*) FROM touches t WHERE t.lead_id=l.id) AS touches, "
            "(SELECT MAX(sent_ts) FROM touches t WHERE t.lead_id=l.id) AS last_ts, "
            "(SELECT status FROM touches t WHERE t.lead_id=l.id "
            " ORDER BY id DESC LIMIT 1) AS last_status "
            "FROM leads l").fetchall()
    finally:
        conn.close()
    now = now_s()
    out = []
    for r in rows:
        r = dict(r)
        if r["touches"] == 0 or r["touches"] >= MAX_TOUCHES:
            continue
        if r["last_status"] in ("replied", "won", "declined", "opt-out", "suppressed"):
            continue
        if is_suppressed(r["channel"], r.get("handle")):
            continue
        days = (now - (r["last_ts"] or now)) / 86400.0
        if days >= FOLLOWUP_DAYS[min(r["touches"] - 1, len(FOLLOWUP_DAYS) - 1)]:
            r["days_since"] = round(days, 1)
            out.append(r)
    return out


def stats():
    conn = get_db()
    try:
        out = {}
        out["leads"] = conn.execute("SELECT COUNT(*) c FROM leads").fetchone()["c"]
        out["touches"] = conn.execute("SELECT COUNT(*) c FROM touches").fetchone()["c"]
        out["suppressed"] = conn.execute("SELECT COUNT(*) c FROM suppression").fetchone()["c"]
        out["by_status"] = {r["status"]: r["c"] for r in conn.execute(
            "SELECT status, COUNT(*) c FROM touches GROUP BY status").fetchall()}
        out["by_channel"] = {r["channel"]: r["c"] for r in conn.execute(
            "SELECT channel, COUNT(*) c FROM leads GROUP BY channel").fetchall()}
        out["replied_leads"] = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) c FROM touches WHERE status='replied'"
        ).fetchone()["c"]
        out["won"] = conn.execute(
            "SELECT COUNT(DISTINCT lead_id) c FROM touches WHERE status='won'"
        ).fetchone()["c"]
        return out
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--init", action="store_true")
    ap.add_argument("--add-lead", help='"Name,Business,channel,handle" (comma-separated)')
    ap.add_argument("--import-leads", help="CSV file")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--channel", default=None)
    ap.add_argument("--draft", type=int, help="lead id to draft a message for")
    ap.add_argument("--log", type=int, help="lead id you actually sent to")
    ap.add_argument("--kind", default="first", choices=["first", "followup", "warmup"])
    ap.add_argument("--status", default="sent")
    ap.add_argument("--suppress", help='"channel,handle"')
    ap.add_argument("--warmup", help='"channel,community" — log a helpful post')
    ap.add_argument("--due", action="store_true", help="leads due a follow-up")
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--sender", help='"Name,Business,Address,Site,Portfolio"')
    a = ap.parse_args()

    init_db()

    if a.init:
        print(f"initialised {DB_PATH}")
        return

    if a.sender:
        parts = (a.sender.split(",") + ["", "", "", "", ""])[:5]
        for k, v in zip(("sender_name", "sender_business", "sender_address",
                         "sender_site", "sender_portfolio"), parts):
            set_setting(k, v.strip())
        print("sender details saved")
        return

    if a.add_lead:
        parts = (a.add_lead.split(",") + ["", "", "", ""])[:4]
        lid = add_lead(name=parts[0].strip(), business=parts[1].strip(),
                       channel=(parts[2].strip() or "etsy"), handle=parts[3].strip())
        print(f"lead {lid} added" if lid else "SKIPPED (suppressed)")
        return

    if a.import_leads:
        added, skipped = import_leads(a.import_leads)
        print(f"imported {added}, skipped {skipped} (suppressed)")
        return

    if a.suppress:
        ch, h = (a.suppress.split(",", 1) + [""])[:2]
        suppress(ch.strip(), h.strip(), "manual")
        print(f"suppressed {ch.strip()}/{h.strip()} — never contact again")
        return

    if a.warmup:
        ch, com = (a.warmup.split(",", 1) + [""])[:2]
        n = log_warmup(ch.strip(), com.strip())
        ok, have, need = warmup_ok(ch.strip(), com.strip())
        print(f"{ch}/{com}: {n} helpful posts ({'READY' if ok else f'need {need}'})")
        return

    if a.list:
        leads = list_leads(a.channel)
        if not leads:
            print("no leads yet")
        for l in leads:
            print(f"  {l['id']:4d}  {l['channel']:9s}  {(l['business'] or l['name'] or '?')[:26]:26s} "
                  f"niche={l.get('niche') or '-':10s} touches={l['touches']} "
                  f"last={l.get('last_status') or '-'}"
                  + ("  [LTD]" if l.get("is_limited_company") else ""))
        return

    if a.stats:
        s = stats()
        print(f"  leads: {s['leads']}   touches: {s['touches']}   suppressed: {s['suppressed']}")
        print(f"  replied: {s['replied_leads']}   won: {s['won']}")
        print(f"  by channel: {s['by_channel']}")
        print(f"  by status:  {s['by_status']}")
        return

    if a.due:
        due = due_followups()
        if not due:
            print("  nobody is due a follow-up")
        for l in due:
            print(f"  {l['id']:4d}  {l['channel']:9s}  {(l['business'] or l['name'] or '?')[:26]:26s} "
                  f"{l['days_since']}d since touch {l['touches']}")
        return

    if a.draft:
        lead = get_lead(a.draft)
        if not lead:
            raise SystemExit(f"no lead {a.draft}")
        ch = a.channel or lead["channel"]
        body, problems = draft(lead, ch, a.kind)
        print(body or "(no message generated)")
        if problems:
            print("\n" + "!" * 60)
            for p in problems:
                print(f"  BLOCKED: {p}")
            print("!" * 60)
        ok, msg = can_send(ch)
        print(f"\n  [{msg}]" + ("" if ok else "  <-- DO NOT SEND ANY MORE TODAY"))
        return

    if a.log:
        lead = get_lead(a.log)
        if not lead:
            raise SystemExit(f"no lead {a.log}")
        ch = a.channel or lead["channel"]
        ok, msg = can_send(ch)
        if not ok and a.status == "sent":
            raise SystemExit(f"REFUSING: {msg}. Sending anyway is how accounts get banned.")
        log_touch(a.log, ch, a.kind, "", a.status)
        print(f"logged {a.kind} to lead {a.log} on {ch} [{a.status}]")
        if a.status in ("declined", "opt-out"):
            print("  lead suppressed permanently")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
