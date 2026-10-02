"""
Amazon Associates support — link building, and an honest statement of what Amazon
does and does NOT let you automate.

READ THIS BEFORE BUILDING ON IT (verified 2026-10-02):

  What Amazon ALLOWS:
    - You may tag any amazon.co.uk product URL with your own `tag=<associate-id>`.
      That is how the whole program works. No API needed.
    - **OneLink** georedirects international visitors to their local Amazon and is
      Amazon's sanctioned mechanism for this.
    - SiteStripe exists for grabbing links manually.

  What Amazon does NOT allow / does not work:
    - **Cold email promotion is BANNED.** Sending your affiliate links by email is an
      explicit Operating Agreement violation and account termination. So the outreach
      engine must NEVER carry an Amazon link. This module refuses to give you one for
      that purpose.
    - **PA-API (Product Advertising API) is unavailable to new associates.** You need
      qualifying sales first — commonly reported as 3 sales in 180 days just to keep the
      account, and a trailing-30-day sales requirement to hold API access. So we cannot
      pull product data from Amazon. Do not build a feature that assumes we can.
    - **Never publish Amazon prices programmatically.** The Operating Agreement requires
      prices be current and sourced from PA-API (which we do not have). A cached price is
      a compliance breach. Use "check price on Amazon" instead.
    - **Cookie window is 24 hours**, not 30 days. (If the item is added to basket inside
      those 24h, you still earn on checkout within 89 days.) Do not build attribution
      that assumes a month.
    - Sub-tracking param is **`ascsubtag`** — not `sub1`. This is what lets you attribute
      a click in our tracker; it is set on the *final* Amazon URL.

  Rate card (UK, 2026): 3-5% typical, 7% fashion, up to 10% luxury beauty/furniture,
  1-2.5% electronics/TV/PC. So an Amazon click is worth roughly 1/10th of a ClickBank
  digital sale — treat it as volume, not as the main earner.

Usage:
  python -m engine.amazon --link https://www.amazon.co.uk/dp/B08N5WRWNW --tag mytag-21
  python -m engine.amazon --search "ceramic mug set"
  python -m engine.amazon --check https://www.amazon.co.uk/dp/B08N5WRWNW --tag mytag-21
"""
import argparse
import re
import urllib.parse

# Amazon Associates rate card, UK 2026 (fractions). Source: Amazon UK commission
# schedule. Kept here so the niche scorer can weight Amazon programmes honestly
# instead of inflating them like a generic 30% "affiliate" listing.
UK_RATES = {
    "luxury_beauty": 0.10, "furniture": 0.10, "fashion": 0.07,
    "physical_media": 0.05, "toys": 0.04, "home": 0.04, "kitchen": 0.045,
    "sports": 0.04, "diy": 0.04, "pet": 0.04, "grocery": 0.03,
    "health": 0.03, "beauty": 0.03, "business": 0.04, "digital_music": 0.05,
    "pc": 0.025, "video_games": 0.02, "tv": 0.02, "electronics": 0.01,
}

DEFAULT_RATE = 0.03

# Amazon's Operating Agreement requires this exact style of statement wherever
# Amazon links appear. Unlike a generic "affiliate disclosure", Amazon specifies
# the wording, so it is generated verbatim rather than paraphrased.
REQUIRED_DISCLOSURE = ("As an Amazon Associate I earn from qualifying purchases.")

# Email promotion of Amazon links is prohibited by the Operating Agreement.
BANNED_CHANNELS = {"email"}


class AmazonPolicyError(RuntimeError):
    """Raised when asked to do something the Operating Agreement forbids."""


def is_amazon_url(url):
    try:
        host = urllib.parse.urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(host.endswith(d) for d in
               ("amazon.co.uk", "amazon.com", "amazon.de", "amazon.fr", "amazon.es",
                "amazon.it", "amazon.ca", "amzn.to", "amzn.eu"))


def asin_from(url):
    """Extract an ASIN from any common Amazon URL shape."""
    if not is_amazon_url(url):
        return ""
    for pat in (r"/dp/([A-Z0-9]{10})", r"/gp/product/([A-Z0-9]{10})",
                r"/product/([A-Z0-9]{10})", r"/d/([A-Z0-9]{10})",
                r"[?&]asin=([A-Z0-9]{10})", r"/([A-Z0-9]{10})(?:[/?]|$)"):
        m = re.search(pat, url)
        if m:
            return m.group(1)
    return ""


def build_link(url, tag, clickid=None, channel=None):
    """Tag an Amazon product URL for a given associate ID.

    Raises AmazonPolicyError if asked to build an EMAIL link — the Operating
    Agreement bans promoting Amazon links by email, and Amazon terminates accounts
    for it. Refusing here means no other module can accidentally do it.

    `clickid` is appended as `ascsubtag`, which is what lets our tracker attribute
    the click. Amazon ignores an unknown ascsubtag, so it is safe.
    """
    if channel and channel.lower() in BANNED_CHANNELS:
        raise AmazonPolicyError(
            "Promoting Amazon links by email is prohibited by the Associates "
            "Operating Agreement — it is grounds for account termination. Use a "
            "web/social channel instead.")
    if not is_amazon_url(url):
        raise ValueError(f"not an Amazon URL: {url}")
    if not tag:
        raise ValueError("an Associates tag is required (e.g. yourtag-21)")

    asin = asin_from(url)
    parts = urllib.parse.urlparse(url)
    q = dict(urllib.parse.parse_qsl(parts.query))
    q["tag"] = tag
    if clickid:
        q["ascsubtag"] = clickid
    # strip tracking cruft Amazon adds; keep only what matters
    for junk in ("ref", "ref_", "pf_rd_p", "pf_rd_r", "pd_rd_w", "pd_rd_wg",
                 "content-id", "smid", "th", "psc", "dib", "dib_tag", "crid",
                 "sprefix", "qid", "sr", "keywords", "language", "currency"):
        q.pop(junk, None)
    clean_q = urllib.parse.urlencode(q)
    path = f"/dp/{asin}" if asin else parts.path
    if not parts.netloc:
        raise ValueError(f"not an absolute Amazon URL: {url}")
    return f"https://{parts.netloc}{path}?{clean_q}"


def estimate_rate(category=None):
    """UK commission fraction. Defaults conservative (3%) when the category is
    unknown — never assume the headline rate."""
    return UK_RATES.get((category or "").lower(), DEFAULT_RATE)


def check_offer(url, tag, channel=None):
    """Inspect a prospective Amazon offer and report what it can/can't do."""
    out = {"is_amazon": is_amazon_url(url), "asin": asin_from(url),
           "rate": estimate_rate(), "blockers": [], "notes": []}
    if not out["is_amazon"]:
        out["blockers"].append("not an Amazon URL")
        return out
    if not tag:
        out["blockers"].append("no Associates tag supplied — links cannot be attributed")
    if channel and channel.lower() in BANNED_CHANNELS:
        out["blockers"].append("email promotion is prohibited by the Operating Agreement")
    out["notes"].append(
        "PA-API unavailable until you have qualifying sales, so product data "
        "(including prices) cannot be pulled programmatically.")
    out["notes"].append(
        "Never display a cached Amazon price — prices must come from PA-API and stay "
        "current. Use 'check price on Amazon' instead.")
    out["notes"].append("Cookie window is 24 hours, not 30 days.")
    if not out["asin"]:
        out["notes"].append("no ASIN found in the URL — is it a product page?")
    return out


def search_url(query, tag, clickid=None):
    """A tagged Amazon SEARCH url — useful when you have no ASIN (e.g. no PA-API)."""
    base = "https://www.amazon.co.uk/s"
    q = {"k": query, "tag": tag}
    if clickid:
        q["ascsubtag"] = clickid
    return f"{base}?{urllib.parse.urlencode(q)}"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--link", help="Amazon product URL to tag")
    ap.add_argument("--search", help="build a tagged search URL instead")
    ap.add_argument("--check", help="report policy/compliance for an Amazon URL")
    ap.add_argument("--tag", help="your Associates tag, e.g. yourname-21")
    ap.add_argument("--clickid", default=None)
    ap.add_argument("--channel", default=None, help="email / youtube / pinterest ...")
    a = ap.parse_args()

    if a.check:
        r = check_offer(a.check, a.tag, a.channel)
        print(f"  amazon:  {r['is_amazon']}")
        print(f"  asin:    {r['asin'] or '(none found)'}")
        print(f"  rate:    {r['rate']*100:.1f}% (UK, default conservative)")
        for b in r["blockers"]:
            print(f"  BLOCKER: {b}")
        for n in r["notes"]:
            print(f"  note:    {n}")
        return

    if a.search:
        if not a.tag:
            raise SystemExit("--tag is required")
        print(search_url(a.search, a.tag, a.clickid))
        return

    if a.link:
        try:
            print(build_link(a.link, a.tag, a.clickid, a.channel))
        except AmazonPolicyError as e:
            raise SystemExit(f"REFUSED: {e}")
        return

    ap.print_help()


if __name__ == "__main__":
    main()
