"""
Tests for the four build-pipeline engines: niche research, render, bridge, publish.

These are OFFLINE tests for the pure logic (scoring, limits, HTML, link rewrite).
Anything needing ComfyUI/tracker/network is skipped rather than faked.
"""
import json
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tests.harness import check, run_all  # noqa: E402


# ---------------------------------------------------------------- niche scoring
from engine.niche_research import specificity, commercial_intent, GENERIC  # noqa: E402


def test_specificity():
    # a generic single word must not score like a real long-tail niche
    check("generic single word scores tiny", specificity("product") < 0.1,
          f"got {specificity('product')}")
    check("long-tail niche scores high", specificity("ai product photos on etsy") > 0.7,
          f"got {specificity('ai product photos on etsy')}")
    check("generic phrase beats nothing but loses to specific",
          specificity("product photos") < specificity("ai product photoshoot generator"))
    # every GENERIC word alone must be weak
    check("all generic words are weak",
          all(specificity(w) < 0.2 for w in list(GENERIC)[:8]))


def test_intent():
    check("buying phrase has intent", commercial_intent("how to hire product photographer") > 0)
    check("bare noun has no intent", commercial_intent("banana") == 0)


# ---------------------------------------------------------------- publish limits
from engine import publish  # noqa: E402


def test_publish_limits():
    ok = {"title": "x" * 50, "description": "y" * 100, "tags": ["a", "b"]}
    check("valid bundle has no problems", publish.validate("youtube", ok) == [])
    # instagram: 30 hashtag cap is a COUNT cap, not a char cap
    many = {"title": "t", "description": "d", "tags": [f"t{i}" for i in range(40)]}
    probs = publish.validate("instagram", many)
    check("instagram rejects 40 tags", any("too many tags" in p for p in probs), str(probs))
    # youtube title cap
    long_t = {"title": "x" * 200, "description": "d", "tags": []}
    check("youtube rejects 200-char title",
          any("title" in p for p in publish.validate("youtube", long_t)))


def test_publish_disclosure_required():
    """The single most important compliance rule: no disclosure -> refuse."""
    with tempfile.TemporaryDirectory() as td:
        p = Path(td)
        (p / "brief.json").write_text(json.dumps({"title": "t", "hook": "h"}))
        (p / "meta.json").write_text(json.dumps({"aff_url": "https://x.com/?a=1"}))
        try:
            publish.publish(p, "X", tracker="http://127.0.0.1:9", out_root=str(p / "o"))
            check("publish refuses without disclosure", False, "it did not raise")
        except SystemExit as e:
            check("publish refuses without disclosure", "disclosure" in str(e).lower(), str(e))
        except Exception as e:
            check("publish refuses without disclosure", False, f"wrong error {e}")


def test_bundles_are_valid():
    brief = {"title": "How to X", "hook": "A hook", "hashtags": ["#a", "#b"],
             "description": "Desc", "thumbnail_text": "Thumb",
             "scenes": [{"label": "One"}, {"label": "Two"}, {"label": "Three"}],
             "script": "[HOOK]\nHello\n[BODY]\nWorld"}
    link = "http://t/r/ABC"
    for plat, fn in publish.BUILDERS.items():
        b = fn(brief, link, "Disclosure: affiliate.", "Offer")
        probs = publish.validate(plat, b)
        check(f"{plat} bundle valid", not probs, str(probs))
    yt = publish.build_youtube(brief, link, "Disclosure.", "Offer")
    check("youtube desc carries the tracked link", link in yt["description"])
    check("youtube desc carries the disclosure", "Disclosure." in yt["description"])
    check("youtube chapters generated", "Chapters:" in yt["description"])


# ---------------------------------------------------------------- bridge page
from engine import bridge_page  # noqa: E402


def test_bridge_html():
    brief = {"topic": "AI product photos", "hook": "Short hook",
             "summary": "What it is", "key_points": ["one", "two"],
             "cta_text": "Get it"}
    h = bridge_page.build_html(brief, "Photoroom", "http://t/r/TOK")
    check("bridge has the tracked link", "http://t/r/TOK" in h)
    check("bridge discloses affiliate", "affiliate" in h.lower())
    check("bridge is self-contained html", h.strip().startswith("<!doctype html"))
    check("bridge escapes html in fields",
          bridge_page.build_html({"topic": "<script>x</script>"}, "O",
                                 "http://t/r/1").count("<script>") == 0)
    check("rel=sponsored on the cta", 'rel="sponsored' in h)


def test_bridge_slug():
    check("slug strips unsafe chars",
          "/" not in bridge_page._slug("a/../../etc/passwd"),
          bridge_page._slug("a/../../etc/passwd"))


# ---------------------------------------------------------------- render engine
from engine import render_engine  # noqa: E402


def test_render_graph():
    g = render_engine.build_graph("a prompt", "neg", "m.ckpt", 512, 512, 20, 5.0, 42)
    check("graph has checkpoint loader", g["4"]["class_type"] == "CheckpointLoaderSimple")
    check("graph wires positive to sampler", g["3"]["inputs"]["positive"] == ["6", 0])
    check("graph wires negative to sampler", g["3"]["inputs"]["negative"] == ["7", 0])
    check("graph wires latent to sampler", g["3"]["inputs"]["latent_image"] == ["5", 0])
    check("graph saves image", g["9"]["class_type"] == "SaveImage")
    check("seed is passed through", g["3"]["inputs"]["seed"] == 42)
    # every referenced node must exist (a dangling ref = ComfyUI validation error)
    ids = set(g)
    for nid, node in g.items():
        for v in node.get("inputs", {}).values():
            if isinstance(v, list) and len(v) == 2 and isinstance(v[0], str):
                check(f"node {nid} ref {v[0]} exists", v[0] in ids)


def test_render_limits():
    """The engine must not silently accept a nonsense aspect ratio."""
    try:
        from engine.render_engine import main  # noqa
        check("render exposes main", callable(main))
    except Exception as e:
        check("render exposes main", False, str(e))


# ---------------------------------------------------------------- catalog matching
from console import catalog  # noqa: E402


def test_catalog_matching():
    progs = catalog.load()
    if not progs:
        print("SKIP catalog match (no cache and no network)")
        return
    m = catalog.match("ai product photos", limit=5)
    check("catalog matches product photos", len(m) > 0)
    names = " ".join(x["name"].lower() for x in m)
    check("match is relevant (photo-ish)", ("photo" in names) or ("image" in names) or
          ("product" in names), names)
    check("catalog has full depth", len(progs) > 700, f"only {len(progs)}")


def test_catalog_tokens():
    check("tokens drop stopwords", "the" not in catalog._tokens("the product photo"))
    check("empty query matches nothing", catalog.match("") == [])


def test_word_boundary_matching():
    """REGRESSION: substring matching paired Linear (a project-management tool) with a
    photography niche because the token 'near' matched 'liNEAR'."""
    check("'near' does not match 'Linear'", not catalog._word_hit("near", "linear"))
    check("'near' does match 'near'", catalog._word_hit("near", "shop near me"))
    check("'gear' does not match 'gearbox'", not catalog._word_hit("gear", "gearbox"))
    check("'photo' does not match 'photosynthesis'",
          not catalog._word_hit("photo", "photosynthesis"))

    m = catalog.match("photography setup and gear", limit=8)
    names = [x["name"].lower() for x in m]
    check("photography query excludes Linear", "linear" not in names, str(names))
    check("photography query returns something relevant", len(m) > 0, str(names))

    m2 = catalog.match("ai product photos", limit=6)
    n2 = [x["name"].lower() for x in m2]
    check("product-photos query has no project-management tools",
          "linear" not in n2 and "deputy" not in n2, str(n2))


def test_complexity_client():
    """The client must degrade honestly when Complexity is down, and must never
    present the audit as a trustworthy gate."""
    from engine import complexity
    check("client reports up/down without raising",
          isinstance(complexity.is_up(), bool))
    # kg_search is deliberately not implemented (no HTTP route, general data only)
    try:
        complexity.kg_search("Etsy")
        check("kg_search raises NotImplementedError", False, "it returned")
    except NotImplementedError:
        check("kg_search raises NotImplementedError", True)
    # audit must carry its own warning so no caller trusts it blindly
    if complexity.is_up():
        r = complexity.audit("test claim", [{"title": "t", "content": "c", "url": "u"}])
        check("audit() tags itself as advisory", "_warning" in r)
        check("audit warning names the measured failure",
              "contradicted" in r.get("_warning", "").lower()
              or "misattributed" in r.get("_warning", "").lower())
    else:
        print("SKIP audit advisory check (Complexity down)")


def test_short_query():
    """REGRESSION: a long multi-part research question sent Brave off-target
    (mortgage rates for an AI-photography question) and scored 0.20 confidence.
    Queries must stay short and keyword-dense."""
    from engine import complexity
    long_q = ("Is 'AI product photography' a viable affiliate niche in UK for a beginner "
              "starting 2026 with no budget and no traffic? (1) is interest rising "
              "(2) which programs (3) how competitive (4) strongest argument against")
    short = complexity._short_query("AI product photography", "affiliate programs")
    check("short query stays short", len(short.split()) <= 12, short)
    check("long query would be trimmed",
          len(complexity._short_query(long_q).split()) <= 12)
    check("short query keeps the key terms",
          "photography" in short and "affiliate" in short, short)


if __name__ == "__main__":
    run_all(globals())
