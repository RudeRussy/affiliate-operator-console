"""
Niche researcher: find niches that are ACTUALLY trending AND worth money.

The insight: "trending" alone is useless for affiliate work. A niche needs
  (a) rising demand      -> search + news + community signals
  (b) commercial intent  -> people typing "how to", "best", "cost", "hire"
  (c) commission depth   -> a real program that pays (cross-ref'd from programs.py)

This module gathers (a) and (b) from free, keyless sources, measures (c) against
the live affiliate-program API, and ranks. It never invents numbers: every signal
carries its source, and the report cites where each value came from.

Sources (all free, no API key):
  autocomplete_google  literal search demand — what people really type
  autocomplete_ddg     second autocomplete engine (different corpus)
  trends_rss           Google Trends daily trending (by geo)
  wikipageviews        Wikipedia interest-over-time (numeric, reliable)
  hn                   Hacker News story volume (tech/tool niches)
  news                 Bing News headline volume (mainstream momentum)
  github               new repos by stars (tool niches)
  producthunt          Product Hunt feed (commercial launches)
  programs             openaffiliate.dev (does it PAY?)

Usage:
  python -m engine.niche_research --seeds "ai product photos,etsy seo" --geo GB
  python -m engine.niche_research --seeds "..." --top 15 --out niches.json
  python -m engine.niche_research --seeds "..." --llm mistral-large-3:675b-cloud
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

STOP = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "how",
        "best", "what", "is", "are", "my", "your", "you", "it", "at", "from", "by",
        "can", "do", "i", "me", "we", "us", "this", "that", "get", "new", "make",
        "guide", "tips", "vs", "not", "no", "use", "using", "top", "way", "ways"}

# Words that signal someone is ready to SPEND (b) — commercial intent.
BUY_TOKENS = ("hire", "cost", "price", "pricing", "service", "agency", "freelance",
              "software", "tool", "tool for", "app", "platform", "for my", "for sale",
              "buy", "subscription", "worth it", "vs ", "alternative", "review",
              "course", "template", "cheap", "professional", "outsource",
              "how to", "how do", "best", "for etsy", "for shopify", "for amazon",
              "for ebay", "for my store", "for small business", "for seller", "generate",
              "create", "increase sales", "convert", "remove background", "listing",
              "mockup", "photoshoot", "studio", "product photo", "product video",
              "ad ", "ads", "marketing", "seo", "niche", "profit", "earn", "sell more")

# Generic single words that carry no niche signal on their own.
GENERIC = {"product", "products", "free", "reddit", "sell", "selling", "photo", "photos",
           "image", "images", "online", "business", "shop", "store", "site", "website",
           "good", "best", "top", "new", "help", "idea", "ideas", "money", "work",
           "make", "need", "way", "thing", "things", "people", "time", "year", "day",
           "amazon", "etsy", "shopify", "ebay", "tiktok", "instagram", "facebook",
           "youtube", "pinterest", "google", "ai", "app", "apps", "tool", "tools"}


def _fetch(url, timeout=12, headers=None):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "*/*")
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def _norm(s: str) -> str:
    s = re.sub(r"<[^>]+>", "", s or "")
    s = s.replace("&amp;", "&").replace("&#39;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", s).strip().lower()


# ---------------------------------------------------------------- sources

def src_autocomplete_google(seed):
    """Literal search demand: Google's own suggestion list."""
    url = ("https://suggestqueries.google.com/complete/search?client=firefox&q="
           + urllib.parse.quote(seed))
    try:
        d = json.loads(_fetch(url))
        return [_norm(s) for s in (d[1] if len(d) > 1 else [])]
    except Exception:
        return []


def src_autocomplete_ddg(seed):
    url = "https://duckduckgo.com/ac/?q=" + urllib.parse.quote(seed) + "&type=list"
    try:
        d = json.loads(_fetch(url))
        out = []
        for item in (d[1] if isinstance(d, list) and len(d) > 1 else []):
            out.append(_norm(item if isinstance(item, str) else item.get("phrase", "")))
        return [x for x in out if x]
    except Exception:
        return []


def src_trends_rss(geo="GB"):
    """Google Trends daily trending terms for a country."""
    try:
        t = _fetch(f"https://trends.google.com/trending/rss?geo={geo}")
        return [_norm(x) for x in re.findall(r"<title>(.*?)</title>", t, re.S)[1:]]
    except Exception:
        return []


def src_wikipageviews(term, months=9):
    """Real numeric interest-over-time. Returns (views, growth_ratio) or None."""
    art = urllib.parse.quote(term.strip().replace(" ", "_").title())
    end = date.today().replace(day=1)
    start = (end - timedelta(days=30 * months)).replace(day=1)
    fmt = "%Y%m%d00"
    url = (f"https://wikimedia.org/api/rest_v1/metrics/pageviews/per-article/"
           f"en.wikipedia/all-access/user/{art}/monthly/{start.strftime(fmt)}/{end.strftime(fmt)}")
    try:
        d = json.loads(_fetch(url))
        items = d.get("items", [])
        if len(items) < 3:
            return None
        recent = sum(i["views"] for i in items[-2:])
        older = sum(i["views"] for i in items[:2]) or 1
        return {"views": sum(i["views"] for i in items),
                "recent": recent, "growth": round(recent / older, 2)}
    except Exception:
        return None


def src_hn(term, limit=20):
    url = ("https://hn.algolia.com/api/v1/search?tags=story&hitsPerPage="
           f"{limit}&query=" + urllib.parse.quote(term))
    try:
        d = json.loads(_fetch(url))
        hits = [h for h in d.get("hits", []) if h.get("title")]
        pts = sum(h.get("points") or 0 for h in hits)
        return {"hits": len(hits), "points": pts,
                "titles": [h["title"] for h in hits[:3]]}
    except Exception:
        return {"hits": 0, "points": 0, "titles": []}


def src_news(term, limit=15):
    url = ("https://www.bing.com/news/search?format=rss&q=" + urllib.parse.quote(term))
    try:
        t = _fetch(url)
        titles = [_norm(x) for x in re.findall(r"<title>(.*?)</title>", t, re.S)[1:]]
        pubdates = re.findall(r"<pubDate>(.*?)</pubDate>", t, re.S)
        return {"count": len(titles), "titles": titles[:4], "dates": pubdates[:4]}
    except Exception:
        return {"count": 0, "titles": [], "dates": []}


def src_github(days=14):
    """New repos by stars — good for tool/software niches."""
    since = (date.today() - timedelta(days=days)).isoformat()
    url = ("https://api.github.com/search/repositories?sort=stars&order=desc&per_page=15"
           f"&q=created:>{since}")
    try:
        d = json.loads(_fetch(url))
        return [{"name": i["full_name"], "stars": i["stargazers_count"],
                 "desc": _norm(i.get("description") or "")[:120],
                 "url": i["html_url"]} for i in d.get("items", [])[:15]]
    except Exception:
        return []


def src_producthunt():
    try:
        t = _fetch("https://www.producthunt.com/feed")
        items = re.findall(r"<item>(.*?)</item>", t, re.S)[:15]
        out = []
        for it in items:
            m = re.search(r"<title>(.*?)</title>", it, re.S)
            if m:
                out.append(_norm(m.group(1)))
        return out
    except Exception:
        return []


def src_programs(term, limit=8):
    """Does this niche PAY? Match against the full cached catalogue (not the hosted
    substring search, which returns nothing for multi-word terms)."""
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    try:
        from console.catalog import match
        return match(term, limit)
    except Exception:
        return []


# ---------------------------------------------------------------- scoring

def keyphrases(texts, n=3):
    """Pull meaningful n-grams out of a list of phrases."""
    out = []
    for t in texts:
        words = [w for w in re.findall(r"[a-z0-9']+", t) if w not in STOP and len(w) > 2]
        for i in range(len(words) - n + 1):
            out.append(" ".join(words[i:i + n]))
    return out


def unigrams(texts):
    out = []
    for t in texts:
        out += [w for w in re.findall(r"[a-z0-9']+", t) if w not in STOP and len(w) > 3]
    return out


def commercial_intent(term):
    """0..N — how close to a purchase this phrase is."""
    t = " " + term.lower() + " "
    return sum(1 for tok in BUY_TOKENS if tok in t)


def specificity(term):
    """How USEFUL the phrase is as a niche: long-tail + not generic.

    A single generic word ('product', 'free') is worthless as a niche even if it
    appears in every autocomplete list. This is the penalty that stops the ranker
    from just reporting the most common English word."""
    words = [w for w in re.findall(r"[a-z0-9']+", term.lower()) if w not in STOP]
    if not words:
        return 0.0
    if len(words) == 1:
        return 0.15 if words[0] not in GENERIC else 0.02
    # every word generic -> weak; mix -> strong; multi-word -> strongest
    non_generic = sum(1 for w in words if w not in GENERIC)
    return 0.4 + 0.3 * non_generic / len(words) + 0.1 * min(len(words), 4)


def research(seeds, geo="GB", verbose=True):
    """Gather all signals for a set of seed topics. Returns a structured report."""
    report = {"seeds": seeds, "geo": geo, "ts": int(time.time()),
              "sources": {}, "candidates": [], "signals": {}}

    def log(*a):
        if verbose:
            print(*a)

    # --- A. demand: autocomplete around each seed (recursive = wider net)
    log("A. autocomplete demand…")
    ac, ac_terms = {}, defaultdict(int)
    for seed in seeds:
        for fn, name in ((src_autocomplete_google, "google"), (src_autocomplete_ddg, "ddg")):
            sugg = fn(seed)
            ac[f"{name}:{seed}"] = sugg
            for s in sugg:
                ac_terms[s] += 1
        # second hop on the strongest suggestions -> deeper demand map
        for extra in ac[f"google:{seed}"][:3]:
            for s in src_autocomplete_google(extra):
                ac_terms[s] += 1
    report["sources"]["autocomplete"] = {k: v for k, v in list(ac.items())[:20]}
    log(f"   {len(ac_terms)} unique search phrases")

    # --- B. momentum: news + trends
    log("B. news + trends momentum…")
    news = {s: src_news(s) for s in seeds}
    trends = src_trends_rss(geo)
    report["sources"]["news"] = news
    report["sources"]["trends"] = trends
    log(f"   {sum(v['count'] for v in news.values())} headlines, {len(trends)} trending terms")

    # --- C. community / tool signals
    log("C. HN + GitHub + ProductHunt…")
    hn = {s: src_hn(s) for s in seeds}
    gh = src_github()
    ph = src_producthunt()
    report["sources"]["hn"] = hn
    report["sources"]["github"] = gh
    report["sources"]["producthunt"] = ph[:10]
    log(f"   HN {sum(v['hits'] for v in hn.values())} hits, {len(gh)} new repos, {len(ph)} launches")

    # --- D. build candidate terms
    log("D. scoring candidates…")
    cand = defaultdict(lambda: {"demand": 0, "sources": set(), "examples": []})
    for term, c in ac_terms.items():
        cand[term]["demand"] += c
        cand[term]["sources"].add("autocomplete")
        if len(cand[term]["examples"]) < 3:
            cand[term]["examples"].append(term)

    # n-gram themes from all phrase corpora
    corpus = list(ac_terms) + [n["titles"][i] for n in news.values() for i in range(len(n["titles"]))]
    corpus += ph + [g["desc"] for g in gh]
    for th, cnt in _count(keyphrases(corpus, 2)).items():
        if cnt >= 2:
            cand[th]["demand"] += cnt * 0.5
            cand[th]["sources"].add("phrase")
    for u, cnt in _count(unigrams(corpus)).items():
        if cnt >= 4:
            cand[u]["demand"] += cnt * 0.3
            cand[u]["sources"].add("phrase")

    # --- E. enrich top candidates with hard numbers
    top = sorted(cand.items(), key=lambda kv: -kv[1]["demand"])[:40]
    out = []
    for term, info in top:
        wi = src_wikipageviews(term)
        nw = src_news(term)
        h = src_hn(term)
        prog = src_programs(term, 5)
        comm = commercial_intent(term)
        spec = specificity(term)

        # Commission signal must be weighted by MATCH QUALITY, not the biggest number
        # found. "ai product photoshoot app" matched a VoIP provider at 200% purely on
        # the word "app" — that is noise, and it must not outrank a real match.
        top = prog[0] if prog else None
        best_rate = 0.0
        best_name = ""
        if top:
            try:
                best_rate = float(top.get("commission_value") or 0)
            except Exception:
                best_rate = 0.0
            best_name = top.get("name") or ""
        match_conf = min((top.get("score") or 0) / 3.0, 1.0) if top else 0.0

        # Multiplicative on purpose: a generic word ('product') has high demand but
        # near-zero specificity and no program, so it cannot buy its way to the top.
        breadth = min(info["demand"], 40) / 40.0            # 0..1
        growth = 1.0
        if wi and wi.get("growth"):
            growth = min(max(wi["growth"], 0.2), 4.0) / 2.0  # ~0.1..2
        momentum = min(nw["count"], 12) / 12.0               # 0..1
        community = min(h["points"] / 200.0, 1.0)            # 0..1
        intent_f = min(comm, 5) / 5.0                        # 0..1
        money_f = match_conf * min(best_rate, 60) / 60.0     # 0..1, confidence-weighted
        trend_bonus = 1.5 if term in trends else 1.0

        base = (0.55 + 0.45 * breadth) * (0.35 + 0.65 * growth)
        score = base * (50 * spec) * (0.45 + 0.55 * intent_f) \
            * (0.55 + 0.45 * momentum) * (0.7 + 0.3 * community) \
            * (0.45 + 0.55 * money_f) * trend_bonus
        out.append({
            "term": term, "score": round(score, 1),
            "specificity": round(spec, 2),
            "demand_count": info["demand"],
            "sources": sorted(info["sources"]),
            "wiki_growth": wi["growth"] if wi else None,
            "wiki_views": wi["views"] if wi else None,
            "news_count": nw["count"],
            "hn_points": h["points"],
            "commercial_intent": comm,
            "best_program": best_name, "best_rate": best_rate,
            "programs": [p.get("name") for p in prog[:3]],
            "trending_now": term in trends,
            "sample": info["examples"][:3],
        })
    out.sort(key=lambda r: -r["score"])
    report["candidates"] = out
    log(f"   {len(out)} scored candidates")
    return report


def _count(items):
    d = defaultdict(int)
    for i in items:
        d[i] += 1
    return d


# ---------------------------------------------------------------- LLM synthesis

SYNTH_SYS = ("You are a niche analyst. You are given REAL data gathered from search "
             "autocomplete, Wikipedia pageviews, Hacker News, news headlines and live "
             "affiliate-program commission data. Group the terms into 5-8 distinct, "
             "ACTIONABLE affiliate niches. Do not invent data — use only what is given. "
             "Reply with JSON only.")

SYNTH_USER = """Data (ranked candidate terms with real measured signals):
{data}

For each niche return:
{{"niche": "short name", "why_it_works": "1-2 sentences grounded in the data above",
  "demand_evidence": "the specific numbers/terms that prove demand",
  "monetisation": "which programs/commissions apply", "difficulty": "low|medium|high",
  "first_content": "the single first piece of content to make",
  "risk": "the main thing that could make this fail"}}

Return {{"niches": [...]}} only."""


def synthesise(report, provider, model, top=25):
    from engine.content_engine import chat, extract_json
    # Only send candidates that are actually usable as niches. Low-specificity junk
    # ("near", "photography near") must never reach the model, or it will invent a
    # plausible-sounding program pairing for a meaningless term.
    usable = [r for r in report["candidates"] if r.get("specificity", 0) >= 0.35]
    rows = usable[:top]
    if not rows:
        raise SystemExit("no candidates passed the specificity filter — nothing worth synthesising")
    slim = [{"term": r["term"], "score": r["score"], "demand": r["demand_count"],
             "wiki_growth": r["wiki_growth"], "news": r["news_count"],
             "hn_points": r["hn_points"], "intent": r["commercial_intent"],
             "program": r["best_program"], "rate": r["best_rate"],
             "trending": r["trending_now"]} for r in rows]
    txt = chat(provider, model, SYNTH_SYS, SYNTH_USER.format(data=json.dumps(slim, indent=1)))
    return extract_json(txt)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seeds", required=True, help="comma-separated starting topics")
    ap.add_argument("--geo", default="GB")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--out", default="niche_research.json")
    ap.add_argument("--llm", help="ollama model to synthesise niches, e.g. mistral-large-3:675b-cloud")
    ap.add_argument("--provider", default="ollama-cloud")
    a = ap.parse_args()

    seeds = [s.strip() for s in a.seeds.split(",") if s.strip()]
    rep = research(seeds, a.geo)

    print("\n=== TOP CANDIDATES ===")
    print(f"  {'score':>7} {'spec':>5} {'intent':>6}  {'term':38s} growth news  program")
    for r in rep["candidates"][:a.top]:
        flag = "TREND " if r["trending_now"] else "      "
        prog = f"{r['best_program'][:16]} {r['best_rate']:g}%" if r["best_program"] else "-"
        print(f"  {r['score']:7.1f} {r.get('specificity', 0):5.2f} {r['commercial_intent']:6d}  "
              f"{flag}{r['term'][:38]:38s} {str(r['wiki_growth']):>6s} {r['news_count']:4d}  {prog}")

    if a.llm:
        print(f"\nSynthesising niches with {a.llm}…")
        try:
            syn = synthesise(rep, a.provider, a.llm, a.top)
            rep["niches"] = syn.get("niches", syn) if isinstance(syn, dict) else syn
            for n in rep.get("niches", []):
                if isinstance(n, dict):
                    print(f"\n  * {n.get('niche')}  [{n.get('difficulty')}]")
                    print(f"    why: {n.get('why_it_works')}")
                    print(f"    proof: {n.get('demand_evidence')}")
                    print(f"    money: {n.get('monetisation')}")
                    print(f"    start: {n.get('first_content')}")
                    print(f"    risk: {n.get('risk')}")
        except Exception as e:
            print(f"  synthesis failed: {e}")

    Path(a.out).write_text(json.dumps(rep, indent=2))
    print(f"\nwrote {a.out}")


if __name__ == "__main__":
    main()
