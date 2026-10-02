"""
Complexity client: use the local Complexity stack (Multi-Model Research & Debate)
as a research engine for the affiliate operation.

Complexity runs at http://127.0.0.1:8000 and offers four things useful here:

  deep_research()  multi-pass research with gap detection + citations   <-- USE THIS
  research()       single-pass answer with web evidence                <-- USE THIS
  council()        multi-model debate + judge synthesis                <-- USE for decisions
  kg_search()      entity grounding (12.5M nodes, incl. YAGO)          <-- USE for facts

  audit()          claim-citation verification                        <-- ADVISORY ONLY

IMPORTANT — read before trusting audit():
Measured on 2026-10-02, `audit()` marked BOTH of these as "verified":
  1. A fabricated program association (Linear, a project-management tool, presented
     as a photography affiliate partner) even though the supplied source explicitly
     said Linear was "unrelated to photography".
  2. A blatantly contradicted statistic (claimed 95%/365-day; source said 20%/30-day).
It only returned "unsupported" when the source had essentially ZERO topical overlap
(a claim about the moon vs a weather forecast).

So audit() detects "the source is about a completely different subject", but NOT
"the source contradicts this claim" or "this claim misattributes the source".
It MUST NOT be used as a publish gate. The real defences remain the word-boundary
program matching and the specificity filter in the niche researcher.

Usage:
  python -m engine.complexity --ask "which UK affiliate networks accept beginners?"
  python -m engine.complexity --deep "recurring commission AI tool affiliate programs UK"
  python -m engine.complexity --council "Is AI product photography a viable affiliate niche?"
  python -m engine.complexity --kg "PartnerStack"
"""
import argparse
import json
import time
import urllib.error
import urllib.parse
import urllib.request

BASE = "http://127.0.0.1:8000"


def _post(path, payload, timeout=600):
    req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode())
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def _get(path, timeout=30):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode())


def is_up(timeout=5):
    try:
        _get("/health", timeout=timeout)
        return True
    except Exception:
        return False


def require_up():
    if not is_up():
        raise SystemExit(
            f"Complexity is not running at {BASE}. Start it with:\n"
            "  cd ~/CodexWork/mac-ai-front-end/backend && "
            "env -u PYTHONPATH .venv/bin/uvicorn app.main:app --host 127.0.0.1 --port 8000"
        )


def deep_research(query, max_passes=3, result_count=20,
                  model="deepseek-v4-pro:cloud", provider="ollama"):
    """Multi-pass research with gap detection. The best mode for landscape questions.

    Returns {answer, sources_count, passes, gaps_found, confidence, evidence, ...}
    `confidence` is honest — it returns low values when evidence is thin."""
    require_up()
    return _post("/api/deep-research", {
        "query": query, "max_passes": max_passes, "result_count": result_count,
        "model": model, "provider": provider,
    })


def research(query, depth="standard", result_count=20,
             model="deepseek-v4-pro:cloud", provider="ollama"):
    """Single-pass cited answer.

    NOTE: /api/specialist/research returns HTTP 500 on the live server as of
    2026-10-02. We therefore fall back to deep_research with a single pass, which
    is the same thing and actually works."""
    require_up()
    try:
        return _post("/api/specialist/research", {
            "query": query, "depth": depth, "model": model,
            "provider": provider, "result_count": result_count,
        })
    except urllib.error.HTTPError as e:
        if e.code >= 500:
            r = deep_research(query, max_passes=1, result_count=result_count,
                              model=model, provider=provider)
            r["_fallback"] = "specialist/research returned 5xx; used deep_research(1 pass)"
            return r
        raise


JUDGE_FALLBACKS = ("kimi-k2.5:cloud", "deepseek-v4-pro:cloud", "glm-5.3:cloud")


def council(question, models=None, judge_model="kimi-k2.5:cloud",
            rounds=2, use_web=True):
    """Multi-model debate with judge synthesis. For genuine decisions where you want
    independent perspectives adversarially tested.

    If the chosen judge model is unavailable, retries with each fallback rather than
    returning a debate with a broken judge."""
    require_up()
    if models is None:
        models = [
            {"model": "deepseek-v4-pro:cloud", "provider": "ollama"},
            {"model": "minimax-m3:cloud", "provider": "ollama"},
        ]
    tried = []
    for jm in (judge_model,) + tuple(j for j in JUDGE_FALLBACKS if j != judge_model):
        if jm in tried:
            continue
        tried.append(jm)
        r = _post("/api/specialist/council", {
            "question": question, "models": models, "judge_model": jm,
            "judge_provider": "ollama", "rounds": rounds, "use_web": use_web,
        })
        syn = r.get("synthesis") or ""
        if "[Judge error:" not in syn and "Judge error" not in syn[:60]:
            r["_judge_used"] = jm
            return r
        last = r
    last["_judge_used"] = None
    last["_judge_error"] = "every judge model failed"
    return last


def kg_search(query, limit=10):
    """Entity lookup against the knowledge graph.

    NOTE: there is no HTTP route for this on the live Complexity server (only
    /api/specialist/*, /api/deep-research, /api/evidence/audit). It is reachable
    only via the MCP tool. Measured 2026-10-02: the graph holds GENERAL entities
    (YAGO/WordNet) — searching "Etsy" returns 'betsy'/'betsygate', and
    "PartnerStack" returns nothing. It carries NO affiliate/commercial data, so it
    is not useful for this operation. Kept for completeness only."""
    raise NotImplementedError(
        "Knowledge graph has no HTTP route and holds only general-encyclopaedic "
        "entities (no affiliate/commercial data). Use the MCP tool if you need it.")


def audit(answer, sources):
    """Claim-citation verification. ADVISORY ONLY — see the module docstring.

    Returns the raw audit plus an explicit warning so no caller treats a
    'verified' verdict here as proof."""
    require_up()
    fixed = []
    for i, s in enumerate(sources, 1):
        s = dict(s)
        s.setdefault("source_id", f"S{i}")
        fixed.append(s)
    r = _post("/api/specialist/audit", {"answer": answer, "sources": fixed})
    r["_warning"] = ("audit() has been measured giving 'verified' verdicts to contradicted "
                     "and misattributed claims. Do NOT use as a publish gate.")
    return r


def _print_research(r, full=False):
    print(f"  sources={r.get('sources_count')} passes={r.get('passes')} "
          f"confidence={r.get('confidence')}")
    if r.get("gaps_found"):
        print(f"  gaps: {len(r['gaps_found'])} identified")
    print()
    print(r.get("answer") or "(no answer)")


def _short_query(term, extra=""):
    """Build a SHORT, keyword-dense query.

    Measured 2026-10-02: a long multi-part question ("...(1) is interest rising
    (2) which programs (3) how competitive (4) strongest argument against, UK,
    2026, no budget...") sent Brave off-target — it returned UK mortgage rates and
    house-price news, and the research scored confidence 0.20. The same subject as
    a short keyword query returned 16 directly relevant sources at confidence 0.60.
    Keep queries short; let the multi-pass gap detection do the expanding."""
    words = [w for w in f"{term} {extra}".split() if w]
    return " ".join(words[:12])


def validate_niche(term, geo="UK", passes=2, competition=True):
    """Validate a niche with cited research, using SHORT focused queries.

    Runs one query for the money question and (optionally) a second for
    competition/saturation, then merges. Returns the primary result with the
    second pass's evidence attached."""
    money = deep_research(_short_query(term, "affiliate programs commission rates"),
                          max_passes=passes, result_count=15)
    if not competition:
        return money
    try:
        comp = deep_research(_short_query(term, "affiliate niche saturated competition"),
                             max_passes=1, result_count=12)
        seen = {(e.get("url") or "") for e in (money.get("evidence") or [])}
        merged = list(money.get("evidence") or [])
        for e in (comp.get("evidence") or []):
            if (e.get("url") or "") not in seen:
                merged.append(e)
                seen.add(e.get("url") or "")
        money["evidence"] = merged
        money["competition_note"] = (comp.get("answer") or "")[:1200]
        # the weaker of the two confidences is the honest overall figure
        try:
            money["confidence"] = min(float(money.get("confidence") or 0),
                                      float(comp.get("confidence") or 0))
        except Exception:
            pass
    except Exception as e:
        money["competition_note"] = f"(competition pass failed: {e})"
    return money


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ask", help="single-pass cited research")
    ap.add_argument("--deep", help="multi-pass deep research")
    ap.add_argument("--council", help="multi-model debate")
    ap.add_argument("--kg", help="knowledge graph entity lookup")
    ap.add_argument("--niche", help="validate a niche name with cited research")
    ap.add_argument("--passes", type=int, default=3)
    ap.add_argument("--out", help="write the raw JSON here")
    a = ap.parse_args()

    if not is_up():
        raise SystemExit(f"Complexity not reachable at {BASE}")

    if a.kg:
        try:
            d = kg_search(a.kg)
            ents = d.get("results") or d.get("entities") or d
            for e in (ents if isinstance(ents, list) else [])[:15]:
                print(f"  {e.get('name')}  [{e.get('type')}]  {e.get('source','')}")
        except NotImplementedError as e:
            print(f"  KG unavailable: {e}")
        return

    if a.niche:
        print(f"Validating niche: {a.niche}")
        r = validate_niche(a.niche, passes=a.passes)
        _print_research(r)
        if a.out:
            open(a.out, "w").write(json.dumps(r, indent=2))
        return

    if a.council:
        print(f"Council debating: {a.council}")
        r = council(a.council)
        print(f"\nJUDGE ({r.get('judge_model')}):\n")
        print(r.get("synthesis") or "")
        for p in r.get("participant_responses") or []:
            m = p.get("model") if isinstance(p, dict) else str(p)
            print(f"\n--- {m} ---")
            print((p.get("response") or p.get("text") or "")[:2500] if isinstance(p, dict) else "")
        if a.out:
            open(a.out, "w").write(json.dumps(r, indent=2))
        return

    if a.deep:
        t = time.time()
        r = deep_research(a.deep, max_passes=a.passes)
        print(f"\nDeep research ({time.time()-t:.0f}s)")
        _print_research(r)
        if a.out:
            open(a.out, "w").write(json.dumps(r, indent=2))
        return

    if a.ask:
        r = research(a.ask)
        print(f"\nResearch")
        print(f"  sources={r.get('sources_count')} model={r.get('model_used')}")
        print()
        print(r.get("answer") or "")
        if a.out:
            open(a.out, "w").write(json.dumps(r, indent=2))
        return

    ap.print_help()


if __name__ == "__main__":
    main()
