"""
Tests for Amazon Associates support.

The rules here are Amazon's, not ours, and breaking them terminates an account.
So the tests assert the REFUSALS as hard as the successes.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.harness import check, run_all  # noqa: E402

from engine import amazon as A  # noqa: E402


def test_recognises_amazon_urls():
    for u in ("https://www.amazon.co.uk/dp/B08N5WRWNW",
              "https://amazon.com/gp/product/B08N5WRWNW",
              "https://www.amazon.de/dp/B08N5WRWNW",
              "https://amzn.to/3xYzAbC"):
        check(f"recognised: {u[:40]}", A.is_amazon_url(u))
    for u in ("https://www.etsy.com/shop/x", "https://clickbank.net", "not a url"):
        check(f"not amazon: {u[:30]}", not A.is_amazon_url(u))


def test_asin_extraction():
    cases = {
        "https://www.amazon.co.uk/dp/B08N5WRWNW": "B08N5WRWNW",
        "https://www.amazon.co.uk/gp/product/B08N5WRWNW/ref=x": "B08N5WRWNW",
        "https://www.amazon.co.uk/Some-Title/dp/B08N5WRWNW/ref=sr_1_3?crid=ABC": "B08N5WRWNW",
        "https://www.amazon.com/d/B08N5WRWNW": "B08N5WRWNW",
    }
    for url, want in cases.items():
        got = A.asin_from(url)
        check(f"asin {want}", got == want, f"got {got}")


def test_tagging_basic():
    out = A.build_link("https://www.amazon.co.uk/dp/B08N5WRWNW", "mytag-21")
    check("tag applied", "tag=mytag-21" in out, out)
    check("canonicalised to /dp/", "/dp/B08N5WRWNW" in out, out)
    check("https preserved", out.startswith("https://"), out)


def test_tagging_strips_tracking_cruft():
    url = ("https://www.amazon.co.uk/Ceramic-Mug/dp/B08N5WRWNW/ref=sr_1_3"
           "?crid=ABC&th=1&psc=1&qid=123&sr=8-3")
    out = A.build_link(url, "mytag-21")
    for junk in ("crid", "th=", "psc", "qid", "sr="):
        check(f"stripped {junk}", junk not in out, out)
    check("kept the tag", "tag=mytag-21" in out, out)
    check("kept the asin", "B08N5WRWNW" in out, out)


def test_ascsubtag_is_the_subid():
    out = A.build_link("https://www.amazon.co.uk/dp/B08N5WRWNW", "mytag-21",
                       clickid="01M3CLICK")
    check("uses ascsubtag (Amazon's sub-id param, NOT sub1)",
          "ascsubtag=01M3CLICK" in out, out)
    check("does not use sub1", "sub1=" not in out, out)


def test_email_is_refused():
    """The Operating Agreement bans email promotion — this must raise, not warn."""
    raised = False
    try:
        A.build_link("https://www.amazon.co.uk/dp/B08N5WRWNW", "mytag-21",
                     channel="email")
    except A.AmazonPolicyError:
        raised = True
    check("email link build raises AmazonPolicyError", raised)


def test_tag_is_mandatory():
    raised = False
    try:
        A.build_link("https://www.amazon.co.uk/dp/B08N5WRWNW", "")
    except ValueError:
        raised = True
    check("missing tag raises", raised)


def test_non_amazon_url_refused():
    raised = False
    try:
        A.build_link("https://www.etsy.com/shop/x", "mytag-21")
    except ValueError:
        raised = True
    check("non-Amazon URL refused", raised)


def test_rate_card_is_conservative():
    # unknown category must NOT default to a flattering figure
    check("unknown category defaults to 3%", A.estimate_rate() == 0.03,
          str(A.estimate_rate()))
    check("unknown string defaults to 3%", A.estimate_rate("made-up") == 0.03)
    check("electronics is low (1%)", A.estimate_rate("electronics") == 0.01)
    check("furniture is 10%", A.estimate_rate("furniture") == 0.10)
    check("fashion is 7%", A.estimate_rate("fashion") == 0.07)
    # the honest signal: amazon in the AI-tools niche is a poor payer
    check("amazon default rate is far below a typical digital programme",
          A.estimate_rate() < 0.10)


def test_check_offer_blocks_email():
    r = A.check_offer("https://www.amazon.co.uk/dp/B08N5WRWNW", "mytag-21",
                      channel="email")
    check("check_offer flags email blocker",
          any("email" in b for b in r["blockers"]), str(r["blockers"]))
    check("check_offer warns prices cannot be pulled",
          any("price" in n.lower() for n in r["notes"]), str(r["notes"]))
    check("check_offer states the 24h cookie window",
          any("24 hour" in n.lower() for n in r["notes"]), str(r["notes"]))


def test_check_offer_no_tag_is_a_blocker():
    r = A.check_offer("https://www.amazon.co.uk/dp/B08N5WRWNW", "")
    check("missing tag is a blocker",
          any("tag" in b for b in r["blockers"]), str(r["blockers"]))


def test_search_url():
    u = A.search_url("ceramic mug set", "mytag-21")
    check("search url is tagged", "tag=mytag-21" in u, u)
    check("search url encodes the query", "ceramic+mug+set" in u, u)
    u2 = A.search_url("mug", "mytag-21", clickid="X1")
    check("search url carries ascsubtag", "ascsubtag=X1" in u2, u2)


def test_disclosure_wording():
    check("required disclosure is present and Amazon's phrasing",
          "Amazon Associate" in A.REQUIRED_DISCLOSURE, A.REQUIRED_DISCLOSURE)
    check("disclosure says qualifying purchases",
          "qualifying purchases" in A.REQUIRED_DISCLOSURE)


if __name__ == "__main__":
    run_all(globals())
