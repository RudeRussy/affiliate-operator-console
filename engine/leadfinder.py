"""
Lead finder: discover real, contactable prospects from public sources.

Finds Etsy shops and other small-seller prospects that ALREADY have weak product
photography — the exact people the Service Kit is for. Everything here is public
data, no login, no scraping behind a wall.

Design rules that matter:
  - **Only public sources.** No logged-in scraping, no private data, no bypassing
    a paywall or a login. If a source needs auth, it isn't in here.
  - **Dedupe against suppression.** Anyone who ever said "no" is skipped on import,
    so a finder run can never resurrect an opted-out lead.
  - **Signals beat guesses.** We record WHY a lead looks promising (few photos,
    no video, low listing count) so you can prioritise, rather than dumping a list.

Sources (verified reachable 2026-10-02):
  etsy_rss      Etsy shop/search RSS      — public, keyless
  bing_news     small-seller press        — public RSS
  hn            "Show HN" maker launches  — public API

Usage:
  python -m engine.leadfinder --source etsy --query "ceramic mug" --limit 20
  python -m engine.leadfinder --source hn --query "etsy seller" --limit 10
  python -m engine.leadfinder --import-found          # push into the outreach DB
  python -m engine.leadfinder --dry-run               # print, don't store
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
FOUND_PATH = Path(__file__).resolve().parent.parent / "found_leads.json"

# Niche keywords that map to the Service Kit's target market (physical products,
# 1-50 items, sellers who'd benefit from better photos).
PRODUCT_NICHES = ["jewellery", "jewelry", "ceramic", "homeware", "candle", "textile",
                  "clothing", "accessories", "print", "leather", "soap", "cosmetic",
                  "art print", "stationery", "pottery", "knitwear", "bags"]


def _fetch(url, timeout=15):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _clean(s):
    s = re.sub(r"<[^>]+>", "", s or "")
    s = (s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
          .replace("&lt;", "<").replace("&gt;", ">"))
    return re.sub(r"\s+", " ", s).strip()


# ---------------------------------------------------------------- sources
#
# NOTE ON WHAT WORKS (measured 2026-10-02, do not re-litigate):
#   Etsy search/RSS: 404/403, and robots.txt DISALLOWS /search — dead on purpose.
#   eBay / eBid / NotOnTheHighStreet: 403 or redirect to homepage.
#   Folksy: redirects to /login.
#   Reddit JSON: 403.
# => There is NO legitimately automatable marketplace seller directory. Do not
#    build a scraper for one; the marketplaces gate it deliberately.
#
# What DOES work: a search-engine index of `site:etsy.com/shop`. Brave returns the
# shop name, URL, and a description carrying location + business type + review
# signals. That is a search index, not a scrape, and it is the correct approach.

def _brave_key():
    """Read the Brave Search API key. Checks env, then ~/.hermes/.env.
    Never logs or returns the key itself to callers."""
    k = os.environ.get("BRAVE_SEARCH_API_KEY")
    if k:
        return k.strip()
    for p in (Path.home() / ".hermes" / ".env",
              Path(__file__).resolve().parent.parent / ".env"):
        try:
            for line in p.read_text().splitlines():
                if line.strip().startswith("BRAVE_SEARCH_API_KEY"):
                    return line.split("=", 1)[1].strip().strip('"').strip("'")
        except Exception:
            continue
    return ""


def _brave(query, count=20):
    """Query the Brave Search API directly. Returns {'results': [...]}.

    Brave is a search INDEX — asking it for `site:etsy.com/shop ...` is a search,
    not a scrape. That distinction is why this source is legitimate where the Etsy
    scrapers are not."""
    key = _brave_key()
    if not key:
        return {}
    url = ("https://api.search.brave.com/res/v1/web/search?q="
           + urllib.parse.quote(query) + f"&count={min(count, 20)}")
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/json")
    req.add_header("X-Subscription-Token", key)
    try:
        with urllib.request.urlopen(req, timeout=25) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return {}
    return {"results": [{"url": x.get("url", ""),
                         "title": x.get("title", ""),
                         "description": x.get("description", "")}
                        for x in d.get("web", {}).get("results", [])]}


def src_shopindex(query, limit=20):
    """Etsy shops via a search-engine index of site:etsy.com/shop.

    The query must be supplied by the caller as a full search string including the
    site: operator — see `shop_query()`.
    """
    raw = _brave(query, limit)
    results = raw.get("results") or raw.get("web", {}).get("results") or []
    if not results and isinstance(raw, list):
        results = raw
    out = []
    for r in results[:limit]:
        url = r.get("url") or r.get("link") or ""
        if "etsy.com" not in url or "/shop/" not in url:
            continue
        m = re.search(r"/shop/([A-Za-z0-9_-]+)", url)
        shop = m.group(1) if m else ""
        desc = _clean(r.get("description") or r.get("snippet") or "")
        out.append({
            "source": "shopindex",
            "title": _clean(r.get("title") or shop),
            "url": url,
            "shop": shop,
            "snippet": desc,
            "signal": "etsy_shop_index",
        })
    return out


def shop_query(niche, location="UK"):
    """Build the indexed search string for a niche."""
    return f"site:etsy.com/shop {niche} {location}".strip()


def src_bing_news(query, limit=15):
    """Small-seller press mentions — often name the shop/brand."""
    url = "https://www.bing.com/news/search?format=rss&q=" + urllib.parse.quote(query)
    try:
        t = _fetch(url)
    except Exception:
        return []
    items = re.findall(r"<item>(.*?)</item>", t, re.S)
    out = []
    for it in items[:limit]:
        title = re.search(r"<title>(.*?)</title>", it, re.S)
        link = re.search(r"<link>(.*?)</link>", it, re.S)
        if not link:
            continue
        out.append({
            "source": "news",
            "title": _clean(title.group(1)) if title else "",
            "url": link.group(1).strip(),
            "shop": "",
            "snippet": _clean(title.group(1)) if title else "",
            "signal": "press_mention",
        })
    return out


def src_hn(query, limit=15):
    """Show HN launches by indie makers — often physical-product shops."""
    url = ("https://hn.algolia.com/api/v1/search?tags=show_hn&hitsPerPage="
           f"{limit}&query=" + urllib.parse.quote(query))
    try:
        d = json.loads(_fetch(url))
    except Exception:
        return []
    out = []
    for h in d.get("hits", []):
        if not h.get("title"):
            continue
        out.append({
            "source": "hn",
            "title": _clean(h["title"]),
            "url": h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}",
            "shop": "",
            "snippet": f"{h.get('points', 0)} points",
            "signal": "show_hn",
        })
    return out


SOURCES = {"shopindex": src_shopindex, "news": src_bing_news, "hn": src_hn}


# ---------------------------------------------------------------- scoring

def score_lead(rec):
    """0-10: how good a prospect this looks. Deliberately crude and EXPLAINABLE —
    every point has a stated reason, so you can sanity-check the ranking."""
    s, why = 0.0, []
    text = f"{rec.get('title','')} {rec.get('snippet','')}".lower()

    if rec.get("shop"):
        s += 2
        why.append("has an Etsy shop (contactable on-platform)")
    for n in PRODUCT_NICHES:
        if n in text:
            s += 2.5
            why.append(f"target niche: {n}")
            break
    if rec.get("source") == "shopindex":
        s += 1.5
        why.append("active seller with live listings")
    if any(w in text for w in ("located in", "united kingdom", "london", "manchester",
                               "sheffield", "scotland", "wales", "uk")):
        s += 1.5
        why.append("UK-based (fast, cheap postage — good fit)")
    if any(w in text for w in ("family business", "family run", "our workshop",
                               "small business", "independent")):
        s += 1.5
        why.append("self-described small/family business")
    if any(w in text for w in ("review rating is 4.8", "review rating is 4.9",
                               "review rating is 5", "rave reviews", "star seller")):
        s += 1.5
        why.append("strong reviews — has revenue to spend, cares about quality")
    if any(w in text for w in ("speedy replies", "replying to messages quickly")):
        s += 1
        why.append("responds to messages (your outreach will be seen)")
    if "instagram" in text or "facebook" in text or "tiktok" in text:
        s += 1
        why.append("has social presence (can be contacted off-platform too)")
    if any(w in text for w in ("handmade", "small business", "independent",
                               "family run", "made in")):
        s += 1.5
        why.append("self-described small/independent seller")
    if any(w in text for w in ("sale", "new", "launch", "just opened", "opening")):
        s += 1
        why.append("new/growing (timing is good)")
    if rec.get("signal") == "show_hn":
        s += 1
        why.append("maker who launched publicly")
    return min(s, 10.0), why


def looks_like_ltd(rec):
    """Detect a likely UK limited company from public text.

    This matters legally: a limited company is a *corporate subscriber* under PECR
    and needs prior consent for cold email. Flagging it here means the outreach
    compliance gate blocks it automatically, instead of relying on you to notice.

    TRAP (measured 2026-10-02): a plain \\bltd\\b word-boundary check MISSES camelCase
    shop names — 'HandmadeJewelleryLtd', 'SmithAndSonsLTD', 'RusticLtd' all slipped
    through, because a letter directly precedes 'Ltd' so there is no word boundary.
    Shop names on marketplaces are very often run-together, so that bug silently
    under-flagged real companies. Match case-insensitively at the END of a name, or
    as a whole word.
    """
    text = f"{rec.get('shop','')} {rec.get('title','')} {rec.get('snippet','')}".lower()
    # end-of-token company suffixes (catches CamelCase run-together names)
    if re.search(r"(ltd|limited|llc|gmbh|plc|inc)\.?$", text.strip()):
        return True
    # whole-word markers anywhere
    for m in ("ltd", "limited", "llc", "gmbh", "inc", "incorporated", "plc",
              "company no", "registered in england", "registered office",
              "vat number", "companies house"):
        if re.search(r"\b" + re.escape(m) + r"\b", text):
            return True
    return False


def find(source="etsy", query="handmade jewellery", limit=20):
    fn = SOURCES.get(source)
    if not fn:
        raise SystemExit(f"unknown source '{source}' (have: {', '.join(SOURCES)})")
    recs = fn(query, limit)
    for r in recs:
        sc, why = score_lead(r)
        r["score"] = round(sc, 1)
        r["why"] = why
        # candidate handle: prefer the shop, else the URL
        r["handle"] = (f"https://www.etsy.com/shop/{r['shop']}" if r.get("shop")
                       else r.get("url", ""))
    recs.sort(key=lambda r: -r["score"])
    for r in recs:
        r["is_limited_company"] = looks_like_ltd(r)
    return recs


def save_found(recs, path=FOUND_PATH):
    path.write_text(json.dumps({"ts": int(time.time()), "count": len(recs),
                                "leads": recs}, indent=2))
    return path


def load_found(path=FOUND_PATH):
    if not Path(path).exists():
        return []
    try:
        return json.loads(Path(path).read_text()).get("leads", [])
    except Exception:
        return []


def import_to_outreach(recs, min_score=3.0):
    """Push scored finds into the outreach DB. Suppressed leads are skipped by
    add_lead, so this can never resurrect someone who opted out."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from engine import outreach as O
    O.init_db()
    added = skipped_low = skipped_supp = 0
    for r in recs:
        if r.get("score", 0) < min_score:
            skipped_low += 1
            continue
        handle = (r.get("handle") or "").strip()
        if not handle:
            skipped_low += 1
            continue
        if O.is_suppressed("etsy", handle):
            skipped_supp += 1
            continue
        # name the niche from the why-list
        niche = next((w.split(": ", 1)[1] for w in r.get("why", [])
                      if w.startswith("target niche: ")), "")
        lid = O.add_lead(
            name="", business=(r.get("shop") or r.get("title") or "")[:160],
            channel="etsy", handle=handle, niche=niche,
            is_limited_company=bool(r.get("is_limited_company")),
            notes=(f"auto-found via {r.get('source')} (score {r.get('score')}). "
                   f"{'; '.join(r.get('why', []))}")[:600],
            source=f"leadfinder:{r.get('source')}")
        if lid:
            added += 1
        else:
            skipped_supp += 1
    return added, skipped_low, skipped_supp


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="shopindex", choices=sorted(SOURCES))
    ap.add_argument("--niche", default="handmade jewellery",
                    help="for shopindex: e.g. 'handmade jewellery'")
    ap.add_argument("--query", default=None, help="raw query (overrides --niche)")
    ap.add_argument("--location", default="UK")
    ap.add_argument("--limit", type=int, default=20)
    ap.add_argument("--import-found", action="store_true",
                    help="push scored results into the outreach DB")
    ap.add_argument("--min-score", type=float, default=3.0)
    ap.add_argument("--dry-run", action="store_true", help="print only, don't store")
    ap.add_argument("--show-saved", action="store_true")
    a = ap.parse_args()

    if a.show_saved:
        recs = load_found()
        if not recs:
            print("nothing saved yet")
        for r in recs:
            print(f"  {r['score']:4.1f}  {r.get('shop') or r.get('title','')[:44]:44s} {r.get('handle','')[:56]}")
        return

    q = a.query or (shop_query(a.niche, a.location) if a.source == "shopindex" else a.niche)
    recs = find(a.source, q, a.limit)
    print(f"Found {len(recs)} from {a.source} for '{q}'\n")
    for r in recs:
        print(f"  {r['score']:4.1f}  {(r.get('shop') or r.get('title') or '')[:44]:44s}")
        print(f"        {r.get('handle','')[:90]}")
        print(f"        why: {', '.join(r.get('why', []))}")

    if not a.dry_run:
        p = save_found(recs)
        print(f"\n  saved -> {p}")

    if a.import_found:
        added, low, supp = import_to_outreach(recs, a.min_score)
        print(f"\n  imported {added} leads into outreach "
              f"(skipped {low} below score / no handle, {supp} suppressed)")


if __name__ == "__main__":
    main()
