"""ULID generation (time-ordered, 26-char Crockford base32) without an extra dependency."""
from __future__ import annotations

import os
import time

_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def new_ulid(ms: int | None = None) -> str:
    if ms is None:
        ms = time.time_ns() // 1_000_000
    value = (ms << 80) | int.from_bytes(os.urandom(10), "big")
    chars = []
    for _ in range(26):
        chars.append(_ALPHABET[value & 31])
        value >>= 5
    return "".join(reversed(chars))
