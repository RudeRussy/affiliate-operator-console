"""Tests for the console review findings (containment, binaries, malformed API data).

Run: .venv/bin/python -m tests.test_console_hardening
"""
import sys


def main():
    from pathlib import Path
    from console import jobs, programs
    ok = 0

    # 1. Path containment: a sibling dir whose name EXTENDS the base must NOT be readable.
    evil = Path(jobs.__file__).resolve().parent.parent / "service_kit_evil"
    try:
        evil.mkdir(exist_ok=True)
        (evil / "x.md").write_text("SECRET")
    except Exception:
        pass
    got = jobs.read_file_in("service_kit", "../service_kit_evil", "x.md")
    assert got is None, f"CONTAINMENT BREACH: read {got!r}"
    print("PASS sibling-dir extension blocked"); ok += 1

    # 2. Plain traversal still blocked
    assert jobs.read_file_in("service_kit", "..", "../MASTERPLAN.md") is None
    print("PASS traversal blocked"); ok += 1

    # 3. Legit read works
    d = Path(jobs.__file__).resolve().parent.parent / "service_kit"
    sub = next((p.name for p in d.iterdir() if p.is_dir()), None)
    if sub:
        f = next((x.name for x in (d / sub).iterdir() if x.suffix == ".md"), None)
        if f:
            assert jobs.read_file_in("service_kit", sub, f) is not None
            print("PASS legit read works"); ok += 1

    # 4. Binary / wrong-suffix rejected
    png = d / sub / "probe.png"
    try:
        png.write_bytes(b"\x89PNG\r\n\x1a\n\x00\x00")
        assert jobs.read_file_in("service_kit", sub, "probe.png") is None
        print("PASS binary rejected"); ok += 1
    finally:
        try: png.unlink()
        except Exception: pass

    # 5. Malformed program data must NOT kill scoring (kimi finding)
    bad = [{"cookie_days": "60", "commission_rate": None, "payout_min": "abc",
            "commission_type": "recurring"},
           {"cookie_days": None, "commission_rate": "30%", "payout_min": 50},
           {"cookie_days": {"x": 1}, "commission_rate": 25, "payout_min": None}]
    for b in bad:
        s = programs.score(b)   # must not raise
        assert isinstance(s, float)
    print("PASS malformed program data does not crash scoring"); ok += 1

    # cleanup
    try:
        (evil / "x.md").unlink(); evil.rmdir()
    except Exception:
        pass

    print(f"\nALL {ok} CONSOLE HARDENING TESTS PASSED")


if __name__ == "__main__":
    main()
