"""
Track B: Service Kit generator.

Turns your AI-generation skills into a sellable service. Generates the complete
client-facing + operator-facing pack, one LLM call per section (reliable, high quality).

Output: service_kit/<slug>/*.md  (+ index.md)

Usage:
  python -m engine.service_kit \
    --service "AI product photos for Etsy and Shopify sellers" \
    --niche "handmade jewellery and homeware sellers" \
    --price-range "£75-£400" \
    --model mistral-large-3:675b-cloud
"""
import argparse
import json
import time
from pathlib import Path

from .content_engine import chat, slugify

SECTIONS = [
    ("00-offer", "The Offer", """Write the core offer definition. Include:
- One-line positioning statement
- Exactly WHO the ideal client is (be specific: platform, size, what they sell)
- The painful problem they have RIGHT NOW that this solves
- The concrete deliverable they receive
- The transformation / outcome
- Why you specifically (local AI pipeline, speed, volume, cost) vs a traditional photographer
- What you explicitly do NOT do (scope boundaries)
Keep it under 400 words. Plain, concrete language. No hype, no 'revolutionary'."""),

    ("01-packages", "Packages & Pricing", """Design exactly THREE service tiers. For each give:
- Name, price (use the given price range; make the middle tier the obvious best value)
- Exact deliverables (how many images/videos/revisions)
- Turnaround time
- Who it's for
Then add: what to charge for extras (rush, extra images, usage rights), how to take payment
(upfront 50% or full), and a payment/refund policy that protects you.
Format as clean markdown tables. Be specific with numbers."""),

    ("02-outreach-dm", "Cold DM Scripts", """Write THREE cold DM scripts for reaching sellers directly on Instagram, Facebook groups,
and Etsy. Rules:
- Lead with a specific observation about THEIR shop/product (show you looked)
- Offer something free/valuable first (a free sample image of one of their products)
- No desperation, no long paragraphs, no 'I came across your profile'
- Under 60 words each
- End with a low-friction question, not a hard pitch
Label each: Instagram DM / Facebook Group / Etsy/Shopify contact."""),

    ("03-outreach-email", "Cold Email Scripts", """Write TWO cold emails:
1. A first-touch email attached to a free sample image of their product
2. A one-line follow-up for 4 days later if no reply
Include subject lines (2 options each). Rules: specific, short (under 120 words), one
clear ask, no walls of text, no 'hope this finds you well'."""),

    ("04-proposal-template", "Proposal Template", """Write a short, scannable client proposal template with placeholders in [SQUARE BRACKETS].
Sections: what they get, exact deliverables, timeline, price + payment terms, revision
policy, what you need from them, next step. Keep it under 350 words. It must be
copy-paste ready and look professional without being corporate."""),

    ("05-delivery-checklist", "Delivery Checklist", """Write the operational checklist for delivering a paid job:
- What to collect from the client before starting
- What to check before sending (quality, aspect ratios per platform, file formats, naming)
- How to package and send (file transfer, folder structure, a short handover note)
- How to ask for a testimonial + referral
- What to log for tax/accounting
Make it a checkable markdown list."""),

    ("06-where-to-find-clients", "Where To Find Clients", """List concrete, specific places to find paying clients in this niche, grouped by
effort/likelihood. Include named Facebook groups, subreddits, Discord servers, marketplaces
(Fiverr/Upwork), and local/offline options. For each, note the right approach and any
rules to respect (e.g. no link-dropping, value-first). Flag anything that bans self-promotion."""),

    ("07-portfolio-plan", "Portfolio Plan", """Design a 5-piece portfolio plan to produce BEFORE selling, using a local AI image/video
pipeline. For each piece specify: what it demonstrates, what to generate, and the before/
after framing. Also state how to present it (where it lives, how a prospect sees it) and
the honesty rule: label AI-generated work as AI-generated, never claim it is a real
photoshoot of a real customer's product."""),
]

SYSTEM = """You are a straight-talking small-business consultant who helps solo operators
package and sell a service. You write concrete, specific, usable copy — never generic
filler. You favour plain English, specific numbers, and realistic expectations. You never
promise guaranteed income or use hype words like revolutionary, game-changing, or 10x.
You always respect affiliate/advertising and platform disclosure rules (label AI-generated
media honestly). Return markdown only."""


def build_user(section_title, instruction, service, niche, price_range):
    return f"""SERVICE: {service}
TARGET NICHE: {niche}
PRICE RANGE: {price_range}
SECTION: {section_title}

{instruction}"""


def generate(service, niche, price_range, provider, model, out_root="service_kit",
             num_predict=3500):
    out_dir = Path(out_root) / slugify(service)
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []

    for slug, title, instruction in SECTIONS:
        user = build_user(title, instruction, service, niche, price_range)
        content = chat(provider, model, SYSTEM, user, num_predict=num_predict)
        body = f"# {title}\n\n{content.strip()}\n"
        (out_dir / f"{slug}.md").write_text(body)
        written.append((slug, title, len(content)))
        print(f"  wrote {slug}.md ({len(content)} chars)")

    # index
    lines = [f"# Service Kit — {service}\n",
             f"**Target niche:** {niche}  \n**Price range:** {price_range}  \n"
             f"**Generated:** {time.strftime('%Y-%m-%d %H:%M')} with `{model}`\n",
             "## Contents\n"]
    for slug, title, n in written:
        lines.append(f"- [{title}](./{slug}.md)")
    (out_dir / "index.md").write_text("\n".join(lines) + "\n")

    (out_dir / "meta.json").write_text(json.dumps({
        "service": service, "niche": niche, "price_range": price_range,
        "provider": provider, "model": model, "ts": int(time.time()),
        "sections": [s for s, _, _ in written],
    }, indent=2))

    return out_dir


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--service", required=True)
    ap.add_argument("--niche", default="small e-commerce sellers")
    ap.add_argument("--price-range", default="£75-£400")
    ap.add_argument("--provider", default="ollama-local")
    ap.add_argument("--model", default="mistral-large-3:675b-cloud")
    ap.add_argument("--out", default="service_kit")
    ap.add_argument("--num-predict", type=int, default=3500)
    a = ap.parse_args()

    print(f"Generating service kit with {a.model} ...")
    d = generate(a.service, a.niche, a.price_range, a.provider, a.model,
                 a.out, a.num_predict)
    print(f"\nKIT: {d}")
    print(f"FILES: {len(list(d.glob('*.md')))}")


if __name__ == "__main__":
    main()
