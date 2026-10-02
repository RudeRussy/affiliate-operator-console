"""
The Operator Console backend.

A local app that guides a solo operator through building income:
  - onboarding wizard -> chooses their path
  - daily task list generated from the 90-day runbook
  - progress tracking against milestones
  - one-click access to the tracker + content/service engines
  - state-aware co-pilot chat

Endpoints:
  GET  /                     the console UI
  GET  /api/state            full current state (profile, tasks, metrics, progress)
  POST /api/profile          save onboarding answers
  GET  /api/tasks            task list (filter: ?status=todo|done)
  POST /api/tasks/{id}/done  mark a task complete / undone
  GET  /api/metrics          metrics summary + milestone progress
  POST /api/metrics          log a metric (clicks, conversions, revenue, content)
  POST /api/copilot          ask the co-pilot (state-aware)
  GET  /api/guardrails       active warnings based on state
"""
import json
import os
import sqlite3
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

from .runbook import TASKS, MILESTONES, PATHS, NICHES, GUARDRAIL_RULES
from .copilot import copilot_reply
from . import jobs, programs

DB = os.environ.get("CONSOLE_DB", str(Path(__file__).resolve().parent / "console.db"))

app = FastAPI(title="operator-console", version="0.1.0")


# ---------- storage ----------
def db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    return c


def init_db():
    c = db()
    c.executescript("""
    CREATE TABLE IF NOT EXISTS profile (
      k TEXT PRIMARY KEY, v TEXT
    );
    CREATE TABLE IF NOT EXISTS task_state (
      task_id TEXT PRIMARY KEY, done INTEGER DEFAULT 0, done_ts INTEGER
    );
    CREATE TABLE IF NOT EXISTS metrics (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER, kind TEXT, value REAL, note TEXT
    );
    CREATE TABLE IF NOT EXISTS events (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      ts INTEGER, text TEXT
    );
    """)
    c.commit()
    c.close()


def get_profile():
    c = db()
    rows = {r["k"]: json.loads(r["v"]) for r in c.execute("SELECT k,v FROM profile")}
    c.close()
    return rows


def set_profile(d):
    c = db()
    for k, v in d.items():
        c.execute("INSERT INTO profile(k,v) VALUES(?,?) "
                  "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (k, json.dumps(v)))
    c.commit()
    c.close()


def task_done_map():
    c = db()
    rows = {r["task_id"]: r["done"] for r in c.execute("SELECT task_id,done FROM task_state")}
    c.close()
    return rows


def metrics_summary():
    c = db()
    rows = list(c.execute("SELECT kind, COALESCE(SUM(value),0) total, COUNT(*) n "
                          "FROM metrics GROUP BY kind"))
    c.close()
    return {r["kind"]: {"total": r["total"], "count": r["n"]} for r in rows}


def log_event(text):
    c = db()
    c.execute("INSERT INTO events(ts,text) VALUES(?,?)", (int(time.time()), text))
    c.commit()
    c.close()


@app.on_event("startup")
def _startup():
    init_db()
    jobs.init_jobs_table()


# ---------- helpers ----------
def active_tasks():
    prof = get_profile()
    path = prof.get("path", "both")
    niche = prof.get("niche")
    done = task_done_map()
    out = []
    for t in TASKS:
        if path != "both" and t["path"] not in (path, "common"):
            continue
        out.append({**t, "done": bool(done.get(t["id"], 0))})
    out.sort(key=lambda x: (x["done"], x["order"]))
    return out


def compute_guardrails():
    prof = get_profile()
    metrics = metrics_summary()
    warnings = []
    for rule in GUARDRAIL_RULES:
        try:
            if rule["check"](prof, metrics):
                warnings.append({"id": rule["id"], "level": rule["level"],
                                 "message": rule["message"], "fix": rule["fix"]})
        except Exception:
            pass
    return warnings


# ---------- endpoints ----------
@app.get("/api/state")
def state():
    prof = get_profile()
    tasks = active_tasks()
    metrics = metrics_summary()
    done_n = sum(1 for t in tasks if t["done"])
    return {
        "profile": prof,
        "tasks": tasks,
        "metrics": metrics,
        "progress": {"done": done_n, "total": len(tasks),
                     "pct": round(100 * done_n / len(tasks)) if tasks else 0},
        "milestones": MILESTONES,
        "guardrails": compute_guardrails(),
        "paths": PATHS, "niches": NICHES,
    }


@app.post("/api/profile")
def save_profile(payload: dict):
    # Validate against the known sets. Without this, an unknown niche value silently
    # bypasses the guardrail checks (e.g. the finance/FCA trap warning).
    if "path" in payload and payload["path"] not in PATHS:
        raise HTTPException(400, f"path must be one of {sorted(PATHS)}")
    if "niche" in payload:
        valid = {n["id"] for n in NICHES}
        if payload["niche"] not in valid:
            raise HTTPException(400, f"niche must be one of {sorted(valid)}")
    # drop anything that isn't a known scalar profile field
    allowed = {"path", "niche", "has_email_list", "uses_link_shortener", "has_disclosure"}
    clean = {k: v for k, v in payload.items() if k in allowed}
    if not clean:
        raise HTTPException(400, "no valid profile fields supplied")
    set_profile(clean)
    log_event(f"profile saved: {list(clean.keys())}")
    return {"ok": True, "profile": get_profile()}


@app.get("/api/tasks")
def tasks(status: str = "all"):
    t = active_tasks()
    if status == "todo":
        t = [x for x in t if not x["done"]]
    elif status == "done":
        t = [x for x in t if x["done"]]
    return {"tasks": t}


@app.post("/api/tasks/{task_id}/done")
def mark_done(task_id: str, payload: dict | None = None):
    done = 1 if (payload or {}).get("done", True) else 0
    c = db()
    c.execute("INSERT INTO task_state(task_id,done,done_ts) VALUES(?,?,?) "
              "ON CONFLICT(task_id) DO UPDATE SET done=excluded.done, done_ts=excluded.done_ts",
              (task_id, done, int(time.time()) if done else None))
    c.commit()
    c.close()
    log_event(f"task {task_id} {'done' if done else 'reopened'}")
    return {"ok": True, "task_id": task_id, "done": bool(done)}


@app.get("/api/metrics")
def metrics():
    return {"metrics": metrics_summary(), "milestones": MILESTONES}


@app.post("/api/metrics")
def log_metric(payload: dict):
    kind = payload.get("kind")
    if kind not in ("clicks", "conversions", "revenue", "content", "outreach", "clients"):
        raise HTTPException(400, "invalid metric kind")
    c = db()
    c.execute("INSERT INTO metrics(ts,kind,value,note) VALUES(?,?,?,?)",
              (int(time.time()), kind, float(payload.get("value", 0)),
               payload.get("note", "")))
    c.commit()
    c.close()
    log_event(f"metric {kind} +{payload.get('value')}")
    return {"ok": True, "metrics": metrics_summary()}


@app.post("/api/copilot")
def copilot(payload: dict):
    q = (payload.get("q") or "").strip()
    if not q:
        raise HTTPException(400, "empty question")
    s = state()
    answer = copilot_reply(q, s)
    return {"answer": answer}


@app.get("/api/guardrails")
def guardrails():
    return {"guardrails": compute_guardrails()}


@app.get("/api/events")
def events(limit: int = 50):
    c = db()
    rows = [dict(r) for r in c.execute(
        "SELECT ts,text FROM events ORDER BY id DESC LIMIT ?", (limit,))]
    c.close()
    return {"events": rows}


# ---------- engines / jobs ----------
@app.get("/api/content-packs")
def content_packs():
    return {"packs": jobs.list_content_packs()}


@app.get("/api/service-kits")
def service_kits():
    return {"kits": jobs.list_service_kits()}


@app.get("/api/doc")
def read_doc(base: str, sub: str, file: str):
    if base not in ("content_out", "service_kit"):
        raise HTTPException(400, "invalid base")
    txt = jobs.read_file_in(base, sub, file)
    if txt is None:
        raise HTTPException(404, "not found")
    return {"content": txt}


@app.post("/api/jobs/content")
def job_content(payload: dict):
    for k in ("topic", "offer", "aff_url"):
        if not payload.get(k):
            raise HTTPException(400, f"{k} required")
    jid = jobs.run_content_pack(payload["topic"], payload["offer"], payload["aff_url"],
                                payload.get("model", "mistral-large-3:675b-cloud"))
    log_event(f"content pack job {jid}")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/service-kit")
def job_service_kit(payload: dict):
    if not payload.get("service"):
        raise HTTPException(400, "service required")
    jid = jobs.run_service_kit(payload["service"],
                               payload.get("niche", "small e-commerce sellers"),
                               payload.get("price_range", "£75-£400"),
                               payload.get("model", "mistral-large-3:675b-cloud"))
    log_event(f"service kit job {jid}")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/audit")
def job_audit(payload: dict):
    d = (payload.get("dir") or "").strip()
    if not d:
        raise HTTPException(400, "dir required")
    _require_inside_roots(d)
    jid = jobs.run_claims_audit(d, payload.get("model", "gpt-oss:120b-cloud"))
    log_event(f"claims audit job {jid}")
    return {"ok": True, "job_id": jid}


def _require_inside_roots(d: str):
    """Never hand an arbitrary path to a subprocess."""
    root = Path(d).resolve()
    allowed = [(Path(__file__).resolve().parent.parent / x).resolve()
               for x in ("service_kit", "content_out")]
    for a in allowed:
        try:
            root.relative_to(a)
            return
        except ValueError:
            continue
    raise HTTPException(400, "dir must be inside service_kit/ or content_out/")


@app.post("/api/jobs/fix")
def job_fix(payload: dict):
    d = (payload.get("dir") or "").strip()
    if not d:
        raise HTTPException(400, "dir required")
    _require_inside_roots(d)
    jid = jobs.run_claims_fix(d)
    log_event(f"claims fix job {jid} on {d}")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/harden")
def job_harden(payload: dict):
    """audit then fix — the safe pipeline."""
    d = (payload.get("dir") or "").strip()
    if not d:
        raise HTTPException(400, "dir required")
    _require_inside_roots(d)
    jid = jobs.start("harden", [jobs.PY, "-c",
                                "import subprocess,sys;"
                                "subprocess.run([sys.executable,'-m','scripts.claims_audit','--dir',sys.argv[1]]);"
                                "subprocess.run([sys.executable,'-m','scripts.claims_fix','--dir',sys.argv[1]])",
                                d], d)
    log_event(f"harden job {jid} on {d}")
    return {"ok": True, "job_id": jid}


# ---------- niche research / render / bridge / publish ----------

@app.post("/api/jobs/niche")
def job_niche(payload: dict):
    seeds = (payload.get("seeds") or "").strip()
    if not seeds:
        raise HTTPException(400, "seeds required (comma-separated topics)")
    if len(seeds) > 400:
        raise HTTPException(400, "seeds too long")
    model = payload.get("model") or None
    jid = jobs.run_niche_research(seeds, payload.get("geo", "GB"), model)
    log_event(f"niche research job {jid}: {seeds}")
    return {"ok": True, "job_id": jid}


# ---------- outreach (the money side) ----------

@app.get("/api/outreach/find")
def api_find(niche: str = "handmade jewellery", location: str = "UK",
             limit: int = 12, min_score: float = 5.0):
    """Discover real leads via the Brave index of site:etsy.com/shop. Scoring is
    explainable — every lead carries the reasons it ranked. Companies are flagged so
    the compliance gate blocks cold email automatically."""
    from engine import leadfinder as LF
    from engine import outreach as O
    O.init_db()
    q = LF.shop_query(niche, location)
    recs = LF.find("shopindex", q, max(1, min(limit, 20)))
    added, low, supp = LF.import_to_outreach(recs, min_score)
    return {"ok": True, "query": q, "found": len(recs), "imported": added,
            "skipped_low": low, "skipped_suppressed": supp,
            "leads": [{"shop": r.get("shop"), "score": r.get("score"),
                       "why": r.get("why", []),
                       "ltd": bool(r.get("is_limited_company"))} for r in recs]}


@app.get("/api/outreach/leads")
def api_leads(channel: str = None):
    from engine import outreach as O
    O.init_db()
    return {"ok": True, "leads": O.list_leads(channel),
            "due": O.due_followups(), "stats": O.stats(),
            "caps": {c: {"cap": O.DAILY_CAPS.get(c, O.DAILY_CAPS["default"]),
                         "sent": O.touches_today(c)} for c in
                     ("email", "etsy", "reddit", "facebook", "discord", "instagram")}}


@app.post("/api/outreach/lead")
def api_add_lead(payload: dict):
    from engine import outreach as O
    O.init_db()
    ch = (payload.get("channel") or "etsy").strip()
    h = (payload.get("handle") or "").strip()
    if ch not in O.DAILY_CAPS:
        raise HTTPException(400, f"unknown channel {ch}")
    lid = O.add_lead(
        name=(payload.get("name") or "").strip()[:120],
        business=(payload.get("business") or "").strip()[:160],
        channel=ch, handle=h[:300],
        niche=(payload.get("niche") or "").strip()[:60],
        notes=(payload.get("notes") or "").strip()[:600],
        is_limited_company=bool(payload.get("is_limited_company")),
        source="console")
    if lid is None:
        return {"ok": True, "skipped": True,
                "note": "that person previously opted out — permanently suppressed"}
    return {"ok": True, "lead_id": lid}


@app.post("/api/outreach/suppress")
def api_suppress(payload: dict):
    from engine import outreach as O
    ch = (payload.get("channel") or "").strip()
    h = (payload.get("handle") or "").strip()
    if not ch or not h:
        raise HTTPException(400, "channel and handle required")
    O.suppress(ch, h, payload.get("reason") or "manual")
    return {"ok": True}


@app.get("/api/outreach/draft")
def api_draft(lead_id: int, channel: str = None, kind: str = "first"):
    from engine import outreach as O
    O.init_db()
    lead = O.get_lead(lead_id)
    if not lead:
        raise HTTPException(404, "no such lead")
    ch = channel or lead["channel"]
    body, problems = O.draft(lead, ch, kind)
    ok, capmsg = O.can_send(ch)
    return {"ok": True, "channel": ch, "body": body, "problems": problems,
            "can_send": ok, "cap": capmsg}


@app.post("/api/outreach/log")
def api_log(payload: dict):
    from engine import outreach as O
    O.init_db()
    lid = payload.get("lead_id")
    if not isinstance(lid, int):
        raise HTTPException(400, "lead_id required")
    lead = O.get_lead(lid)
    if not lead:
        raise HTTPException(404, "no such lead")
    ch = payload.get("channel") or lead["channel"]
    status = payload.get("status") or "sent"
    if status not in ("sent", "replied", "no_reply", "declined", "won", "suppressed"):
        raise HTTPException(400, "bad status")
    if status == "sent":
        ok, msg = O.can_send(ch)
        if not ok:
            raise HTTPException(429, msg)
    O.log_touch(lid, ch, payload.get("kind") or "first", "", status)
    return {"ok": True, "suppressed": status in ("declined", "opt-out", "suppressed")}


@app.post("/api/outreach/warmup")
def api_warmup(payload: dict):
    from engine import outreach as O
    ch = (payload.get("channel") or "").strip()
    com = (payload.get("community") or "").strip()
    if not ch or not com:
        raise HTTPException(400, "channel and community required")
    n = O.log_warmup(ch, com, int(payload.get("n") or 1))
    ok, have, need = O.warmup_ok(ch, com)
    return {"ok": True, "posts": n, "ready": ok, "need": need}


@app.get("/api/niches")
def get_niches():
    """Latest niche research result, if any."""
    p = Path(__file__).resolve().parent.parent / "niches_live.json"
    if not p.exists():
        return {"ok": True, "niches": [], "candidates": [], "note": "no research run yet"}
    try:
        d = json.loads(p.read_text())
        return {"ok": True, "niches": d.get("niches", []),
                "candidates": d.get("candidates", [])[:40],
                "seeds": d.get("seeds", []), "ts": d.get("ts")}
    except Exception as e:
        raise HTTPException(500, f"bad niche file: {e}")


def _require_pack(d: str):
    """A pack dir must be a real directory inside content_out/."""
    root = (Path(__file__).resolve().parent.parent / "content_out").resolve()
    p = (Path(__file__).resolve().parent.parent / d).resolve() if not Path(d).is_absolute() else Path(d).resolve()
    try:
        p.relative_to(root)
    except ValueError:
        raise HTTPException(400, "pack must be inside content_out/")
    if not p.is_dir():
        raise HTTPException(400, "pack dir not found")
    return str(p)


@app.post("/api/jobs/render")
def job_render(payload: dict):
    d = _require_pack((payload.get("pack") or "").strip())
    steps = int(payload.get("steps") or 28)
    if not 1 <= steps <= 150:
        raise HTTPException(400, "steps must be 1..150")
    jid = jobs.run_render(d, steps, payload.get("limit"), payload.get("aspect", "16:9"))
    log_event(f"render job {jid} on {d}")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/bridge")
def job_bridge(payload: dict):
    d = _require_pack((payload.get("pack") or "").strip())
    url = (payload.get("offer_url") or "").strip()
    name = (payload.get("offer_name") or "").strip()
    if not url.startswith(("http://", "https://")):
        raise HTTPException(400, "offer_url must be an absolute http(s) URL")
    if not name:
        raise HTTPException(400, "offer_name required")
    jid = jobs.run_bridge_page(d, url, name)
    log_event(f"bridge page job {jid} for {name}")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/publish")
def job_publish(payload: dict):
    d = _require_pack((payload.get("pack") or "").strip())
    name = (payload.get("offer_name") or "").strip()
    if not name:
        raise HTTPException(400, "offer_name required")
    plats = payload.get("platforms") or "youtube,tiktok,instagram,pinterest,blog"
    jid = jobs.run_publish(d, name, plats)
    log_event(f"publish job {jid} for {name} ({plats})")
    return {"ok": True, "job_id": jid}


@app.post("/api/jobs/review")
def job_review(payload: dict | None = None):
    jid = jobs.run_code_review(model=(payload or {}).get("model",
                                                        "mistral-large-3:675b-cloud"))
    return {"ok": True, "job_id": jid}


@app.get("/api/jobs")
def list_jobs(limit: int = 25):
    return {"jobs": jobs.all_jobs(limit)}


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    j = jobs.get(job_id)
    if not j:
        raise HTTPException(404, "no such job")
    return j


@app.get("/api/models")
def models():
    """Larger Ollama Cloud models that actually emit text (verified live)."""
    return {"models": [
        "mistral-large-3:675b-cloud",
        "gpt-oss:120b-cloud",
        "deepseek-v4-pro:cloud",
        "glm-5.3:cloud",
        "minimax-m3:cloud",
        "kimi-k3:cloud",
    ]}


@app.get("/api/programs")
def find_programs(q: str = "AI", limit: int = 12):
    """Live affiliate program data (openaffiliate.dev), ranked for a zero-budget beginner."""
    try:
        rows = programs.ranked(q, limit=min(limit, 30))
        return {"query": q, "count": len(rows), "programs": rows}
    except Exception as e:
        raise HTTPException(502, f"program lookup failed: {e}")


@app.get("/", response_class=HTMLResponse)
def index():
    return (Path(__file__).resolve().parent / "static" / "index.html").read_text()
