"""
Content engine: turn a topic + offer into a publish-ready content pack using a
LOCAL or cloud LLM (Ollama-compatible). Zero API cost when pointed at local Ollama.

Produces, per pack (written to content_out/<slug>/):
  brief.json      structured: title, hook, script, description, hashtags, scenes, image_prompts
  script.md       human-readable script
  description.txt YouTube/TikTok description WITH compliant affiliate disclosure
  images/         (optional) SDXL renders queued to ComfyUI, one per image prompt
  meta.json       provenance: model, topic, offer, ts, disclosure text used

Usage:
  python -m engine.content_engine --topic "Run SDXL locally on a Mac" \
      --offer "DiffusionBee" --aff-url "https://..." --provider ollama-local \
      --model qwen3.5:cloud --out content_out
"""
import argparse
import json
import os
import re
import time
import urllib.error
import urllib.request
from pathlib import Path

OLLAMA_LOCAL = "http://127.0.0.1:11434"
COMPLEXITY = "http://127.0.0.1:8000"

# Compliance disclosure text (UK ASA/CMA + FTC-safe). From the legal research:
# must be clear, prominent, BEFORE the endorsement, and not buried.
DISCLOSURE_LONG = (
    "Disclosure: this video contains affiliate links. If you buy through them I may "
    "earn a commission at no extra cost to you. I only recommend tools I have actually "
    "used. As an affiliate I earn from qualifying purchases."
)
DISCLOSURE_SHORT = "#ad #affiliate"

SYSTEM = """You are a senior performance-marketing copywriter who writes FACELESS YouTube
content for a solo operator in the AI-tools niche. You write for SEARCH INTENT (people
looking to solve a specific problem), not for hype. You never exaggerate, never promise
income, and never fabricate benchmarks or first-hand experience that was not supplied.

Return ONLY a single JSON object with EXACTLY these keys:
{
  "title": "<=70 chars, search-intent, specific",
  "hook": "1-2 sentences, the first 5 seconds, no 'hey guys'",
  "script": "full voiceover script, 300-600 words, natural spoken English, sections labelled [HOOK] [INTRO] [BODY] [CTA]",
  "scenes": [{"n": 1, "voiceover": "...", "visual": "what is on screen"}],
  "description": "YouTube description, 150-250 words, ends with a natural soft CTA",
  "hashtags": ["#tag1", "#tag2"],
  "image_prompts": ["3-6 SDXL prompts for thumbnails/b-roll, each a comma-separated prompt"],
  "thumbnail_text": "<=4 words overlay",
  "target_keyword": "the search query this video targets"
}
No markdown fences. No commentary. Valid JSON only."""


def _post_json(url, payload, timeout=300, attempts=3):
    """POST JSON with retry + exponential backoff on transient (connection/5xx) errors."""
    last = None
    for i in range(attempts):
        try:
            data = json.dumps(payload).encode()
            req = urllib.request.Request(url, data=data,
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            last = e
            if e.code < 500 and e.code != 429:      # 4xx (except 429) won't fix itself
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last = e
        if i < attempts - 1:
            time.sleep(2 ** i)                      # 1s, 2s
    raise last if last else RuntimeError("request failed")


def chat(provider, model, system, user, timeout=300, num_predict=12000):
    """Ollama-compatible chat. provider: ollama-local | ollama-cloud (same host)."""
    base = OLLAMA_LOCAL
    payload = {
        "model": model,
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "options": {"temperature": 0.7, "num_predict": num_predict},
    }
    out = _post_json(f"{base}/api/chat", payload, timeout=timeout)
    if out.get("done_reason") == "length":
        print("WARNING: model output hit the token cap; JSON may be truncated")
    return out.get("message", {}).get("content", "")


def _repair_truncated_json(text):
    """Close an unterminated JSON object/array so a length-truncated reply still parses.
    Drops any trailing incomplete key/value first."""
    # strip a dangling partial token after the last complete value
    text = re.sub(r',\s*"[^"]*$', "", text)
    text = re.sub(r',\s*$', "", text)
    stack = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch in "{[":
                stack.append(ch)
            elif ch in "}]":
                if stack:
                    stack.pop()
    if in_str:
        text += '"'
    for opener in reversed(stack):
        text += "}" if opener == "{" else "]"
    return text


def extract_json(text):
    """LLMs sometimes wrap JSON; grab the outermost object. Repairs if truncated."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    start, depth = None, 0
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                return json.loads(text[start:i + 1])
    # no complete object found -> try repairing the tail
    if start is not None:
        try:
            return json.loads(_repair_truncated_json(text[start:]))
        except Exception:
            pass
    raise ValueError("no JSON object found in model output")


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def build_user_prompt(topic, offer, aff_url, extra_context=""):
    return f"""Create one faceless YouTube video content pack.

TOPIC / search intent: {topic}
AFFILIATE OFFER I am promoting: {offer}
AFFILIATE LINK (goes in description, after the disclosure): {aff_url}
{('EXTRA CONTEXT: ' + extra_context) if extra_context else ''}

Rules:
- The video must genuinely help the viewer solve the problem FIRST; the offer is the natural tool.
- Never claim to have done something that is not evidenced. Speak from workflow, not invented results.
- The description MUST START with this exact disclosure line: "{DISCLOSURE_LONG}"
- Target UK + US English audience. No income promises. No medical/financial claims.
"""


def generate_pack(topic, offer, aff_url, provider, model, out_root="content_out",
                  extra_context="", timeout=300):
    out_dir = Path(out_root) / slugify(topic)
    out_dir.mkdir(parents=True, exist_ok=True)

    raw = chat(provider, model, SYSTEM,
               build_user_prompt(topic, offer, aff_url, extra_context), timeout)
    brief = extract_json(raw)

    # Force compliant description: disclosure first, exactly once.
    body = brief.get("description", "").strip()
    if not body.startswith(DISCLOSURE_LONG[:30]):
        body = f"{DISCLOSURE_LONG}\n\n{body}"
    if DISCLOSURE_SHORT not in body:
        body = f"{body}\n\n{DISCLOSURE_SHORT}"
    brief["description"] = body

    (out_dir / "brief.json").write_text(json.dumps(brief, indent=2))
    (out_dir / "description.txt").write_text(brief["description"])
    (out_dir / "script.md").write_text(
        f"# {brief.get('title','')}\n\n**Hook:** {brief.get('hook','')}\n\n"
        f"**Target keyword:** {brief.get('target_keyword','')}\n\n"
        f"---\n\n{brief.get('script','')}\n"
    )
    (out_dir / "meta.json").write_text(json.dumps({
        "topic": topic, "offer": offer, "aff_url": aff_url,
        "provider": provider, "model": model, "ts": int(time.time()),
        "disclosure": DISCLOSURE_LONG, "synthetic": True,
    }, indent=2))
    return out_dir, brief


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", required=True)
    ap.add_argument("--offer", required=True)
    ap.add_argument("--aff-url", required=True)
    ap.add_argument("--provider", default="ollama-local")
    ap.add_argument("--model", default="qwen3.5:cloud")
    ap.add_argument("--out", default="content_out")
    ap.add_argument("--context", default="")
    ap.add_argument("--timeout", type=int, default=300)
    a = ap.parse_args()

    d, brief = generate_pack(a.topic, a.offer, a.aff_url, a.provider, a.model,
                             a.out, a.context, a.timeout)
    print(f"PACK: {d}")
    print(f"TITLE: {brief.get('title')}")
    print(f"KEYWORD: {brief.get('target_keyword')}")
    print(f"SCENES: {len(brief.get('scenes', []))}  IMAGES: {len(brief.get('image_prompts', []))}")
    print(f"DESCRIPTION starts: {brief['description'][:80]}...")


if __name__ == "__main__":
    main()
