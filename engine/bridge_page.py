"""
Bridge page generator: turn a content pack into a fast, self-contained landing page
whose outbound links carry REAL tracker tokens, so clicks are attributed.

A "bridge page" sits between a video/article and the merchant: it warms the visitor,
discloses the affiliate relationship, then hands off to the tracked redirect.

Design choices:
  - Self-contained HTML (inline CSS). No npm, no build step, works offline.
  - Every outbound link points at the TRACKER (/r/{token}), never the raw merchant URL,
    so a click is always attributable. If the tracker is unreachable we say so rather
    than silently shipping untracked links.
  - The affiliate disclosure is REQUIRED and cannot be turned off (ASA/FTC).
  - Optionally creates the offer+campaign in the tracker so tokens are real.

Usage:
  python -m engine.bridge_page --pack content_out/<slug> \
      --offer-url "https://merchant.com/?ref=YOU" --offer-name "Photoroom" \
      --tracker http://127.0.0.1:8788 --out sites
"""
import argparse
import html
import json
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_TRACKER = "http://127.0.0.1:8788"

TEMPLATE = """<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<meta name="description" content="{meta_desc}">
<style>
  :root{{--bg:#0f1115;--card:#171a21;--ink:#e8eaed;--dim:#9aa4b2;--accent:#4da3ff;--warn:#2a2118;--warnink:#e8c07a}}
  @media(prefers-color-scheme:light){{:root{{--bg:#f7f8fa;--card:#fff;--ink:#14181f;--dim:#5a6472;--warn:#fff8e6;--warnink:#7a5b12}}}}
  *{{box-sizing:border-box}}
  body{{margin:0;font:16px/1.6 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif;background:var(--bg);color:var(--ink)}}
  .wrap{{max-width:760px;margin:0 auto;padding:40px 22px 80px}}
  h1{{font-size:1.9rem;line-height:1.25;margin:.2em 0 .4em;letter-spacing:-.01em}}
  h2{{font-size:1.15rem;margin:2em 0 .6em}}
  p{{color:var(--ink)}}
  .lede{{font-size:1.08rem;color:var(--dim)}}
  .card{{background:var(--card);border-radius:14px;padding:22px 24px;margin:20px 0;border:1px solid rgba(128,128,128,.18)}}
  .cta{{display:block;text-align:center;background:var(--accent);color:#04101f;font-weight:650;
        text-decoration:none;padding:15px 22px;border-radius:11px;margin:26px 0 10px;font-size:1.05rem}}
  .cta:hover{{filter:brightness(1.08)}}
  .sub{{text-align:center;color:var(--dim);font-size:.85rem}}
  ul{{padding-left:1.15em}}li{{margin:.45em 0}}
  .disc{{background:var(--warn);color:var(--warnink);border-radius:10px;padding:12px 15px;font-size:.83rem;margin-top:34px}}
  .disc a{{color:inherit}}
  footer{{color:var(--dim);font-size:.8rem;margin-top:26px;text-align:center}}
</style>
</head>
<body>
<div class="wrap">
  <h1>{h1}</h1>
  <p class="lede">{lede}</p>

  <div class="card">
    <h2>What this is</h2>
    <p>{what}</p>
  </div>

  <h2>{list_title}</h2>
  <ul>{bullets}</ul>

  <a class="cta" href="{cta_url}" rel="sponsored nofollow noopener" target="_blank">{cta_text}</a>
  <div class="sub">{cta_sub}</div>

  <div class="disc">
    <strong>Affiliate disclosure:</strong> links on this page are affiliate links. If you buy
    through them I may earn a commission at no extra cost to you. This never affects what I
    recommend &mdash; I only link to tools I would use myself.
    <a href="{disc_url}" rel="nofollow">How I choose tools</a>.
  </div>
  <footer>{footer}</footer>
</div>
</body>
</html>
"""


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "page").lower()).strip("-")[:60]


def _post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def tracker_up(tracker):
    try:
        _get(f"{tracker}/healthz", timeout=5)
        return True
    except Exception:
        return False


def ensure_tracking(tracker, offer_name, offer_url, campaign_name, source="bridge"):
    """Create the offer + campaign, or reuse one with the same name. Returns token."""
    offers = _get(f"{tracker}/api/offers").get("offers", [])
    oid = next((o["id"] for o in offers if o["name"] == offer_name), None)
    if oid is None:
        oid = _post(f"{tracker}/api/offers",
                    {"name": offer_name, "offer_url": offer_url, "currency": "GBP"})["offer_id"]

    campaigns = _get(f"{tracker}/api/campaigns").get("campaigns", [])
    match = next((c for c in campaigns if c["name"] == campaign_name), None)
    if match:
        return match["token"], oid, False
    r = _post(f"{tracker}/api/campaigns",
              {"name": campaign_name, "source": source, "offer_id": oid})
    # The tracker returns {campaign_id, link}; older builds may return {token}.
    # Derive the token defensively so a response-shape change can't silently
    # downgrade us to an untracked page.
    token = r.get("token")
    if not token and "/r/" in (r.get("link") or ""):
        token = r["link"].rsplit("/r/", 1)[-1]
    if not token:
        cs = _get(f"{tracker}/api/campaigns").get("campaigns", [])
        m = next((c for c in cs if c["name"] == campaign_name), None)
        token = m["token"] if m else None
    if not token:
        raise RuntimeError("campaign created but no token could be determined")
    return token, oid, True


def build_html(brief, offer_name, cta_url, disc_url="#", site_name="", pack_slug=""):
    """Compose the page. Missing brief fields degrade gracefully rather than crash."""
    topic = brief.get("topic") or brief.get("title") or offer_name
    hook = brief.get("hook") or ""
    bullets = brief.get("key_points") or brief.get("bullets") or []
    if isinstance(bullets, str):
        bullets = [b.strip("- ").strip() for b in bullets.split("\n") if b.strip()]

    title = html.escape(f"{topic} — {offer_name}" if offer_name else topic)
    h1 = html.escape(brief.get("h1") or topic)
    lede = html.escape(hook or f"A straight-talking look at {topic}.")
    what = html.escape(
        brief.get("summary")
        or brief.get("description")
        or f"This page covers what actually matters when you're choosing {offer_name} "
           f"for {topic}, and skips the fluff.")
    list_title = html.escape(brief.get("list_title") or "What to look for")
    bl = "\n    ".join(f"<li>{html.escape(str(b))}</li>" for b in bullets[:8]) \
        or "<li>Add key points to the content pack to fill this section.</li>"

    return TEMPLATE.format(
        title=title,
        meta_desc=html.escape((brief.get("summary") or lede)[:155]),
        h1=h1, lede=lede, what=what, list_title=list_title, bullets=bl,
        cta_url=html.escape(cta_url, quote=True),
        cta_text=html.escape(brief.get("cta_text") or f"Check out {offer_name}"),
        cta_sub=html.escape(brief.get("cta_sub") or "Opens the official site."),
        disc_url=html.escape(disc_url, quote=True),
        footer=html.escape(site_name or f"© {time.strftime('%Y')} — independent reviews"),
    )


def generate(pack_dir: Path, offer_url, offer_name, tracker=DEFAULT_TRACKER,
             out_root="sites", no_track=False):
    pack_dir = Path(pack_dir)
    brief_p = pack_dir / "brief.json"
    brief = json.loads(brief_p.read_text()) if brief_p.exists() else {}

    slug = _slug(brief.get("topic") or pack_dir.name)
    dest = Path(out_root) / slug
    dest.mkdir(parents=True, exist_ok=True)

    tracked, token = False, None
    if not no_track:
        if not tracker_up(tracker):
            print(f"  ! tracker not reachable at {tracker} — writing UNTRACKED page")
        else:
            try:
                token, oid, created = ensure_tracking(
                    tracker, offer_name, offer_url,
                    campaign_name=f"bridge:{slug}", source="bridge")
                tracked = True
                print(f"  tracked link ready: {tracker}/r/{token}"
                      f"{' (new campaign)' if created else ' (existing)'}")
            except Exception as e:
                print(f"  ! tracker error ({e}) — writing UNTRACKED page")

    cta = f"{tracker}/r/{token}" if tracked else offer_url
    html_out = build_html(brief, offer_name, cta, disc_url="#disclosure",
                          pack_slug=slug, site_name=f"{offer_name} guide")

    (dest / "index.html").write_text(html_out)

    # a tiny build record so we can audit what shipped
    (dest / "page.json").write_text(json.dumps({
        "slug": slug, "source_pack": str(pack_dir), "offer_name": offer_name,
        "offer_url": offer_url, "tracked": tracked, "token": token,
        "cta_url": cta, "ts": int(time.time()),
    }, indent=2))

    # robots + sitemap make it deployable as-is
    (dest / "robots.txt").write_text("User-agent: *\nAllow: /\n")
    print(f"  wrote {dest/'index.html'} ({len(html_out)} bytes)")
    return {"slug": slug, "dir": str(dest), "tracked": tracked, "token": token,
            "cta_url": cta}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--offer-url", required=True, help="your affiliate URL (raw merchant link)")
    ap.add_argument("--offer-name", required=True)
    ap.add_argument("--tracker", default=DEFAULT_TRACKER)
    ap.add_argument("--out", default="sites")
    ap.add_argument("--no-track", action="store_true",
                    help="skip the tracker and emit the raw link (not recommended)")
    a = ap.parse_args()
    r = generate(Path(a.pack), a.offer_url, a.offer_name, a.tracker, a.out, a.no_track)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
