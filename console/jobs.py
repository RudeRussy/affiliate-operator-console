"""
Background job runner for the console.

Runs the engines (content pack, service kit, audits) as subprocesses and tracks
status/output so the UI can show live progress. Jobs are recorded in the console DB.
"""
import os
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = os.environ.get("CONSOLE_DB", str(Path(__file__).resolve().parent / "console.db"))
PY = str(ROOT / ".venv" / "bin" / "python")

_jobs: dict = {}
_lock = threading.Lock()


def _db():
    c = sqlite3.connect(DB, timeout=10)
    c.row_factory = sqlite3.Row
    return c


def init_jobs_table():
    c = _db()
    c.execute("""CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, kind TEXT, args TEXT, status TEXT,
        started_ts INTEGER, ended_ts INTEGER, output TEXT, exit_code INTEGER)""")
    c.commit()
    c.close()


def _record(job):
    c = _db()
    c.execute("""INSERT INTO jobs(id,kind,args,status,started_ts,ended_ts,output,exit_code)
                 VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(id) DO UPDATE SET status=excluded.status,
                   ended_ts=excluded.ended_ts, output=excluded.output,
                   exit_code=excluded.exit_code""",
              (job["id"], job["kind"], job.get("args", ""), job["status"],
               job.get("started_ts"), job.get("ended_ts"),
               job.get("output", ""), job.get("exit_code")))
    c.commit()
    c.close()


def _run(job_id, cmd, cwd):
    try:
        p = subprocess.run(cmd, cwd=str(cwd), capture_output=True, text=True, timeout=1800)
        out = (p.stdout or "") + (p.stderr or "")
        with _lock:
            j = _jobs[job_id]
            j["status"] = "done" if p.returncode == 0 else "failed"
            j["output"] = out[-20000:]
            j["exit_code"] = p.returncode
            j["ended_ts"] = int(time.time())
            _record(j)
    except Exception as e:
        with _lock:
            j = _jobs[job_id]
            j["status"] = "failed"
            j["output"] = f"error: {e}"
            j["ended_ts"] = int(time.time())
            _record(j)


def start(kind, cmd, args=""):
    init_jobs_table()
    job_id = uuid.uuid4().hex[:12]
    job = {"id": job_id, "kind": kind, "args": args, "status": "running",
           "started_ts": int(time.time()), "output": "", "exit_code": None}
    with _lock:
        _jobs[job_id] = job
    _record(job)
    threading.Thread(target=_run, args=(job_id, cmd, ROOT), daemon=True).start()
    return job_id


def get(job_id):
    with _lock:
        j = _jobs.get(job_id)
    if j:
        return j
    c = _db()
    r = c.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
    c.close()
    return dict(r) if r else None


def all_jobs(limit=25):
    c = _db()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM jobs ORDER BY started_ts DESC LIMIT ?", (limit,))]
    c.close()
    # overlay live in-memory state
    with _lock:
        for r in rows:
            if r["id"] in _jobs:
                r.update({k: _jobs[r["id"]].get(k, r.get(k)) for k in
                          ("status", "output", "exit_code", "ended_ts")})
    return rows


# ---------- engine wrappers ----------
def run_content_pack(topic, offer, aff_url, model="mistral-large-3:675b-cloud"):
    cmd = [PY, "-m", "engine.content_engine", "--topic", topic, "--offer", offer,
           "--aff-url", aff_url, "--model", model]
    return start("content", cmd, f"{topic} | {offer}")


def run_service_kit(service, niche, price_range, model="mistral-large-3:675b-cloud"):
    cmd = [PY, "-m", "engine.service_kit", "--service", service, "--niche", niche,
           "--price-range", price_range, "--model", model]
    return start("service_kit", cmd, service)


def run_claims_audit(target_dir, model="gpt-oss:120b-cloud"):
    cmd = [PY, "-m", "scripts.claims_audit", "--dir", target_dir, "--model", model]
    return start("audit", cmd, target_dir)


def run_claims_fix(target_dir):
    """Apply an audit's safe rewrites in place (backs the kit up first)."""
    cmd = [PY, "-m", "scripts.claims_fix", "--dir", target_dir]
    return start("claims_fix", cmd, target_dir)


def run_code_review(paths=("tracker", "engine", "console", "scripts"),
                    model="mistral-large-3:675b-cloud"):
    cmd = [PY, "-m", "scripts.code_review", "--paths", *paths, "--model", model]
    return start("review", cmd, ",".join(paths))


def run_niche_research(seeds, geo="GB", model=None, provider="ollama-cloud"):
    """Find trending niches. Optionally synthesise with an LLM (slow, run as a job)."""
    cmd = [PY, "-m", "engine.niche_research", "--seeds", seeds, "--geo", geo,
           "--out", "niches_live.json"]
    if model:
        cmd += ["--llm", model, "--provider", provider]
    return start("niche", cmd, seeds)


def run_render(pack_dir, steps=28, limit=None, aspect="16:9"):
    """Render a content pack's image prompts via ComfyUI (one at a time)."""
    cmd = [PY, "-m", "engine.render_engine", "--pack", pack_dir,
           "--steps", str(steps), "--aspect", aspect]
    if limit:
        cmd += ["--limit", str(limit)]
    return start("render", cmd, pack_dir)


def run_bridge_page(pack_dir, offer_url, offer_name, tracker="http://127.0.0.1:8788"):
    cmd = [PY, "-m", "engine.bridge_page", "--pack", pack_dir,
           "--offer-url", offer_url, "--offer-name", offer_name, "--tracker", tracker]
    return start("bridge", cmd, pack_dir)


def run_publish(pack_dir, offer_name, platforms="youtube,tiktok,instagram,pinterest,blog",
                tracker="http://127.0.0.1:8788"):
    cmd = [PY, "-m", "engine.publish", "--pack", pack_dir,
           "--offer-name", offer_name, "--platforms", platforms, "--tracker", tracker]
    return start("publish", cmd, pack_dir)


def run_outreach_draft(lead_id, channel=None, kind="first"):
    cmd = [PY, "-m", "engine.outreach", "--draft", str(lead_id), "--kind", kind]
    if channel:
        cmd += ["--channel", channel]
    return start("outreach_draft", cmd, f"lead{lead_id}")


# ---------- listings ----------
def list_content_packs():
    out = []
    base = ROOT / "content_out"
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir():
                brief = d / "brief.json"
                title = d.name
                if brief.exists():
                    try:
                        import json
                        title = json.loads(brief.read_text()).get("title", d.name)
                    except Exception:
                        pass
                out.append({"dir": d.name, "title": title,
                            "files": sorted(f.name for f in d.iterdir())})
    return out


def list_service_kits():
    out = []
    base = ROOT / "service_kit"
    if base.exists():
        for d in sorted(base.iterdir()):
            if d.is_dir():
                out.append({"dir": d.name,
                            "files": sorted(f.name for f in d.iterdir() if f.suffix == ".md")})
    return out


TEXT_SUFFIXES = {".md", ".txt", ".json", ".csv"}
MAX_READ_BYTES = 2_000_000  # 2 MB cap — never slurp a multi-GB file into memory


def read_file_in(base, sub, filename):
    """Safely read a TEXT file inside base/sub.

    Containment uses Path.relative_to (NOT str.startswith — a sibling dir named
    'service_kit_evil' would pass a prefix check). Rejects binary/oversized files.
    """
    root = (ROOT / base).resolve()
    try:
        p = (ROOT / base / sub / filename).resolve()
    except Exception:
        return None
    # real containment check
    try:
        p.relative_to(root)
    except ValueError:
        return None
    if not p.exists() or not p.is_file():
        return None
    if p.suffix.lower() not in TEXT_SUFFIXES:
        return None
    try:
        if p.stat().st_size > MAX_READ_BYTES:
            return None
    except OSError:
        return None
    try:
        return p.read_text()
    except (UnicodeDecodeError, OSError):
        return None
