# Affiliate Operator Console

A local-first toolkit for running an affiliate + service business solo: find
prospects, generate content, publish it, track clicks, and sell AI product
photography to small online sellers.

Everything runs on your own machine. No accounts, no SaaS, no cloud required for
the core.

## Quick start

```bash
git clone <this-repo>
cd affiliate_stack
./start.sh
```

Then open **http://127.0.0.1:8790**.

`start.sh` creates a virtualenv, installs dependencies, and runs both services:

| Service | URL | Purpose |
|---|---|---|
| **Console** | http://127.0.0.1:8790 | The app — 11 views |
| Tracker | http://127.0.0.1:8788 | Click attribution & postbacks |

Other entry points:

```bash
./start.sh test       # run the test suite
./start.sh console    # console only
./start.sh tracker    # tracker only
```

Requirements: Python 3.11+. No Docker, no Node, no build step.

## The layout adapts

The UI detects viewport size and changes shape:

- **Desktop (≥900px)** — fixed left sidebar, centred content
- **Tablet (600–899px)** — narrower sidebar
- **Mobile (<600px)** — sidebar becomes a bottom tab bar, thumb-reachable,
  with iOS safe-area insets handled

It is a single self-contained HTML file. No CDN, no external fonts, no framework.

## What each part does

### Find leads
Discovers small-seller prospects from **public sources only** — no logins, no
paywall bypass, no scraping behind a wall. Each lead records *why* it looks
promising, so you can overrule the ranking.

It also auto-detects UK limited companies from shop names and text.

### Verify leads
A name from a search snippet is not enough to act on. The verifier searches for
each shop by name and classifies it:

| Status | Meaning |
|---|---|
| `confirmed` | the shop's own page came back, with description and location |
| `weak` | a passing mention only — not promoted to confirmed |
| `unverified` | nothing found — do not contact |
| `non_uk` | confirmed, but located outside the UK |
| `error` | lookup failed — retry; never recorded as unverified |

### Outreach
Generates messages and tracks what happened. **Nothing auto-sends** — cold
outreach is written, then sent by hand.

**UK PECR compliance is enforced in code, and the rule is counter-intuitive:**

| Lead type | Cold email | Why |
|---|---|---|
| Corporate subscriber (limited company, LLP) | Lawful, if you identify yourself and give a valid address | PECR exempts corporate subscribers from the consent requirement |
| Individual subscriber (sole trader, ordinary partnership) | **Blocked** | Prior consent or a soft opt-in required — you have neither |

Also enforced: one "no" suppresses permanently; per-channel daily caps; warm-up
gates before pitching in communities; follow-ups on day 3/7/14 then stop; never
chase someone who replied.

### Claim safety
An audit + auto-fix pass over AI-generated marketing copy. It catches fabricated
claims — e.g. an LLM writing "I use UK-based servers" for software that runs on a
laptop. Run it before anything goes public.

### Content, render, publish
Content packs → images (via a local ComfyUI install, optional) → bridge pages
with tracked links → per-platform bundles. Publishing **refuses** to produce a
bundle without an affiliate disclosure.

### Amazon & ClickBank
Link building and honest limits. Amazon **bans affiliate links in cold email** —
the code refuses to supply one for that purpose. Amazon's sub-ID is `ascsubtag`;
ClickBank's is `tid`. Getting that wrong loses every conversion silently.

## Tests

```bash
./start.sh test
# or
pytest tests/ -q
```

Test suites cover the compliance gates, lead scoring and verification,
link building, publishing limits, and tracker attribution.

> A note on the tests: an early version of this suite could pass while
> assertions failed, and another version depended on execution order. Both are
> fixed — the harness raises on failure and isolates state per test.

## Configuration

Optional environment variables (copy `.env.example` to `.env`):

```bash
BRAVE_SEARCH_API_KEY=...   # only needed for lead discovery
```

Nothing else is required to run the app.

## Licence

MIT — see `LICENSE`.
