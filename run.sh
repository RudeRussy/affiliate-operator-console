#!/usr/bin/env bash
# Affiliate stack control script.
#   ./run.sh console      start the Operator Console (macOS UI) — the main app
#   ./run.sh tracker      start the click tracker (separate service the links point at)
#   ./run.sh seed         create a demo offer + campaign, print the test link
#   ./run.sh report       print the click->conversion leaderboard
#   ./run.sh pack ...     generate a content pack (engine/content_engine.py --help)
#   ./run.sh kit ...      generate a service kit (engine/service_kit.py --help)
#   ./run.sh programs Q   live affiliate program lookup (openaffiliate.dev)
#   ./run.sh niches S     research trending+profitable niches (real signals)
#   ./run.sh render PACK  render a content pack's images via ComfyUI
#   ./run.sh bridge PACK  build a tracked bridge page for a pack
#   ./run.sh publish PACK cut platform-ready publish bundles
#   ./run.sh catalog      affiliate program catalogue (cache/search/stats)
#   ./run.sh cx --deep Q  Complexity: multi-pass cited research (local :8000)
#   ./run.sh cx --council Q  Complexity: multi-model debate + judge
#   ./run.sh cx --niche N    Complexity: validate a niche with cited evidence
#   ./run.sh leads ...    outreach: leads, drafts, follow-ups (the money side)
#       ./run.sh leads --add-lead "Jane,Copper Lane,etsy,https://etsy.com/shop/x"
#       ./run.sh leads --list
#       ./run.sh leads --draft 3 --channel email
#       ./run.sh leads --due
#       ./run.sh leads --stats
#   ./run.sh audit DIR    audit generated copy for risky/fabricated claims
#   ./run.sh fix DIR      apply the audit's safe rewrites in place (backup made)
#   ./run.sh harden DIR   audit THEN fix (the safe pipeline)
#   ./run.sh test         run all tests
set -euo pipefail
cd "$(dirname "$0")"
PY=".venv/bin/python"
PORT="${TRACKER_PORT:-8788}"
CPORT="${CONSOLE_PORT:-8790}"
export TRACKER_BASE_URL="${TRACKER_BASE_URL:-http://127.0.0.1:$PORT}"

case "${1:-}" in
  console)
    echo "Operator Console -> http://127.0.0.1:$CPORT"
    exec $PY -m uvicorn console.app:app --host 127.0.0.1 --port "$CPORT"
    ;;
  tracker)
    echo "Tracker on $TRACKER_BASE_URL  (Ctrl-C to stop)"
    exec $PY -m uvicorn tracker.app:app --host 127.0.0.1 --port "$PORT"
    ;;
  seed)   exec $PY -m tracker.cli seed ;;
  init)   exec $PY -m tracker.cli init ;;
  report) exec $PY -m tracker.cli report ;;
  pack)   shift; exec $PY -m engine.content_engine "$@" ;;
  kit)    shift; exec $PY -m engine.service_kit "$@" ;;
  niches) shift; exec $PY -m engine.niche_research "$@" ;;
  render) shift; exec $PY -m engine.render_engine "$@" ;;
  bridge) shift; exec $PY -m engine.bridge_page "$@" ;;
  publish) shift; exec $PY -m engine.publish "$@" ;;
  catalog) shift; exec $PY -m console.catalog "$@" ;;
  cx)     shift; exec $PY -m engine.complexity "$@" ;;
  leads)  shift; exec $PY -m engine.outreach "$@" ;;
  find)   shift; exec $PY -m engine.leadfinder "$@" ;;
  amazon) shift; exec $PY -m engine.amazon "$@" ;;
  verify-leads) shift; exec $PY -m engine.leadverify "$@" ;;
  audit)  shift; exec $PY -m scripts.claims_audit "$@" ;;
  fix)    shift; exec $PY -m scripts.claims_fix "$@" ;;
  harden)
    shift
    D="$1"
    $PY -m scripts.claims_audit --dir "$D" || true
    echo "--- applying safe rewrites ---"
    $PY -m scripts.claims_fix --dir "$D"
    exit 0
    ;;
  programs)
    shift; exec $PY -c "
import sys
from console.programs import ranked
q = sys.argv[1] if len(sys.argv) > 1 else 'AI'
for p in ranked(q, 12):
    print(f\"{p['score']:5.1f}  {p['name']:20s} {str(p['commission_rate'] or ''):6s} {str(p['commission_type'] or ''):10s} cookie {p['cookie_days']}d  via {p['network']}\")
" "$@" ;;
  test)
    bash test_tracker.sh
    bash test_console.sh
    bash test_console2.sh
    bash test_profile.sh
    $PY -m tests.test_hardening
    $PY -m tests.test_console_hardening
    $PY -m tests.test_build_pipeline
    $PY -m tests.test_outreach
    $PY -m tests.test_leadfinder
    $PY -m tests.test_amazon
    $PY -m tests.test_leadverify
    ;;
  *)
    echo "usage: $0 {console|tracker|seed|init|report|pack|kit|programs|audit|fix|harden|test}"
    exit 1
    ;;
esac
