"""
Tests for lead verification.

The point of this module is to convert a name we guessed from a search snippet into
something we can act on — or drop it. So the tests pin the CLASSIFICATION rules, which
are where a wrong answer costs real effort.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.harness import check, run_all  # noqa: E402

from engine import leadverify as V  # noqa: E402


def test_confirmed_when_shop_page_returned():
    results = [{"url": "https://www.etsy.com/shop/AmyWallisJewellery",
                "title": "AmyWallisJewellery - Etsy",
                "description": "Handmade silver jewellery located in Salisbury."}]
    status, ev = V.classify("AmyWallisJewellery", results)
    check("shop page => confirmed", status == "confirmed", status)
    check("evidence captured", "Salisbury" in ev, ev)


def test_unverified_when_nothing_returned():
    status, ev = V.classify("DoesNotExistShop", [])
    check("no results => unverified (do not contact)", status == "unverified", status)


def test_weak_when_mentioned_but_not_the_shop_page():
    """A passing mention must NOT be promoted to confirmed — that would send you to DM
    a shop you have not actually located."""
    results = [{"url": "https://randomblog.com/post",
                "title": "10 great jewellery shops",
                "description": "we love WhatnotJewellery among others"}]
    status, ev = V.classify("WhatnotJewellery", results)
    check("passing mention => weak, not confirmed", status == "weak", status)


def test_unrelated_results_are_unverified():
    results = [{"url": "https://unrelated.com", "title": "Something else",
                "description": "nothing to do with it"}]
    status, ev = V.classify("MyShop", results)
    check("unrelated results => unverified", status == "unverified", status)


def test_error_status_is_distinct_from_unverified():
    """A failed lookup must never be recorded as 'unverified' — that would silently
    bin a good lead because the network blipped."""
    code = V.verify_lead.__doc__ or ""
    # exercise the real guard by monkeypatching the lookup to raise
    orig = V._brave_lookup
    V._brave_lookup = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        status, ev = V.verify_lead("AnyShop", "https://www.etsy.com/shop/AnyShop")
    finally:
        V._brave_lookup = orig
    check("lookup failure => error, not unverified",
          status == "error", status)
    check("error carries the reason", "boom" in ev, ev)


def test_ensure_columns_is_idempotent():
    from engine.outreach import get_db
    c = get_db()
    V._ensure_columns(c)
    V._ensure_columns(c)  # must not raise on second call
    cols = {r[1] for r in c.execute("PRAGMA table_info(leads)")}
    for want in ("verify_status", "verify_evidence", "verify_ts"):
        check(f"column {want} exists", want in cols)
    check("no duplicate columns", len([c for c in cols if c.startswith("verify_")]) == 3)


if __name__ == "__main__":
    run_all(globals())
