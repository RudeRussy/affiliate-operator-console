"""
Tests for the outreach engine.

The compliance rules are the point: it must REFUSE to emit a message that would
breach UK PECR, and must never re-contact someone who opted out.
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.harness import check, temp_db, run_all  # noqa: E402

_tmp = temp_db()

from engine import outreach as O  # noqa: E402

O.init_db()


def test_lead_lifecycle():
    lid = O.add_lead(name="Jane", business="Copper Lane", channel="etsy",
                     handle="https://etsy.com/shop/copperlane", niche="jewellery")
    check("lead added", lid == 1, str(lid))
    l = O.get_lead(lid)
    check("lead retrievable", l["business"] == "Copper Lane")
    check("no touches yet", len(O.lead_touches(lid)) == 0)


def test_suppression_is_permanent():
    O.suppress("etsy", "https://etsy.com/shop/copperlane", "opt-out")
    check("suppressed flag set", O.is_suppressed("etsy", "https://etsy.com/shop/copperlane"))
    again = O.add_lead(name="Jane", business="Copper Lane", channel="etsy",
                       handle="https://etsy.com/shop/copperlane")
    check("suppressed lead cannot be re-added", again is None, str(again))


def test_decline_auto_suppresses():
    lid = O.add_lead(name="Bob", business="Bob's", channel="facebook", handle="fb.com/bobs")
    before = O.is_suppressed("facebook", "fb.com/bobs")
    O.log_touch(lid, "facebook", "first", "", "declined")
    after = O.is_suppressed("facebook", "fb.com/bobs")
    check("decline auto-suppresses", (not before) and after, f"{before}->{after}")


def test_email_requires_compliance_elements():
    # A LIMITED COMPANY (corporate subscriber) is the only case where cold email is
    # lawful without consent. Test the identification requirements on that case.
    lid = O.add_lead(name="Sarah", business="Sarah Designs Ltd", channel="email",
                     handle="sarah@example-ltd.co.uk", niche="homeware",
                     is_limited_company=True)
    _, problems = O.draft(O.get_lead(lid), "email")
    joined = " ".join(problems).lower()
    check("email blocks without sender identity (PECR)", "sender_name" in joined, str(problems))
    check("email blocks without a postal address", "sender_address" in joined, str(problems))

    O.set_setting("sender_name", "Russ Smith")
    O.set_setting("sender_address", "12 Example Street, London")
    body, problems2 = O.draft(O.get_lead(lid), "email")
    check("corporate-subscriber email clears once id+address set",
          not problems2, str(problems2))
    check("email contains an opt-out", "reply 'no'" in body, body[-120:])


def test_individual_subscriber_email_is_blocked():
    """REGRESSION (found by Kimi K3 review, verified against ICO guidance):
    the original gate was INVERTED. ICO: consent is required for INDIVIDUAL
    subscribers (sole traders / ordinary partnerships); corporate subscribers
    (limited companies) may be cold-emailed without consent. The old code blocked
    companies and permitted sole traders — exactly backwards, and Etsy sellers are
    overwhelmingly sole traders."""
    O.set_setting("sender_name", "Russ Smith")
    O.set_setting("sender_address", "12 Example Street, London")
    lid = O.add_lead(name="Jane", business="Copper Lane Jewellery", channel="email",
                     handle="jane@copperlane.co.uk", niche="jewellery",
                     is_limited_company=False)
    _, problems = O.draft(O.get_lead(lid), "email")
    joined = " ".join(problems).lower()
    check("sole trader (individual subscriber) email is BLOCKED",
          "individual" in joined, str(problems))
    check("block explains the consent requirement", "consent" in joined, str(problems))
    # a limited company must NOT be blocked for the same reason
    lid2 = O.add_lead(name="Big", business="Big Ltd", channel="email",
                      handle="hi@bigltd.co.uk", is_limited_company=True)
    _, p2 = O.draft(O.get_lead(lid2), "email")
    check("limited company is not blocked as an individual subscriber",
          not any("individual subscriber" in p for p in p2), str(p2))


def test_social_templates_are_not_pitchy():
    lid = O.add_lead(name="Ann", business="Anns", channel="reddit", handle="u/ann",
                     niche="jewellery")
    body, problems = O.draft(O.get_lead(lid), "reddit")
    check("reddit draft has no compliance problems", not problems, str(problems))
    # the research says: lead with a free tip, pitch second
    check("reddit draft leads with a free tip",
          "foam board" in body.lower() or "white foam" in body.lower(), body[:120])
    check("reddit draft is not a bare advert", len(body) > 200, str(len(body)))


def test_warmup_gate():
    ok, have, need = O.warmup_ok("reddit", "r/EtsySellers")
    check("warmup initially not ready", not ok, f"{have}/{need}")
    for _ in range(5):
        O.log_warmup("reddit", "r/EtsySellers")
    ok2, have2, _ = O.warmup_ok("reddit", "r/EtsySellers")
    check("warmup ready after required posts", ok2 and have2 >= 5, f"{have2}")


def test_daily_cap():
    ch = "testcap"
    O.DAILY_CAPS[ch] = 2
    ok, msg = O.can_send(ch)
    check("cap allows first send", ok, msg)
    lid = O.add_lead(name="x", business="x", channel=ch, handle="x1")
    O.log_touch(lid, ch, "first", "")
    O.log_touch(lid, ch, "followup", "")
    ok2, msg2 = O.can_send(ch)
    check("cap blocks beyond limit", not ok2, msg2)


def test_followup_schedule():
    lid = O.add_lead(name="Chase", business="Chase Ltd", channel="instagram",
                     handle="@chase")
    check("brand new lead not due", all(d["id"] != lid for d in O.due_followups()))
    # simulate a first touch 4 days ago
    import sqlite3
    import time
    conn = O.get_db()
    conn.execute("INSERT INTO touches(lead_id,channel,kind,sent_ts,status,created_ts) "
                 "VALUES(?,?,?,?,?,?)",
                 (lid, "instagram", "first", int(time.time()) - 4 * 86400, "sent",
                  int(time.time())))
    conn.commit()
    conn.close()
    check("lead with 4-day-old touch is due",
          any(d["id"] == lid for d in O.due_followups()))

    # a replied lead must never be chased
    O.log_touch(lid, "instagram", "first", "", "replied")
    check("replied lead is NOT chased",
          all(d["id"] != lid for d in O.due_followups()))


def test_max_touches():
    lid = O.add_lead(name="Max", business="Max Ltd", channel="discord", handle="max#1")
    for _ in range(O.MAX_TOUCHES):
        O.log_touch(lid, "discord", "first", "")
    check("never chase beyond MAX_TOUCHES",
          all(d["id"] != lid for d in O.due_followups()))


def test_import_csv():
    # self-contained: create the suppression this test relies on
    O.suppress("etsy", "https://etsy.com/shop/copperlane", "opt-out")
    p = Path(_tmp) / "leads.csv"
    p.write_text("name,business,channel,handle,niche,product_count,is_limited_company\n"
                 "Zoe,Zoe Ceramics,etsy,https://etsy.com/shop/zoe,ceramics,22,\n"
                 "Ltd Co,Ltd Co,email,x@ltd.co.uk,homeware,,\n"
                 ",Already Gone,etsy,https://etsy.com/shop/copperlane,,\n")
    added, skipped = O.import_leads(str(p))
    check("csv import adds rows", added == 2, f"added={added}")
    check("csv import skips suppressed", skipped == 1, f"skipped={skipped}")
    zoe = [l for l in O.list_leads() if l["business"] == "Zoe Ceramics"]
    check("csv import keeps niche", zoe and zoe[0]["niche"] == "ceramics",
          str(zoe[0] if zoe else None))
    check("csv import keeps product_count",
          zoe and zoe[0]["product_count"] == 22)


def test_dedupe_same_handle():
    """Regression: a finder run across several niches re-found the same shop, and one
    shop ended up in the pipeline 5 times. That inflates counts and means contacting
    the same person repeatedly."""
    h = "https://www.etsy.com/shop/DupeShop"
    first = O.add_lead(name="", business="DupeShop", channel="etsy", handle=h)
    second = O.add_lead(name="", business="DupeShop", channel="etsy", handle=h)
    check("first add succeeds", first is not None, str(first))
    check("duplicate handle is rejected", second is None, str(second))
    n = len([l for l in O.list_leads() if l["handle"] == h])
    check("only one row for that handle", n == 1, f"found {n}")
    # a different channel is a different lead, not a duplicate
    third = O.add_lead(name="", business="DupeShop", channel="instagram",
                       handle="@dupeshop")
    check("same shop on another channel is allowed", third is not None, str(third))


def test_drafts_do_not_claim_to_have_seen_photos():
    """Guard: every lead is just an Etsy shop URL — the finder reads a search snippet,
    it never sees the shop. So no template may assert anything about the prospect's
    existing photos. We shipped 'a few of the photos are working against them' for a
    while, which was a lie a 30-second check would have caught."""
    banned = ("photos are working against", "lighting on a couple", "photos aren't",
              "i noticed your photos", "your photos need")
    for ch in ("email", "etsy", "instagram", "facebook", "reddit", "discord"):
        lid = O.add_lead(name="X", business=f"Biz{ch}", channel=ch,
                         handle=f"handle-{ch}", niche="jewellery")
        body, _ = O.draft(O.get_lead(lid), ch)
        low = (body or "").lower()
        for phrase in banned:
            check(f"{ch} draft does not claim to have seen their photos",
                  phrase not in low, f"contains: {phrase}")


def test_followup_does_not_claim_to_have_seen_photos():
    lid = O.add_lead(name="Y", business="BizFollow", channel="etsy",
                     handle="handle-follow", niche="ceramic")
    body, _ = O.draft(O.get_lead(lid), "etsy", kind="followup")
    low = (body or "").lower()
    check("follow-up makes no unverifiable photo claim",
          "photos are working against" not in low and "lighting on a couple" not in low,
          low[:200])


def test_stats():
    # self-contained: make a lead so the count assertion means something
    O.add_lead(name="Stat", business="Stat Shop", channel="etsy",
               handle="https://etsy.com/shop/statshop")
    O.suppress("etsy", "https://etsy.com/shop/suppressedone", "opt-out")
    s = O.stats()
    check("stats counts leads", s["leads"] > 0, str(s["leads"]))
    check("stats reports suppression count", s["suppressed"] >= 1, str(s["suppressed"]))
    check("stats has by_channel", isinstance(s["by_channel"], dict))


if __name__ == "__main__":
    run_all(globals())
