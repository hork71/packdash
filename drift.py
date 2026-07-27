"""Materialize package drift after an import.

Computes, per (package, os) group, the newest installed version
(rpmvercmp, judged among ACTIVE servers) and stores the result in
package_drift plus the server_packages.is_latest flag. The API then
only runs small indexed queries instead of scanning the whole
inventory on every request.

import.py calls materialize() inside its transaction; run this file
standalone to rebuild the tables without a fresh import:

    python drift.py
"""

import os

import psycopg2
from psycopg2.extras import execute_values

import rpmver


def materialize(cur):
    """Rebuild package_drift and server_packages.is_latest.

    Returns (group_count, drifting_count).
    """
    cur.execute("""
        SELECT pv.package_id, s.os, s.inventory_status,
               pv.id, pv.version, pv.release
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        JOIN package_versions pv ON pv.id = sp.package_version_id
    """)

    groups = {}
    for package_id, os_name, status, version_id, version, release in cur.fetchall():
        versions = groups.setdefault((package_id, os_name), {})
        entry = versions.setdefault(version_id, {
            "vr": (version, release),
            "active": 0,
        })
        if status == "ACTIVE":
            entry["active"] += 1

    rows = []
    for (package_id, os_name), versions in groups.items():
        active_ids = [vid for vid, e in versions.items() if e["active"]]

        # "Latest" is judged among versions an ACTIVE server still runs;
        # groups seen only on MISSING servers fall back to all versions.
        candidates = active_ids or list(versions)
        latest_id = max(
            candidates,
            key=lambda vid: rpmver.vr_key(versions[vid]["vr"])
        )

        rows.append((
            package_id,
            os_name,
            latest_id,
            len(active_ids),
            sum(e["active"] for e in versions.values()),
            sum(e["active"] for vid, e in versions.items() if vid != latest_id),
        ))

    cur.execute("DELETE FROM package_drift")

    execute_values(cur, """
        INSERT INTO package_drift(
            package_id, os, latest_version_id,
            version_count, server_count, behind_count
        )
        VALUES %s
    """, rows)

    cur.execute("""
        UPDATE server_packages sp
        SET is_latest = (sp.package_version_id = pd.latest_version_id)
        FROM servers s, package_versions pv, package_drift pd
        WHERE s.id = sp.server_id
          AND pv.id = sp.package_version_id
          AND pd.package_id = pv.package_id
          AND pd.os = s.os
    """)

    drifting = sum(1 for r in rows if r[3] > 1)
    return len(rows), drifting


if __name__ == "__main__":
    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        dbname=os.environ.get("PGDATABASE", "extrap"),
        user=os.environ.get("PGUSER", "testuser"),
        password=os.environ.get("PGPASSWORD"),
    )
    cur = conn.cursor()

    total, drifting = materialize(cur)

    conn.commit()
    cur.close()
    conn.close()

    print(f"Drift materialized: {total} package/os groups, {drifting} drifting.")
