"""
Affiliate program catalog: cache the full openaffiliate.dev catalogue locally and
match it properly.

Why: the hosted API does LITERAL substring matching, so a multi-word query like
"ai product photos" returns nothing at all even though Photoroom/ProductScope exist.
We pull the whole catalogue once (785 programs, 2 requests), cache it on disk, and
score matches locally across name / aliases / tags / category / description.

Usage:
  python -m console.catalog --refresh
  python -m console.catalog --match "ai product photos"
  python -m console.catalog --stats
"""
import argparse
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

API = "https://openaffiliate.dev/api/programs"
CACHE = Path(__file__).resolve().parent / "program_catalog.json"
TTL = 24 * 3600  # refresh daily

STOP = {"the", "a", "an", "and", "or", "for", "to", "of", "in", "on", "with", "my",
        "your", "how", "what", "best", "free", "ai"}


def _get(url, timeout=30):
    req = urllib.request.Request(url)
    req.add_header("User-Agent", "affiliate-stack/1.0")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def fetch_all(verbose=False):
    """Download the whole catalogue via offset paging."""
    out, offset, total = [], 0, None
    while True:
        d = _get(f"{API}?limit=500&offset={offset}")
        ps = d.get("programs", [])
        total = d.get("total") or total
        out += ps
        if verbose:
            print(f"  fetched {len(out)}/{total}")
        if not ps or len(out) >= (total or 0):
            break
        offset += len(ps)
    return out


def refresh(verbose=False):
    ps = fetch_all(verbose)
    CACHE.write_text(json.dumps({"ts": int(time.time()), "count": len(ps),
                                 "programs": ps}, indent=1))
    return ps


def load(force=False):
    if not force and CACHE.exists():
        try:
            d = json.loads(CACHE.read_text())
            if time.time() - d.get("ts", 0) < TTL and d.get("programs"):
                return d["programs"]
        except Exception:
            pass
    try:
        return refresh()
    except Exception:
        if CACHE.exists():
            return json.loads(CACHE.read_text())["programs"]
        return []


def _rate_value(p):
    c = p.get("commission") or {}
    v = c.get("value")
    try:
        return float(v)
    except Exception:
        return 0.0


def _tokens(s):
    return [w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP and len(w) > 2]


def _blob(p):
    parts = [p.get("name") or "", p.get("category") or "", p.get("description") or "",
             p.get("shortDescription") or ""]
    for k in ("aliases", "tags", "agentKeywords"):
        v = p.get(k)
        if isinstance(v, list):
            parts += [str(x) for x in v]
        elif v:
            parts.append(str(v))
    return " ".join(parts).lower()


def _word_hit(token, hay):
    """Whole-word match, not substring.

    Substring matching is a trap: the token "near" matches "liNEAR", which paired a
    project-management tool with a photography niche. Real words only."""
    return re.search(rf"(?<![a-z0-9]){re.escape(token)}(?![a-z0-9])", hay) is not None


def match(term, limit=8, min_score=0.5):
    """Score the catalogue against a free-text term. Returns ranked programs."""
    progs = load()
    toks = [t for t in _tokens(term) if len(t) >= 4]  # 3-char tokens match far too much
    if not toks:
        return []
    scored = []
    for p in progs:
        name = (p.get("name") or "").lower()
        aliases = " ".join(p.get("aliases") or []).lower() if isinstance(p.get("aliases"), list) else ""
        tags = " ".join(str(t) for t in (p.get("tags") or [])).lower() if isinstance(p.get("tags"), list) else ""
        cat = (p.get("category") or "").lower()
        desc = (p.get("description") or "").lower()

        s = 0.0
        for t in toks:
            if _word_hit(t, name):
                s += 3.0
            if aliases and _word_hit(t, aliases):
                s += 2.5
            if tags and _word_hit(t, tags):
                s += 2.0
            if _word_hit(t, cat):
                s += 1.5
            if _word_hit(t, desc):
                s += 0.6
        if s <= 0:
            continue
        s = s / len(toks)
        # weight by payout quality so a better-paying program outranks a passing mention
        s *= 0.7 + 0.3 * min(_rate_value(p), 50) / 50.0
        if s >= min_score:
            scored.append((s, p))
    scored.sort(key=lambda kv: -kv[0])
    out = []
    for s, p in scored[:limit]:
        out.append({
            "name": p.get("name"), "score": round(s, 2),
            "category": p.get("category"),
            "commission_rate": (p.get("commission") or {}).get("rate"),
            "commission_type": (p.get("commission") or {}).get("type"),
            "commission_value": _rate_value(p),
            "cookie_days": p.get("cookieDays"),
            "network": p.get("network"),
            "url": p.get("url") or p.get("signupUrl"),
            "description": (p.get("description") or "")[:220],
        })
    return out


def best_rate(term):
    m = match(term, limit=5)
    if not m:
        return 0.0, "", []
    b = max(m, key=lambda x: x["commission_value"] or 0)
    return b["commission_value"] or 0.0, b["name"] or "", [x["name"] for x in m[:3]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--match")
    ap.add_argument("--stats", action="store_true")
    a = ap.parse_args()
    if a.refresh:
        ps = refresh(verbose=True)
        print(f"cached {len(ps)} programs -> {CACHE}")
    if a.stats:
        ps = load()
        cats = {}
        for p in ps:
            cats[p.get("category") or "?"] = cats.get(p.get("category") or "?", 0) + 1
        print(f"programs: {len(ps)}")
        for c, n in sorted(cats.items(), key=lambda kv: -kv[1])[:15]:
            print(f"  {n:4d}  {c}")
    if a.match:
        for r in match(a.match, 12):
            print(f"  {r['score']:5.2f}  {r['name'][:26]:26s} {str(r['commission_rate']):>5s} "
                  f"{str(r['commission_type'] or ''):9s} cookie {r['cookie_days']}d  {r['network']}")


if __name__ == "__main__":
    main()
