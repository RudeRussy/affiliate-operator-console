"""
Shared test harness for the affiliate_stack suite.

WHY THIS EXISTS — and it is not decoration:

The original per-file `check()` only *recorded* failures into a list and printed
them. pytest collected the test functions, ran them, saw no exception, and reported
"all passed" — **even when an assertion had failed**. That is a false green: a broken
build would have been signed off as verified. Proven by sabotaging one assertion and
watching pytest still say "12 passed".

So `check()` here RAISES on failure. Every runner — pytest, `run.sh test`, CI — now
sees the truth. `run_all()` keeps the script-mode nicety of showing *every* failure
rather than stopping at the first.

Rule: never add a test helper that swallows a failure. A test that cannot fail is
worse than no test, because it buys false confidence.
"""
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


class Failure(AssertionError):
    """Raised by check(). An AssertionError so pytest reports it natively."""


def check(name, cond, detail=""):
    """Assert. Prints PASS/FAIL and RAISES on failure so no runner can miss it."""
    if cond:
        print(f"PASS {name}")
        return
    msg = f"FAIL {name}" + (f"  {detail}" if detail else "")
    print(msg)
    raise Failure(msg)


def temp_db(env_var="OUTREACH_DB", name="t.db"):
    """Point an engine's DB at a throwaway file. Returns the temp dir."""
    d = tempfile.mkdtemp()
    os.environ[env_var] = str(Path(d) / name)
    return Path(d)


def isolate_db():
    """Give the outreach DB a FRESH empty file and re-init.

    Call before EVERY test. Without this, tests contaminate each other through
    module-level DB state: `test_lead_lifecycle` asserts the first lead gets id 1,
    which is only true on an empty table. Script mode ran the files in one order and
    passed; pytest ran them alphabetically and 5 tests failed on leftover rows.

    A test that only passes in one execution order is not a test.
    """
    d = tempfile.mkdtemp()
    path = str(Path(d) / "t.db")
    os.environ["OUTREACH_DB"] = path
    try:
        from engine import outreach as O
        O.DB_PATH = path
        O.init_db()
    except Exception:
        pass


def run_all(module_globals):
    """Run every test_* function in the calling module, in source order.

    Isolates the DB before each test, continues past a failure so script mode shows
    all of them, then exits non-zero. Call as: run_all(globals())
    """
    fns = [(k, v) for k, v in module_globals.items()
           if k.startswith("test_") and callable(v)
           and getattr(v, "__module__", "") == module_globals.get("__name__")]
    fns.sort(key=lambda kv: getattr(kv[1], "__code__", None)
             and kv[1].__code__.co_firstlineno or 0)

    failed = []
    for name, fn in fns:
        isolate_db()
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 — deliberate: keep going
            failed.append((name, exc))

    print()
    if failed:
        print(f"{len(failed)} FAILED:")
        for name, exc in failed:
            print(f"  - {name}: {exc}")
        sys.exit(1)
    print("ALL TESTS PASSED")
