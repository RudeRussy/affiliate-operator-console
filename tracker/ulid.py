"""Zero-dependency ULID generator: 26-char Crockford base32, lexicographically sortable.

Monotonic within a millisecond: if the clock has not advanced, the random part is
incremented so IDs generated in the same ms still sort in creation order.
"""
import os
import threading
import time

_B32 = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"

_lock = threading.Lock()
_last_ms = -1
_last_rnd = [0] * 16


def _encode_time(t: int) -> str:
    out = ""
    for _ in range(10):
        out = _B32[t & 31] + out
        t >>= 5
    return out


def _inc_rnd(rnd):
    for i in range(15, -1, -1):
        if rnd[i] < 31:
            rnd[i] += 1
            return True
        rnd[i] = 0
    return False  # overflow (astronomically unlikely)


def ulid() -> str:
    global _last_ms, _last_rnd
    with _lock:
        t = int(time.time() * 1000)
        # Guard against clock regression (NTP/manual): never go backwards, or we
        # could emit duplicate IDs.
        if t <= _last_ms:
            _inc_rnd(_last_rnd)
            t = _last_ms
        else:
            _last_ms = t
            _last_rnd = [b & 31 for b in os.urandom(16)]
        ts = _encode_time(t)
        rnd = "".join(_B32[b] for b in _last_rnd)
        return ts + rnd


if __name__ == "__main__":
    ids = [ulid() for _ in range(2000)]
    assert all(len(i) == 26 for i in ids), "ULID length"
    assert ids == sorted(ids), "ULIDs must be monotonically sortable"
    assert len(set(ids)) == len(ids), "ULIDs must be unique"
    print("ULID OK (2000 monotonic + unique), e.g.", ids[0])
