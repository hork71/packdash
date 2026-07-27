"""Pure-Python RPM version comparison (rpmvercmp algorithm).

Splits version strings into numeric and alphabetic segments and compares
segment-wise, the way rpm itself does, so "6.0.45" > "6.0.9" and
"1.2~rc1" < "1.2". No external libraries.
"""

import re
from functools import cmp_to_key

_SEGMENT = re.compile(r"(\d+|[a-zA-Z]+|~)")


def rpmvercmp(a, b):
    """Compare two RPM version or release strings. Returns -1, 0 or 1."""
    if a == b:
        return 0

    sa = _SEGMENT.findall(a)
    sb = _SEGMENT.findall(b)

    i = 0
    while True:
        ca = sa[i] if i < len(sa) else None
        cb = sb[i] if i < len(sb) else None

        # Tilde sorts before everything, including end-of-string.
        if ca == "~" or cb == "~":
            if ca != "~":
                return 1
            if cb != "~":
                return -1
            i += 1
            continue

        if ca is None and cb is None:
            return 0
        if ca is None:
            return -1
        if cb is None:
            return 1

        if ca.isdigit() and cb.isdigit():
            na, nb = int(ca), int(cb)
            if na != nb:
                return 1 if na > nb else -1
        elif ca.isdigit():
            # A numeric segment always beats an alphabetic one.
            return 1
        elif cb.isdigit():
            return -1
        elif ca != cb:
            return 1 if ca > cb else -1

        i += 1


def compare_vr(a, b):
    """Compare two (version, release) tuples."""
    c = rpmvercmp(a[0], b[0])
    if c != 0:
        return c
    return rpmvercmp(a[1], b[1])


# Usable as sort key for (version, release) tuples.
vr_key = cmp_to_key(compare_vr)
