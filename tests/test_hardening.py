"""Unit tests for tracker hardening (currency safety, URL validation, rate limit, ULID).

Run: .venv/bin/python -m tests.test_hardening
"""
import os
import sys


def main():
    from tracker.app import to_gbp, valid_http_url
    from tracker.ulid import ulid
    ok = 0

    # unknown currency must raise, never silently assume 1:1
    try:
        to_gbp(100, "XYZ")
        print("FAIL: unknown currency did not raise"); sys.exit(1)
    except ValueError:
        print("PASS unknown currency raises"); ok += 1

    try:
        to_gbp("abc", "GBP")
        print("FAIL: bad amount did not raise"); sys.exit(1)
    except ValueError:
        print("PASS bad amount raises"); ok += 1

    assert to_gbp(100, "USD") == 79.0 and to_gbp(100, "GBP") == 100.0
    print("PASS known-currency conversion correct"); ok += 1

    for bad in ["javascript:alert(1)", "data:text/html,x", "file:///etc/passwd",
                "not-a-url", ""]:
        assert not valid_http_url(bad), f"should reject {bad!r}"
    print("PASS dangerous URLs rejected"); ok += 1
    for good in ["https://example.com/offer?sub1=x", "http://a.b/c"]:
        assert valid_http_url(good), f"should accept {good!r}"
    print("PASS valid URLs accepted"); ok += 1

    os.environ["TRACKER_RATE_MAX"] = "5"
    import importlib
    import tracker.app as A
    importlib.reload(A)
    hits = [A.rate_limited("9.9.9.9") for _ in range(8)]
    assert hits[:5] == [False] * 5 and hits[5] is True, hits
    print("PASS rate limiter blocks after limit"); ok += 1

    ids = [ulid() for _ in range(5000)]
    assert ids == sorted(ids) and len(set(ids)) == 5000
    print("PASS ULID 5000 monotonic + unique"); ok += 1

    print(f"\nALL {ok} HARDENING TESTS PASSED")


if __name__ == "__main__":
    main()
