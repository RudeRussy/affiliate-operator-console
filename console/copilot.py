"""
State-aware co-pilot for the operator console.

Given the operator's current state (profile, tasks, metrics, guardrails) and a question,
returns practical, next-action guidance. Uses a local/cloud Ollama model; falls back to a
deterministic rule-based answer if the model is unavailable.
"""
import json
import os
import urllib.request

OLLAMA = "http://127.0.0.1:11434"
COPILOT_MODEL = os.environ.get("COPILOT_MODEL", "mistral-large-3:675b-cloud")

SYSTEM = """You are the operator's hands-on coach for building a solo affiliate/service
business. The operator is a UK-based beginner losing their job, with zero budget, no
audience, and a 64GB Mac Studio running local AI (image/video generation, local LLMs).

You are given their CURRENT STATE (profile, done/todo tasks, metrics, active guardrails).
Rules:
- ALWAYS ground your answer in their actual state. Never give generic advice.
- Tell them the SINGLE next action, then the one after it. Be specific and concrete.
- Be honest about timelines: first commission 6-12 weeks, consistent income 6-12 months.
  Never promise quick money or guaranteed income.
- Warn them when a guardrail is firing.
- Keep it short and usable — under 250 words. Plain English, no jargon, no hype.
- If they ask what to do next, pick from their actual TODO tasks.
- UK context (HMRC, ICO, ASA, FCA). Never suggest promoting regulated financial products.
"""


def _state_brief(state):
    prof = state.get("profile", {})
    tasks = state.get("tasks", [])
    todo = [t for t in tasks if not t["done"]][:6]
    metrics = state.get("metrics", {})
    g = state.get("guardrails", [])
    lines = [
        f"PATH: {prof.get('path', 'not set')}",
        f"NICHE: {prof.get('niche', 'not set')}",
        f"PROGRESS: {state.get('progress', {}).get('done', 0)}/{state.get('progress', {}).get('total', 0)} tasks done",
        "NEXT TASKS:",
    ]
    for t in todo:
        lines.append(f"  - [{t['phase']}] {t['title']}")
    lines.append(f"METRICS: {json.dumps(metrics)}")
    if g:
        lines.append("ACTIVE GUARDRAILS:")
        for w in g:
            lines.append(f"  - [{w['level']}] {w['message']} -> {w['fix']}")
    return "\n".join(lines)


def _fallback(state):
    tasks = [t for t in state.get("tasks", []) if not t["done"]]
    g = state.get("guardrails", [])
    out = []
    if g:
        w = g[0]
        out.append(f"⚠️ {w['message']}\n→ {w['fix']}")
    if tasks:
        t = tasks[0]
        out.append(f"**Next action:** {t['title']}\n\n{t['detail']}")
        if len(tasks) > 1:
            out.append(f"**Then:** {tasks[1]['title']}")
    else:
        out.append("All current tasks are done. Review your metrics and scale the best-performing creative.")
    out.append("\n_Reminder: results lag effort by 3-6 months. Judge your cadence, not weekly income._")
    return "\n\n".join(out)


def copilot_reply(question, state, timeout=180):
    payload = {
        "model": COPILOT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content": f"CURRENT STATE:\n{_state_brief(state)}\n\n"
                                        f"OPERATOR QUESTION: {question}"},
        ],
        "stream": False,
        "options": {"temperature": 0.5, "num_predict": 2000},
    }
    try:
        req = urllib.request.Request(f"{OLLAMA}/api/chat",
                                     data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode())
        txt = d.get("message", {}).get("content", "").strip()
        return txt if txt else _fallback(state)
    except Exception:
        return _fallback(state)
