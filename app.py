"""Package tracker API + static frontend.

Read-only over the database import.py fills. Drift is precomputed at
import time (drift.py) into package_drift + server_packages.is_latest,
so requests only run small indexed queries; list endpoints paginate.
"""

import uuid as uuidlib
from datetime import datetime

from flask import Flask, abort, jsonify, request, send_from_directory

import db
import rpmver

app = Flask(__name__)
app.teardown_appcontext(db.close_conn)

_SEVERITY_RANK = {"Critical": 4, "Important": 3, "Moderate": 2, "Low": 1}


def _max_severity(severities):
    ranked = [s for s in severities if s in _SEVERITY_RANK]
    if not ranked:
        return None
    return max(ranked, key=lambda s: _SEVERITY_RANK[s])

def suma_advisory_link(id, source):

    links = {
        "suma4": f"https://server1.com/rhn/errata/details/Details.do?eid={id}",
        "suma5": f"https://server2.com/rhn/errata/details/Details.do?eid={id}"
    }

    return links.get(source, '')


@app.get("/")
def index():
    return send_from_directory("static", "index.html")


def clean(rows):
    """RealDictRows -> plain dicts with ISO date strings."""
    out = []
    for r in rows:
        d = dict(r)
        for k, v in d.items():
            if isinstance(v, datetime):
                d[k] = v.isoformat()
        out.append(d)
    return out


def page_params(default_limit=50, max_limit=200):
    try:
        limit = int(request.args.get("limit", default_limit))
        offset = int(request.args.get("offset", 0))
    except ValueError:
        return default_limit, 0
    return max(1, min(limit, max_limit)), max(offset, 0)


@app.get("/api/stats")
def stats():
    status_counts = db.query("""
        SELECT inventory_status, COUNT(*) AS n
        FROM servers
        GROUP BY inventory_status
    """)
    os_counts = db.query("""
        SELECT os, os_release,
               os || ' ' || os_release AS label,
               COUNT(*) AS n
        FROM servers
        WHERE inventory_status = 'ACTIVE'
        GROUP BY os, os_release
        ORDER BY n DESC, os, os_release
    """)
    beheergroep_counts = db.query("""
        SELECT beheergroep, COUNT(*) AS n
        FROM servers
        WHERE inventory_status = 'ACTIVE'
        GROUP BY beheergroep
        ORDER BY n DESC, beheergroep
    """)
    package_count = db.query("SELECT COUNT(*) AS n FROM packages")[0]["n"]
    top_packages = db.query("""
        SELECT p.id AS package_id, p.name, SUM(pd.server_count) AS n
        FROM package_drift pd
        JOIN packages p ON p.id = pd.package_id
        GROUP BY p.id, p.name
        ORDER BY n DESC, p.name
        LIMIT 10
    """)
    last_run = db.query("SELECT * FROM inventory_runs ORDER BY id DESC LIMIT 1")

    drifting_packages = db.query("""
        SELECT COUNT(*) AS n FROM package_drift WHERE version_count > 1
    """)[0]["n"]
    servers_behind = db.query("""
        SELECT COUNT(DISTINCT sp.server_id) AS n
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        WHERE NOT sp.is_latest AND s.inventory_status = 'ACTIVE'
    """)[0]["n"]
    advisory_packages = db.query("""
        SELECT COUNT(DISTINCT pd.package_id) AS n
        FROM package_drift pd
        JOIN package_version_errata pve ON pve.package_version_id = pd.latest_version_id
        WHERE pd.version_count > 1
    """)[0]["n"]

    return jsonify({
        "status_counts": clean(status_counts),
        "os_counts": clean(os_counts),
        "beheergroep_counts": clean(beheergroep_counts),
        "package_count": package_count,
        "top_packages": clean(top_packages),
        "last_run": clean(last_run)[0] if last_run else None,
        "drifting_packages": drifting_packages,
        "servers_behind": servers_behind,
        "advisory_packages": advisory_packages,
    })


_SERVER_SORT = {
    "hostname": "s.hostname",
    "beheergroep": "s.beheergroep",
    "owner": "s.owner",
    "os": "s.os",
    "servicelevel": "s.servicelevel",
    "inventory_status": "s.inventory_status",
    "package_count": "package_count",
    "behind_count": "behind_count",
    "last_seen": "s.last_seen",
}


@app.get("/api/servers")
def servers():
    limit, offset = page_params()
    where = []
    params = []
    for field, column in (
        ("os", "s.os"),
        ("os_release", "s.os_release"),
        ("status", "s.inventory_status"),
    ):
        value = request.args.get(field)
        if value:
            where.append(f"{column} = %s")
            params.append(value)
    beheergroep = request.args.get("beheergroep")
    if beheergroep:
        where.append("s.beheergroep ILIKE %s")
        params.append(f"%{beheergroep}%")
    q = request.args.get("q")
    if q:
        where.append("s.hostname ILIKE %s")
        params.append(f"%{q}%")

    where_sql = ("WHERE " + " AND ".join(where)) if where else ""
    sort = _SERVER_SORT.get(request.args.get("sort", ""), "s.hostname")
    direction = "DESC" if request.args.get("dir") == "desc" else "ASC"

    total = db.query(
        f"SELECT COUNT(*) AS n FROM servers s {where_sql}", params)[0]["n"]

    rows = clean(db.query(f"""
        SELECT s.*,
               (SELECT COUNT(*) FROM server_packages sp
                WHERE sp.server_id = s.id) AS package_count,
               (SELECT COUNT(*) FROM server_packages sp
                WHERE sp.server_id = s.id AND NOT sp.is_latest) AS behind_count
        FROM servers s
        {where_sql}
        ORDER BY {sort} {direction}, s.hostname
        LIMIT %s OFFSET %s
    """, params + [limit, offset]))

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": rows})


@app.get("/api/servers/<server_id>")
def server_detail(server_id):
    try:
        uuidlib.UUID(server_id)
    except ValueError:
        abort(404)

    rows = db.query("SELECT * FROM servers WHERE id = %s", (server_id,))
    if not rows:
        abort(404)
    server = clean(rows)[0]

    packages = clean(db.query("""
        SELECT p.id AS package_id, p.name,
               pv.version, pv.release, pv.arch,
               sp.install_time, sp.is_latest,
               lv.version AS latest_version, lv.release AS latest_release
        FROM server_packages sp
        JOIN package_versions pv ON pv.id = sp.package_version_id
        JOIN packages p ON p.id = pv.package_id
        JOIN servers s ON s.id = sp.server_id
        LEFT JOIN package_drift pd
               ON pd.package_id = pv.package_id
              AND pd.os = s.os
              AND pd.os_release = s.os_release
        LEFT JOIN package_versions lv ON lv.id = pd.latest_version_id
        WHERE sp.server_id = %s
        ORDER BY p.name
    """, (server_id,)))

    server["behind_count"] = sum(1 for p in packages if not p["is_latest"])
    return jsonify({"server": server, "packages": packages})


@app.get("/api/packages")
def packages():
    limit, offset = page_params()
    q = request.args.get("q")
    where_sql = "WHERE p.name ILIKE %s" if q else ""
    params = [f"%{q}%"] if q else []

    total = db.query(
        f"SELECT COUNT(*) AS n FROM packages p {where_sql}", params)[0]["n"]

    rows = clean(db.query(f"""
        SELECT p.id, p.name,
               (SELECT COUNT(*) FROM package_versions pv
                WHERE pv.package_id = p.id) AS version_count,
               (SELECT COUNT(DISTINCT sp.server_id)
                FROM server_packages sp
                JOIN package_versions pv ON pv.id = sp.package_version_id
                WHERE pv.package_id = p.id) AS server_count,
               EXISTS(SELECT 1 FROM package_drift pd
                      WHERE pd.package_id = p.id
                        AND pd.version_count > 1) AS has_drift
        FROM packages p
        {where_sql}
        ORDER BY p.name
        LIMIT %s OFFSET %s
    """, params + [limit, offset]))

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": rows})


@app.get("/api/packages/<int:package_id>")
def package_detail(package_id):
    pkg = db.query("SELECT id, name FROM packages WHERE id = %s", (package_id,))
    if not pkg:
        abort(404)

    latest_ids = {(r["os"], r["os_release"]): r["latest_version_id"]
                  for r in db.query("""
        SELECT os, os_release, latest_version_id
        FROM package_drift
        WHERE package_id = %s
    """, (package_id,))}

    rows = db.query("""
        SELECT s.os, s.os_release, s.inventory_status,
               pv.id AS version_id, pv.version, pv.release, pv.arch,
               s.id AS server_id, s.hostname, s.beheergroep, s.osversie,
               sp.install_time
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        JOIN package_versions pv ON pv.id = sp.package_version_id
        WHERE pv.package_id = %s
        ORDER BY s.hostname
    """, (package_id,))

    by_level = {}
    for row in rows:
        by_level.setdefault((row["os"], row["os_release"]), {}).setdefault(
            (row["version"], row["release"]), []).append(row)

    advisories_by_version = {}
    target_ids = [vid for vid in latest_ids.values() if vid]
    if target_ids:
        adv_rows = db.query("""
            SELECT pve.package_version_id, e.advisory_name, e.advisory_type,
                   e.synopsis, e.severity, e.issue_date, e.advisory_id, e.suma_source,
                   array_agg(DISTINCT ec.cve) FILTER (WHERE ec.cve IS NOT NULL) AS cves
            FROM package_version_errata pve
            JOIN errata e ON e.advisory_name = pve.advisory_name
            LEFT JOIN errata_cves ec ON ec.advisory_name = e.advisory_name
            WHERE pve.package_version_id = ANY(%s)
            GROUP BY pve.package_version_id, e.advisory_name, e.advisory_type,
                     e.synopsis, e.severity, e.issue_date
            ORDER BY CASE e.severity
                WHEN 'Critical' THEN 1 WHEN 'Important' THEN 2
                WHEN 'Moderate' THEN 3 WHEN 'Low' THEN 4 ELSE 5 END,
                e.advisory_name
        """, (target_ids,))
        for row in adv_rows:
            advisories_by_version.setdefault(row["package_version_id"], []).append({
                "advisory_name": row["advisory_name"],
                "advisory_type": row["advisory_type"],
                "synopsis": row["synopsis"],
                "severity": row["severity"],
                "advisory_link": suma_advisory_link(row["advisory_id"], row["suma_source"]),
                "issue_date": row["issue_date"].isoformat() if row["issue_date"] else None,
                "cves": row["cves"] or [],
            })

    os_groups = []
    for (os_name, os_release), versions in sorted(by_level.items()):
        latest_id = latest_ids.get((os_name, os_release))

        vlist = []
        for vkey in sorted(versions, key=rpmver.vr_key, reverse=True):
            vrows = versions[vkey]
            is_latest = any(r["version_id"] == latest_id for r in vrows)
            vlist.append({
                "version": vkey[0],
                "release": vkey[1],
                "arch": vrows[0]["arch"],
                "is_latest": is_latest,
                "advisories": advisories_by_version.get(latest_id, []) if is_latest else [],
                "servers": clean([{
                    "id": r["server_id"],
                    "hostname": r["hostname"],
                    "osversie": r["osversie"],
                    "beheergroep": r["beheergroep"],
                    "inventory_status": r["inventory_status"],
                    "install_time": r["install_time"],
                } for r in vrows]),
            })
        os_groups.append({
            "os": os_name,
            "os_release": os_release,
            "drifting": len(vlist) > 1,
            "versions": vlist,
        })

    return jsonify({
        "id": pkg[0]["id"],
        "name": pkg[0]["name"],
        "os_groups": os_groups,
    })


def _drift_fleet(os_filter, os_release, q, limit, offset):
    """Fleet-wide drift: read straight from the materialized package_drift
    table, so the common (unfiltered) case stays a fast indexed read."""
    where = ["pd.version_count > 1"]
    params = []
    if os_filter:
        where.append("pd.os = %s")
        params.append(os_filter)
    if os_release:
        where.append("pd.os_release = %s")
        params.append(os_release)
    if q:
        where.append("p.name ILIKE %s")
        params.append(f"%{q}%")

    where_sql = " AND ".join(where)
    total = db.query(f"""
        SELECT COUNT(*) AS n
        FROM package_drift pd
        JOIN packages p ON p.id = pd.package_id
        WHERE {where_sql}
    """, params)[0]["n"]

    groups = clean(db.query(f"""
        SELECT pd.package_id, p.name, pd.os, pd.os_release, pd.behind_count,
               pd.latest_version_id
        FROM package_drift pd
        JOIN packages p ON p.id = pd.package_id
        WHERE {where_sql}
        ORDER BY pd.behind_count DESC, p.name, pd.os, pd.os_release
        LIMIT %s OFFSET %s
    """, params + [limit, offset]))

    return total, groups


def _drift_scoped(beheergroep, os_filter, os_release, q, limit, offset):
    """Drift within one beheergroep, computed live against each server's
    stored is_latest flag (still judged against the fleet-wide latest
    version — the reference point stays fleet-wide, only what's measured
    against it is scoped). A package only appears if this beheergroep
    itself has a server behind, so packages fully in sync here drop out
    even when the fleet as a whole is drifting on them.
    """
    where = ["s.inventory_status = 'ACTIVE'", "s.beheergroep ILIKE %s"]
    params = [f"%{beheergroep}%"]
    if os_filter:
        where.append("s.os = %s")
        params.append(os_filter)
    if os_release:
        where.append("s.os_release = %s")
        params.append(os_release)
    if q:
        where.append("p.name ILIKE %s")
        params.append(f"%{q}%")

    where_sql = " AND ".join(where)
    behind_having = "HAVING COUNT(*) FILTER (WHERE NOT sp.is_latest) > 0"

    total = db.query(f"""
        SELECT COUNT(*) AS n FROM (
            SELECT 1
            FROM server_packages sp
            JOIN servers s ON s.id = sp.server_id
            JOIN package_versions pv ON pv.id = sp.package_version_id
            JOIN packages p ON p.id = pv.package_id
            WHERE {where_sql}
            GROUP BY pv.package_id, s.os, s.os_release
            {behind_having}
        ) AS drifting_groups
    """, params)[0]["n"]

    groups = clean(db.query(f"""
        SELECT pv.package_id, p.name, s.os, s.os_release,
               COUNT(*) FILTER (WHERE NOT sp.is_latest) AS behind_count,
               pd.latest_version_id
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        JOIN package_versions pv ON pv.id = sp.package_version_id
        JOIN packages p ON p.id = pv.package_id
        JOIN package_drift pd
               ON pd.package_id = pv.package_id
              AND pd.os = s.os
              AND pd.os_release = s.os_release
        WHERE {where_sql}
        GROUP BY pv.package_id, p.name, s.os, s.os_release, pd.latest_version_id
        {behind_having}
        ORDER BY behind_count DESC, p.name, s.os, s.os_release
        LIMIT %s OFFSET %s
    """, params + [limit, offset]))

    return total, groups


@app.get("/api/drift")
def drift():
    limit, offset = page_params()
    os_filter = request.args.get("os")
    os_release = request.args.get("os_release")
    q = request.args.get("q")
    beheergroep = request.args.get("beheergroep")

    if beheergroep:
        total, groups = _drift_scoped(beheergroep, os_filter, os_release, q, limit, offset)
    else:
        total, groups = _drift_fleet(os_filter, os_release, q, limit, offset)

    # Version spread for just this page of groups, scoped the same way.
    if groups:
        keys = tuple((g["package_id"], g["os"], g["os_release"]) for g in groups)
        spread_where = ["s.inventory_status = 'ACTIVE'",
                         "(pv.package_id, s.os, s.os_release) IN %s"]
        spread_params = [keys]
        if beheergroep:
            spread_where.append("s.beheergroep ILIKE %s")
            spread_params.append(f"%{beheergroep}%")

        spread = db.query(f"""
            SELECT pv.package_id, s.os, s.os_release, pv.version, pv.release,
                   sp.is_latest, MIN(pv.arch) AS arch,
                   COUNT(*) AS server_count
            FROM server_packages sp
            JOIN servers s ON s.id = sp.server_id
            JOIN package_versions pv ON pv.id = sp.package_version_id
            WHERE {" AND ".join(spread_where)}
            GROUP BY pv.package_id, s.os, s.os_release,
                     pv.version, pv.release, sp.is_latest
        """, spread_params)

        by_key = {}
        for row in spread:
            by_key.setdefault(
                (row["package_id"], row["os"], row["os_release"]), []).append(row)

        for g in groups:
            versions = by_key.get((g["package_id"], g["os"], g["os_release"]), [])
            versions.sort(
                key=lambda v: rpmver.vr_key((v["version"], v["release"])),
                reverse=True)
            g["versions"] = [{
                "version": v["version"],
                "release": v["release"],
                "arch": v["arch"],
                "server_count": v["server_count"],
                "is_latest": v["is_latest"],
            } for v in versions]

        # Advisory summary for the newest version of each group on this
        # page — enough for a badge without a second page load. Full
        # advisory/CVE detail lives on the package detail page.
        version_ids = {g["latest_version_id"] for g in groups if g.get("latest_version_id")}
        adv_by_version = {}
        if version_ids:
            adv_rows = db.query("""
                SELECT pve.package_version_id, e.severity
                FROM package_version_errata pve
                JOIN errata e ON e.advisory_name = pve.advisory_name
                WHERE pve.package_version_id IN %s
            """, (tuple(version_ids),))
            for row in adv_rows:
                adv_by_version.setdefault(row["package_version_id"], []).append(row["severity"])

        for g in groups:
            severities = adv_by_version.get(g.get("latest_version_id"), [])
            g["advisories"] = (
                {"count": len(severities), "max_severity": _max_severity(severities)}
                if severities else None
            )
            g.pop("latest_version_id", None)

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": groups})


@app.get("/api/runs")
def runs():
    return jsonify(clean(db.query(
        "SELECT * FROM inventory_runs ORDER BY id DESC LIMIT 100")))


if __name__ == "__main__":
    app.run(port=8000, debug=True)
