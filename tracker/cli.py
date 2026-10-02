"""CLI for the tracker: init, seed a demo offer+campaign, report."""
import sys
from .db import init_db, get_db, now_ms
from .ulid import ulid
from .app import BASE_URL


def cmd_init():
    init_db()
    print("DB initialised.")


def cmd_seed_demo():
    init_db()
    conn = get_db()
    try:
        cur = conn.execute(
            """INSERT INTO offers(name,network,offer_url,default_payout,currency,
                 status_map_json,created_ts) VALUES(?,?,?,?,?,?,?)""",
            ("Demo Offer (placeholder)", "clickbank",
             "https://example.com/affiliate-offer", 25.0, "GBP", "{}", now_ms()),
        )
        offer_id = cur.lastrowid
        token = "demo" + ulid()[:6]
        conn.execute(
            """INSERT INTO campaigns(token,name,source,offer_id,creative_id,created_ts)
               VALUES(?,?,?,?,?,?)""",
            (token, "Demo Campaign", "tiktok", offer_id, "hook-01", now_ms()),
        )
        conn.commit()
        print(f"Demo offer_id={offer_id}")
        print(f"Test link: {BASE_URL}/r/{token}")
    finally:
        conn.close()


def cmd_report():
    init_db()
    conn = get_db()
    try:
        rows = conn.execute(
            """SELECT COALESCE(c.creative_id,'(none)') grp,
                      COUNT(DISTINCT c.click_id) clicks, COUNT(v.id) conv,
                      COALESCE(SUM(v.payout),0) rev
               FROM clicks c
               LEFT JOIN conversions v ON v.click_id=c.click_id
                    AND v.conv_type NOT IN ('rejected')
               WHERE c.is_bot=0 GROUP BY grp ORDER BY rev DESC"""
        ).fetchall()
        print(f"{'creative':20s} {'clicks':>8s} {'conv':>6s} {'revenue':>10s}")
        for r in rows:
            print(f"{r['grp']:20s} {r['clicks']:>8d} {r['conv']:>6d} {r['rev']:>10.2f}")
    finally:
        conn.close()


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "init"
    {"init": cmd_init, "seed": cmd_seed_demo, "report": cmd_report}[cmd]()
