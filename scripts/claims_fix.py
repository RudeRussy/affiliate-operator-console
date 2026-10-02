"""
Claims FIXER: apply an audit's safe rewrites automatically.

The claims auditor only DETECTS risky claims — it leaves them in the copy. This reads
the audit table (QUOTE -> FIX) plus the kit files and rewrites the offending phrases
in place, backing up first.

It deliberately only applies the *safe rewrite* column, and refuses to write if it
cannot find a match, so it never silently mangles copy.

Usage:
  python -m scripts.claims_fix --dir service_kit/<kit> --audit service_kit/<kit>/CLAIMS_AUDIT.md
  python -m scripts.claims_fix --dir ... --dry-run
"""
import argparse
import re
import shutil
import sys
import time
from pathlib import Path

# Rows in the audit markdown table: | # | QUOTE | TYPE | WHY | FIX |
ROW = re.compile(r"^\|\s*\d+\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|\s*$")
OK_TYPES = ("OK-NO-ISSUE", "OK", "—", "-", "")
CURLY = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'", "—": "-", "–": "-"})


def _clean_quote(q: str) -> str:
    """Strip markdown emphasis AND surrounding quote marks, normalise curly punctuation.
    The audit table wraps quotes in "..." so we must remove those or nothing matches."""
    q = q.replace("**", "").replace("*", "").strip()
    q = q.translate(CURLY)
    # strip one layer of surrounding quotes
    if len(q) >= 2 and q[0] in '"\'' and q[-1] in '"\'':
        q = q[1:-1].strip()
    return q


def parse_audit(text: str):
    """Return list of (quote, type, fix) for rows that actually need fixing."""
    out = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        quote, typ, _why, fix = m.groups()
        t = typ.strip().strip("*").upper()
        if any(t.startswith(o) for o in OK_TYPES if o):
            continue
        fix = fix.strip()
        if not fix or fix in ("—", "-"):
            continue
        out.append((_clean_quote(quote), t, fix.replace("**", "").strip()))
    return out


def _fuzzy_contains(hay: str, needle: str) -> bool:
    if not needle:
        return False
    probe = needle[:45] if len(needle) >= 45 else needle
    return probe.lower() in hay.lower()


def apply_fixes(directory: Path, audit_file: Path, dry_run=False):
    items = parse_audit(audit_file.read_text())
    files = [f for f in sorted(directory.glob("*.md")) if f.name != audit_file.name]
    applied, missed = 0, []

    backup = None
    if not dry_run:
        backup = directory.parent / f"{directory.name}.bak-{time.strftime('%Y%m%d-%H%M%S')}"
        shutil.copytree(directory, backup)

    for quote, typ, fix in items:
        hit = False
        for f in files:
            text = f.read_text()
            if _fuzzy_contains(text, quote):
                # replace the whole quoted phrase if present, else the leading fragment
                if quote in text:
                    new = text.replace(quote, fix)
                else:
                    frag = quote[:40]
                    idx = text.lower().find(frag.lower())
                    # fall back: replace the line containing the fragment
                    lines = text.splitlines()
                    new_lines = []
                    for ln in lines:
                        if frag.lower() in ln.lower():
                            new_lines.append(fix)
                        else:
                            new_lines.append(ln)
                    new = "\n".join(new_lines) + ("\n" if text.endswith("\n") else "")
                    if idx < 0:
                        continue
                if new != text:
                    if not dry_run:
                        f.write_text(new)
                    print(f"  {'[dry] ' if dry_run else ''}fixed ({typ[:18]}) in {f.name}")
                    applied += 1
                    hit = True
                    break
        if not hit:
            missed.append((typ, quote[:70]))

    if backup:
        print(f"  backup: {backup}")
    return applied, missed


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--audit", default=None, help="defaults to <dir>/CLAIMS_AUDIT.md")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    d = Path(a.dir).resolve()
    if not d.is_dir():
        print(f"not a dir: {d}"); sys.exit(1)
    audit = Path(a.audit).resolve() if a.audit else d / "CLAIMS_AUDIT.md"
    if not audit.exists():
        print(f"no audit file: {audit}"); sys.exit(1)

    print(f"Applying safe rewrites from {audit.name} into {d.name} ...")
    applied, missed = apply_fixes(d, audit, a.dry_run)
    print(f"\napplied={applied}  unmatched={len(missed)}")
    for typ, q in missed:
        print(f"  UNMATCHED [{typ[:16]}] {q}")
    sys.exit(0 if applied else 2)


if __name__ == "__main__":
    main()
