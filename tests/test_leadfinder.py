"""
Tests for the lead finder.

The two things that matter and are easy to get wrong:

1. **Limited-company detection must not miss run-together shop names.** A plain
   \\bltd\\b regex misses 'HandmadeJewelleryLtd' — and missing it means cold-emailing a
   corporate subscriber, which breaches UK PECR. The original bug under-flagged every
   camelCase company name.
2. **Importing finds must never resurrect someone who opted out.**
"""
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.harness import check, temp_db, run_all  # noqa: E402

from engine import leadfinder as LF  # noqa: E402
from engine import outreach as O     # noqa: E402

_tmp = temp_db()
O.init_db()


def test_ltd_detection_catches_camelcase():
    # regression: these all returned False before the fix
    for name in ("HandmadeJewelleryLtd", "SmithAndSonsLTD", "RusticLtd",
                 "CopperLaneLtd", "BigCoPlc"):
        check(f"ltd detected: {name}", LF.looks_like_ltd({"shop": name}), name)


def test_ltd_detection_whole_words():
    for rec in ({"shop": "Copper Lane Ltd"}, {"shop": "Designs Limited"},
                {"snippet": "registered in England company no 12345"},
                {"snippet": "registered office in London"},
                {"shop": "Something LLC"}):
        check(f"ltd whole-word detected: {list(rec.values())[0]}",
              LF.looks_like_ltd(rec))


def test_ltd_detection_no_false_positives():
    # these are NOT companies and must not be flagged
    for name in ("Gladbel", "AmyWallisJewellery", "CopperLane", "Narratorium",
                 "LimitedEditionPrints", "HandmadeBySo", "TheJewelleryHutUK"):
        check(f"not wrongly flagged: {name}", not LF.looks_like_ltd({"shop": name}), name)


def test_scoring_is_explainable():
    rec = {"source": "shopindex", "shop": "CopperLaneJewellery",
           "title": "CopperLaneJewellery",
           "snippet": "Handmade jewellery located in London, United Kingdom. "
                      "Family business. Rave reviews! Speedy replies!"}
    score, why = LF.score_lead(rec)
    check("scored above zero", score > 0, str(score))
    check("score capped at 10", score <= 10, str(score))
    check("every point has a stated reason", len(why) >= 3, str(why))
    check("reasons are human-readable", all(isinstance(w, str) and w for w in why))
    check("uk signal detected", any("UK-based" in w for w in why), str(why))


def test_scoring_caps_at_ten():
    rec = {"source": "shopindex", "shop": "X", "title": "handmade jewellery candle",
           "snippet": ("located in London united kingdom family business rave reviews "
                       "speedy replies instagram handmade small business independent "
                       "new launch made in handmade")}
    score, _ = LF.score_lead(rec)
    check("max score clamped to 10", score == 10.0, str(score))


def test_import_skips_suppressed():
    # someone finds a shop, opts out
    O.suppress("etsy", "https://www.etsy.com/shop/OptedOutShop", "opt-out")
    recs = [{"source": "shopindex", "shop": "OptedOutShop", "title": "OptedOutShop",
             "snippet": "handmade jewellery located in London", "score": 8.0,
             "handle": "https://www.etsy.com/shop/OptedOutShop", "why": ["x"]},
            {"source": "shopindex", "shop": "FreshShop", "title": "FreshShop",
             "snippet": "handmade jewellery located in London", "score": 8.0,
             "handle": "https://www.etsy.com/shop/FreshShop", "why": ["x"]}]
    added, low, supp = LF.import_to_outreach(recs, min_score=5.0)
    check("import adds the fresh lead", added == 1, f"added={added}")
    check("import skips the suppressed lead", supp == 1, f"supp={supp}")
    handles = [l["handle"] for l in O.list_leads()]
    check("suppressed shop not in DB",
          "https://www.etsy.com/shop/OptedOutShop" not in handles)


def test_import_respects_min_score():
    recs = [{"source": "shopindex", "shop": "LowScore", "title": "LowScore",
             "snippet": "", "score": 2.0, "handle": "https://www.etsy.com/shop/LowScore",
             "why": []}]
    added, low, supp = LF.import_to_outreach(recs, min_score=5.0)
    check("import skips below min_score", added == 0 and low == 1, f"{added}/{low}")


def test_import_flags_ltd():
    recs = [{"source": "shopindex", "shop": "ShinyLtd", "title": "ShinyLtd",
             "snippet": "handmade jewellery", "score": 8.0,
             "handle": "https://www.etsy.com/shop/ShinyLtd",
             "is_limited_company": True, "why": ["x"]}]
    LF.import_to_outreach(recs, min_score=5.0)
    ltd = [l for l in O.list_leads() if l["business"] == "ShinyLtd"]
    check("imported lead carries ltd flag", ltd and ltd[0]["is_limited_company"] == 1,
          str(ltd))


def test_ltd_lead_can_receive_compliant_email():
    """End-to-end: a detected company IS cold-emailable under PECR — corporate
    subscribers are exempt from the consent requirement — PROVIDED identity and a
    postal address are present. (The earlier version of this test asserted the
    opposite, which encoded an inverted reading of the ICO guidance.)

    Self-contained: creates its own company lead."""
    lid = O.add_lead(name="", business="ShinyLtd", channel="email",
                     handle="info@shiny-ltd.co.uk", is_limited_company=True)
    ltd = O.get_lead(lid)
    O.set_setting("sender_name", "Russ")
    O.set_setting("sender_address", "1 Test Street")
    body, problems = O.draft(ltd, "email")
    check("company lead is NOT blocked as an individual subscriber",
          not any("individual subscriber" in p for p in problems), str(problems))
    check("company lead email generates with id + address set", bool(body), str(problems))
    check("company lead email still carries an opt-out",
          "reply 'no'" in body.lower(), body[-120:])

    # and the inverse: a sole trader in the same shape MUST be blocked
    lid2 = O.add_lead(name="", business="SoleTraderShop", channel="email",
                      handle="hi@soletradershop.co.uk", is_limited_company=False)
    _, p2 = O.draft(O.get_lead(lid2), "email")
    check("sole trader IS blocked from cold email",
          any("individual subscriber" in p.lower() for p in p2), str(p2))


def test_shop_query_builder():
    q = LF.shop_query("handmade jewellery", "UK")
    check("shop query uses site: operator", "site:etsy.com/shop" in q, q)
    check("shop query includes niche", "handmade jewellery" in q, q)
    check("shop query includes location", "UK" in q, q)


def test_save_and_load_roundtrip():
    recs = [{"source": "shopindex", "shop": "Roundtrip", "score": 7.0,
             "handle": "x", "why": []}]
    p = Path(_tmp) / "found.json"
    LF.save_found(recs, p)
    back = LF.load_found(p)
    check("save/load roundtrips", back == recs, str(back))
    check("saved file is valid json", isinstance(json.loads(p.read_text()), dict))


def test_missing_file_returns_empty():
    check("missing file loads as empty list",
          LF.load_found(Path(_tmp) / "nope.json") == [])


if __name__ == "__main__":
    run_all(globals())
