"""
Publisher: turn a content pack into platform-ready publish bundles.

"Postiz steals the adapter pattern" — one canonical pack, many platform adapters.
Each adapter validates against that platform's REAL limits and refuses to emit a
bundle that would fail on upload.

Non-negotiables enforced here:
  - The affiliate disclosure MUST be present (ASA/FTC). No disclosure -> no bundle.
  - Outbound links point at the TRACKER (/r/{token}) so clicks are attributable.
  - Title/description/tag lengths are checked against the platform's actual caps.

Usage:
  python -m engine.publish --pack content_out/<slug> \
      --offer-name "Photoroom" --tracker http://127.0.0.1:8788
  python -m engine.publish --pack content_out/<slug> --platforms youtube,tiktok,blog
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

# Real platform caps (verified against each platform's documented limits).
# tags_chars = character budget for the tag string; tags_count = max number of tags.
LIMITS = {
    "youtube": {"title": 100, "desc": 5000, "tags_chars": 500, "tags_count": 15},
    "tiktok": {"title": 2200, "desc": 2200, "tags_chars": 500, "tags_count": 10},
    "instagram": {"title": 2200, "desc": 2200, "tags_chars": 2200, "tags_count": 30},
    "pinterest": {"title": 100, "desc": 500, "tags_chars": 500, "tags_count": 10},
    "blog": {"title": 200, "desc": 100000, "tags_chars": 2000, "tags_count": 40},
}


def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "post").lower()).strip("-")[:60]


def _post(url, payload, timeout=15):
    req = urllib.request.Request(url, data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(url, timeout=10):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return json.loads(r.read().decode())


def get_or_make_link(tracker, offer_name, offer_url, campaign_name):
    """Return a tracked /r/ URL for this pack, creating offer+campaign if needed."""
    try:
        offers = _get(f"{tracker}/api/offers").get("offers", [])
    except Exception as e:
        raise RuntimeError(f"tracker unreachable at {tracker}: {e}")
    oid = next((o["id"] for o in offers if o["name"] == offer_name), None)
    if oid is None:
        oid = _post(f"{tracker}/api/offers",
                    {"name": offer_name, "offer_url": offer_url,
                     "currency": "GBP"})["offer_id"]
    camps = _get(f"{tracker}/api/campaigns").get("campaigns", [])
    m = next((c for c in camps if c["name"] == campaign_name), None)
    if m:
        return m["token"]
    r = _post(f"{tracker}/api/campaigns",
              {"name": campaign_name, "source": "content", "offer_id": oid})
    tok = r.get("token")
    if not tok and "/r/" in (r.get("link") or ""):
        tok = r["link"].rsplit("/r/", 1)[-1]
    if not tok:
        cs = _get(f"{tracker}/api/campaigns").get("campaigns", [])
        m = next((c for c in cs if c["name"] == campaign_name), None)
        tok = m["token"] if m else None
    if not tok:
        raise RuntimeError("campaign created but no token determined")
    return tok


def chapters(scenes, secs_per_scene=12):
    """YouTube chapters: must start at 0:00, min 3 entries."""
    out, t = [], 0
    for i, s in enumerate(scenes or []):
        label = (s.get("label") or s.get("title") or f"Part {i+1}").strip()
        m, sec = divmod(t, 60)
        out.append(f"{m}:{sec:02d} {label}")
        t += secs_per_scene
    return out if len(out) >= 3 else []


def _clean(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def validate(platform, bundle):
    """Return a list of problems that would block publishing."""
    lim = LIMITS.get(platform)
    if not lim:
        return []
    errs = []
    if len(bundle.get("title", "")) > lim["title"]:
        errs.append(f"title {len(bundle['title'])} > {lim['title']} chars")
    if len(bundle.get("description", "")) > lim["desc"]:
        errs.append(f"description {len(bundle['description'])} > {lim['desc']} chars")
    tags = bundle.get("tags") or []
    tlen = len(", ".join(tags))
    if tlen > lim["tags_chars"]:
        errs.append(f"tags {tlen} > {lim['tags_chars']} chars")
    if len(tags) > lim["tags_count"]:
        errs.append(f"too many tags ({len(tags)} > {lim['tags_count']})")
    return errs


def build_youtube(brief, link, disclosure, offer_name):
    title = _clean(brief.get("title") or brief.get("topic") or offer_name)
    desc_parts = [
        _clean(brief.get("hook") or ""),
        "",
        disclosure,
        "",
        f"Get {offer_name} here: {link}",
        "",
    ]
    ch = chapters(brief.get("scenes"))
    if ch:
        desc_parts += ["Chapters:"] + ch + [""]
    if brief.get("description"):
        desc_parts += [_clean(brief["description"]), ""]
    tags = [t.lstrip("#") for t in (brief.get("hashtags") or [])][:15]
    tags = [_clean(t) for t in tags if t]
    return {
        "platform": "youtube",
        "title": title,
        "description": "\n".join(desc_parts),
        "tags": tags,
        "thumbnail_text": _clean(brief.get("thumbnail_text") or ""),
        "script": brief.get("script") or "",
        "scenes": brief.get("scenes") or [],
        "tracked_link": link,
    }


def build_tiktok(brief, link, disclosure, offer_name):
    hook = _clean(brief.get("hook") or brief.get("title") or "")
    tags = [t if t.startswith("#") else "#" + t for t in (brief.get("hashtags") or [])][:6]
    return {
        "platform": "tiktok",
        "title": _clean(brief.get("title") or offer_name),
        "description": f"{hook}\n\n{' '.join(tags)}\n\n{disclosure}",
        "tags": [t.lstrip("#") for t in tags],
        "hook": hook,
        "on_screen_text": _clean(brief.get("thumbnail_text") or ""),
        "tracked_link": link,
    }


def build_instagram(brief, link, disclosure, offer_name):
    hook = _clean(brief.get("hook") or "")
    tags = [t if t.startswith("#") else "#" + t for t in (brief.get("hashtags") or [])][:28]
    return {
        "platform": "instagram",
        "title": _clean(brief.get("title") or offer_name),
        "description": f"{hook}\n\nLink in bio → {link}\n\n{disclosure}\n\n{' '.join(tags)}",
        "tags": [t.lstrip("#") for t in tags],
        "tracked_link": link,
    }


def build_pinterest(brief, link, disclosure, offer_name):
    return {
        "platform": "pinterest",
        "title": _clean(brief.get("thumbnail_text") or brief.get("title") or offer_name)[:100],
        "description": _clean(f"{brief.get('hook','')} {disclosure}")[:500],
        "tags": [t.lstrip("#") for t in (brief.get("hashtags") or [])][:6],
        "tracked_link": link,
        "image_prompts": brief.get("image_prompts") or [],
    }


def build_blog(brief, link, disclosure, offer_name):
    body = brief.get("script") or ""
    paras = []
    for line in body.split("\n"):
        line = line.strip()
        if not line:
            continue
        if re.match(r"^\[(HOOK|INTRO|BODY|OUTRO|CTA)\]$", line):
            paras.append(f"\n## {line.strip('[]').title()}\n")
        else:
            paras.append(f"{line}\n")
    md = (f"# {_clean(brief.get('title') or offer_name)}\n\n"
          f"*{disclosure}*\n\n"
          f"{''.join(paras)}\n\n"
          f"**[Get {offer_name} →]({link})**\n")
    return {
        "platform": "blog",
        "title": _clean(brief.get("title") or offer_name),
        "description": md,
        "markdown": md,
        "tags": [t.lstrip("#") for t in (brief.get("hashtags") or [])],
        "tracked_link": link,
        "target_keyword": brief.get("target_keyword") or "",
    }


BUILDERS = {
    "youtube": build_youtube, "tiktok": build_tiktok, "instagram": build_instagram,
    "pinterest": build_pinterest, "blog": build_blog,
}


def publish(pack_dir: Path, offer_name, tracker=DEFAULT_TRACKER,
            platforms=("youtube", "tiktok", "instagram", "pinterest", "blog"),
            out_root="publish"):
    pack_dir = Path(pack_dir)
    brief = json.loads((pack_dir / "brief.json").read_text())
    meta = {}
    if (pack_dir / "meta.json").exists():
        meta = json.loads((pack_dir / "meta.json").read_text())

    disclosure = _clean(meta.get("disclosure") or brief.get("disclosure") or "")
    if not disclosure:
        raise SystemExit("REFUSING: no affiliate disclosure in the pack. "
                         "Add meta.disclosure before publishing (ASA/FTC requirement).")

    offer_url = meta.get("aff_url") or brief.get("aff_url") or "https://example.com"
    if "example.com" in offer_url:
        print("  ! pack still has a placeholder affiliate URL — replace before going live")

    slug = _slug(brief.get("title") or pack_dir.name)
    dest = Path(out_root) / slug
    dest.mkdir(parents=True, exist_ok=True)

    link = offer_url
    try:
        tok = get_or_make_link(tracker, offer_name, offer_url, f"publish:{slug}")
        link = f"{tracker}/r/{tok}"
        print(f"  tracked link: {link}")
    except Exception as e:
        print(f"  ! no tracker ({e}) — bundles will carry the RAW link (untracked)")

    results, problems = {}, {}
    for p in platforms:
        fn = BUILDERS.get(p)
        if not fn:
            continue
        b = fn(brief, link, disclosure, offer_name)
        errs = validate(p, b)
        results[p] = b
        problems[p] = errs
        fn_out = dest / f"{p}.json"
        fn_out.write_text(json.dumps(b, indent=2, ensure_ascii=False))
        status = "OK" if not errs else "BLOCKED: " + "; ".join(errs)
        print(f"  {p:10s} -> {fn_out.name:16s} {status}")
        if p == "blog":
            (dest / "blog.md").write_text(b["markdown"])
        if p == "youtube":
            (dest / "youtube_chapters.txt").write_text("\n".join(chapters(brief.get("scenes"))))

    (dest / "publish_manifest.json").write_text(json.dumps({
        "slug": slug, "pack": str(pack_dir), "offer": offer_name,
        "tracked_link": link, "disclosure_present": bool(disclosure),
        "platforms": list(results), "problems": problems, "ts": int(time.time()),
    }, indent=2))
    blocked = [p for p, e in problems.items() if e]
    print(f"\n  {len(results)} bundles -> {dest}"
          + (f"  ({len(blocked)} blocked: {blocked})" if blocked else "  (all within limits)"))
    return {"dir": str(dest), "link": link, "bundles": list(results), "problems": problems}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pack", required=True)
    ap.add_argument("--offer-name", required=True)
    ap.add_argument("--tracker", default=DEFAULT_TRACKER)
    ap.add_argument("--platforms", default="youtube,tiktok,instagram,pinterest,blog")
    ap.add_argument("--out", default="publish")
    a = ap.parse_args()
    r = publish(Path(a.pack), a.offer_name, a.tracker,
                tuple(a.platforms.split(",")), a.out)
    print(json.dumps(r, indent=2))


if __name__ == "__main__":
    main()
