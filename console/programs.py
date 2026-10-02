"""
Affiliate program finder — live data from openaffiliate.dev (public API).

Returns real programs with commission type/rate, cookie window, network, approval
difficulty, payout minimum and tracking method — the exact fields needed to choose
what to promote instead of guessing.

API: GET https://openaffiliate.dev/api/programs?q=<query>&sort=relevance&limit=N
"""
import json
import os
import urllib.parse
import urllib.request

API = os.environ.get("OPEN_AFFILIATE_API", "https://openaffiliate.dev/api/programs")
UA = "affiliate-stack/1.0 (+local operator console)"


def search(query: str, limit: int = 12, timeout: int = 20):
    params = urllib.parse.urlencode({
        "q": query, "sort": "relevance", "limit": limit,
        "utm_source": "local-operator-console",
    })
    url = f"{API}?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = json.loads(r.read().decode())
    return [_normalise(p) for p in data.get("programs", [])]


def _normalise(p: dict) -> dict:
    c = p.get("commission") or {}
    pay = p.get("payout") or {}

    def _int(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0

    def _float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return 0.0

    cookie = _int(p.get("cookieDays"))
    return {
        "name": p.get("name"),
        "slug": p.get("slug"),
        "url": p.get("url"),
        "category": p.get("category"),
        "commission_type": c.get("type"),          # recurring | one-time
        "commission_rate": c.get("rate"),
        "commission_duration": c.get("duration"),
        "cookie_days": cookie,
        "network": p.get("network"),
        "approval": p.get("approval"),
        "approval_time": p.get("approvalTime"),
        "payout_min": _float(pay.get("minimum")),
        "payout_currency": pay.get("currency"),
        "payout_frequency": pay.get("frequency"),
        "tracking": p.get("trackingMethod"),
        "attribution": p.get("attribution"),
        "signup_url": p.get("signupUrl"),
        "description": p.get("description") or p.get("shortDescription"),
        "restrictions": p.get("restrictions") or [],
        "verified": bool(p.get("verified")),
    }


def score(p: dict) -> float:
    """Rank for a zero-budget beginner: recurring + long cookie + auto-approval + low min.
    Fully defensive: a malformed field must never propagate out of here."""
    s = 0.0
    try:
        if (p.get("commission_type") or "").lower() == "recurring":
            s += 40
        rate = p.get("commission_rate") or ""
        s += min(float(str(rate).replace("%", "").strip()), 50)
    except (TypeError, ValueError):
        pass
    try:
        cd = int(p.get("cookie_days") or 0)
        if cd >= 60:
            s += 15
        elif cd >= 30:
            s += 8
    except (TypeError, ValueError):
        pass
    ap = (p.get("approval") or "").lower()
    if ap in ("auto", "automatic", "instant"):
        s += 12
    try:
        if float(p.get("payout_min") or 0) <= 50:
            s += 8
    except (TypeError, ValueError):
        pass
    if p.get("verified"):
        s += 5
    return round(s, 1)


def ranked(query: str, limit: int = 12):
    rows = search(query, limit=limit)
    for r in rows:
        r["score"] = score(r)
    rows.sort(key=lambda x: x["score"], reverse=True)
    return rows
