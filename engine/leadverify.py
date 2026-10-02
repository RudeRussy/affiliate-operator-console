"""
Lead verification: turn an unverified name into a checked lead.

WHY THIS EXISTS
---------------
The finder produces shop names from a single search snippet. That is enough to rank
them, but NOT enough to act on: we never saw the shop. Etsy returns 403 and its
robots.txt disallows /search, so we cannot open the shop to look.

What we CAN do is search the index for the shop by name and read what comes back.
If the shop appears with its own title, description and location, it is real and
active. If nothing comes back, the name is probably stale or the snippet was noise,
and it should be dropped rather than contacted.

This is a verification step, not a source of truth about their photos — we still have
never seen those, and no message may claim otherwise.

Verification status written per lead:
  confirmed   the index returns the shop with its own listing/title
  weak        something comes back but it is not clearly this shop
  unverified  nothing found — do not contact
  error       the lookup failed (network/quota); retry, do not treat as unverified

Usage:
  python -m engine.leadverify                 # verify all unverified leads
  python -m engine.leadverify --limit 10
  python -m engine.leadverify --report        # summary only
"""
import argparse
import json
import os
import re
import sqlite3
import sys
import time
import urllib.parse
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _ensure_columns(conn):
    """Add verification columns if missing. Idempotent."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(leads)")}
    for name, sql in (("verify_status", "ALTER TABLE leads ADD COLUMN verify_status TEXT"),
                      ("verify_evidence", "ALTER TABLE leads ADD COLUMN verify_evidence TEXT"),
                      ("verify_ts", "ALTER TABLE leads ADD COLUMN verify_ts INTEGER")):
        if name not in cols:
            conn.execute(sql)
    conn.commit()


def _brave_lookup(shop, limit=5):
    """Search the index for this shop name. Returns list of results."""
    from engine.leadfinder import _brave
    # quote the shop name so we match the shop, not random words
    q = f'"{shop}" etsy shop'
    raw = _brave(q, limit)
    return raw.get("results", [])


def classify(shop, results):
    """Decide whether these results confirm the shop exists.

    Deliberately conservative: 'weak' rather than 'confirmed' when the evidence is
    ambiguous, because a false 'confirmed' sends you to DM a shop that may not exist.
    """
    if not results:
        return "unverified", ""
    shop_l = shop.lower()
    for r in results:
        url = (r.get("url") or "").lower()
        title = (r.get("title") or "").lower()
        desc = (r.get("description") or "").lower()
        # strongest evidence: the index has the shop's own page
        if f"/shop/{shop_l}" in url:
            return "confirmed", (r.get("description") or r.get("title") or "")[:300]
        # next: the name appears in a title about an etsy shop
        if shop_l.replace("-", " ") in title.replace("-", " ") and "etsy" in (url + title):
            return "confirmed", (r.get("description") or r.get("title") or "")[:300]
    # fall back: name appears anywhere with etsy context
    joined = " ".join((r.get("url", "") + " " + r.get("title", "") + " "
                       + r.get("description", "")) for r in results).lower()
    if shop_l.replace("-", " ") in joined.replace("-", " "):
        return "weak", joined[:200]
    return "unverified", joined[:200]


def verify_lead(shop, handle):
    """Return (status, evidence)."""
    try:
        results = _brave_lookup(shop)
    except Exception as exc:  # network/quota — must NOT be read as 'unverified'
        return "error", f"lookup failed: {exc}"
    return classify(shop, results)


def run(limit=None, sleep_s=1.1, report_only=False):
    from engine import outreach as O
    conn = O.get_db()
    _ensure_columns(conn)

    rows = conn.execute(
        "SELECT id,business,handle FROM leads "
        "WHERE verify_status IS NULL OR verify_status='' ORDER BY id"
        + (f" LIMIT {int(limit)}" if limit else "")
    ).fetchall()

    if report_only or not rows:
        return summarise(conn)

    print(f"verifying {len(rows)} leads (index lookups, ~{sleep_s}s apart)\n")
    counts = {}
    for r in rows:
        shop = r["business"] or ""
        if not shop:
            status, ev = "unverified", "no shop name stored"
        else:
            status, ev = verify_lead(shop, r["handle"])
        conn.execute("UPDATE leads SET verify_status=?,verify_evidence=?,verify_ts=? WHERE id=?",
                     (status, ev, int(time.time()), r["id"]))
        conn.commit()
        counts[status] = counts.get(status, 0) + 1
        mark = {"confirmed": "OK ", "weak": "?? ", "unverified": "-- ",
                "error": "!! "}.get(status, "?  ")
        print(f"  {mark} {shop[:34]:36s} {status}")
        if status in ("confirmed", "weak"):
            print(f"        {ev[:110]}")
        time.sleep(sleep_s)

    print()
    return summarise(conn)


def summarise(conn=None):
    from engine import outreach as O
    if conn is None:
        conn = O.get_db()
        _ensure_columns(conn)
    rows = conn.execute(
        "SELECT COALESCE(verify_status,'(not checked)') s, COUNT(*) n "
        "FROM leads GROUP BY s ORDER BY n DESC").fetchall()
    total = conn.execute("SELECT COUNT(*) n FROM leads").fetchone()["n"]
    print(f"  verification of {total} leads:")
    for r in rows:
        print(f"    {r['s']:14s} {r['n']}")
    return {r["s"]: r["n"] for r in rows}


def contacts(conn=None):
    """Which verified leads can actually be reached, and how."""
    from engine import outreach as O
    if conn is None:
        conn = O.get_db()
        _ensure_columns(conn)
    out = {"with_email": 0, "shop_url_only": 0}
    for r in conn.execute("SELECT handle FROM leads WHERE verify_status='confirmed'"):
        h = r["handle"] or ""
        if "@" in h:
            out["with_email"] += 1
        elif h:
            out["shop_url_only"] += 1
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.1)
    ap.add_argument("--contacts", action="store_true",
                    help="how the confirmed leads can be contacted")
    a = ap.parse_args()

    if a.contacts:
        c = contacts()
        print(f"  confirmed leads reachable by email:  {c['with_email']}")
        print(f"  confirmed leads with only a shop URL: {c['shop_url_only']}")
        print("  (Etsy shops must be messaged through Etsy — we hold no addresses)")
        return

    run(limit=a.limit, sleep_s=a.sleep, report_only=a.report)


if __name__ == "__main__":
    main()
