"""Normalize (os, osversie) to the drift level the fleet is grouped by.

RedHat and SUSE package builds are compatible within a major release
(el9, sles15), so those families group by major. For Ubuntu the
major.minor pair IS the release identity (22.04 vs 24.04), so it is
kept whole; unknown families fall back to the full osversie string.
"""

_MAJOR_ONLY = {"RedHat", "SLES", "SUSE"}


def os_release(os_name, osversie):
    if not osversie:
        return "unknown"
    if os_name in _MAJOR_ONLY:
        return osversie.split(".")[0]
    return osversie
