"""
Independent code review of the affiliate stack using a LARGE Ollama Cloud model.

Sends the source tree to a big model and asks for a brutal, severity-rated review
focused on: correctness bugs, security issues, data loss, and missing edge cases.

Usage:
  python -m scripts.code_review --model kimi-k3:cloud --paths tracker engine
"""
import argparse
import json
import urllib.request
from pathlib import Path

OLLAMA = "http://127.0.0.1:11434"

SYSTEM = """You are a hostile senior code reviewer. Your job is to find REAL bugs, not to
be nice. Review the code for: (1) correctness bugs that would cause wrong behaviour,
(2) security issues (injection, unvalidated input, SSRF, path traversal), (3) data loss or
corruption risks, (4) missing edge cases and error handling, (5) concurrency/race issues.
For each finding give: severity (CRITICAL/HIGH/MEDIUM/LOW), the file and function, exactly
what is wrong, and a concrete fix. Do NOT invent problems that aren't there; do NOT pad
with style nitpicks. If a file is fine, say so briefly. Be specific and cite the code.
End with a one-line honest verdict on whether this is production-ready."""


def gather(paths):
    out = []
    for p in paths:
        root = Path(p)
        files = sorted(root.rglob("*.py")) if root.is_dir() else [root]
        for f in files:
            if "__pycache__" in str(f):
                continue
            out.append(f"### FILE: {f}\n```python\n{f.read_text()}\n```")
    return "\n\n".join(out)


def review(code, model, num_predict=6000, timeout=600):
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"Review this codebase:\n\n{code}"}],
        "stream": False,
        "options": {"temperature": 0.2, "num_predict": num_predict},
    }
    req = urllib.request.Request(f"{OLLAMA}/api/chat",
                                 data=json.dumps(payload).encode(),
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.loads(r.read().decode())
    return d.get("message", {}).get("content", ""), d.get("done_reason")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="kimi-k3:cloud")
    ap.add_argument("--paths", nargs="+", default=["tracker", "engine"])
    ap.add_argument("--out", default="code_review.md")
    ap.add_argument("--num-predict", type=int, default=6000)
    a = ap.parse_args()

    code = gather(a.paths)
    print(f"Reviewing {len(code)} chars of code with {a.model} ...")
    text, reason = review(code, a.model, a.num_predict)

    # Guard: never clobber existing output with an empty response. Thinking models
    # (e.g. kimi-k3) can burn the whole token budget on reasoning and emit nothing;
    # reason='length' with 0 chars means exactly that.
    if not text.strip():
        print(f"REFUSED TO WRITE: model returned {len(text)} chars (done_reason={reason}).")
        print("Try a model that emits text (mistral-large-3:675b-cloud) or raise --num-predict.")
        sys.exit(2)

    Path(a.out).write_text(f"# Code Review ({a.model})\n\n{text}\n")
    print(f"done_reason={reason}  chars={len(text)}  -> {a.out}")


if __name__ == "__main__":
    main()
