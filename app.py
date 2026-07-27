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
        SELECT os, COUNT(*) AS n
        FROM servers
        WHERE inventory_status = 'ACTIVE'
        GROUP BY os
        ORDER BY n DESC, os
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

    return jsonify({
        "status_counts": clean(status_counts),
        "os_counts": clean(os_counts),
        "beheergroep_counts": clean(beheergroep_counts),
        "package_count": package_count,
        "top_packages": clean(top_packages),
        "last_run": clean(last_run)[0] if last_run else None,
        "drifting_packages": drifting_packages,
        "servers_behind": servers_behind,
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
        ("beheergroep", "s.beheergroep"),
        ("os", "s.os"),
        ("status", "s.inventory_status"),
    ):
        value = request.args.get(field)
        if value:
            where.append(f"{column} = %s")
            params.append(value)
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
               ON pd.package_id = pv.package_id AND pd.os = s.os
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

    latest_ids = {r["os"]: r["latest_version_id"] for r in db.query("""
        SELECT os, latest_version_id
        FROM package_drift
        WHERE package_id = %s
    """, (package_id,))}

    rows = db.query("""
        SELECT s.os, s.inventory_status,
               pv.id AS version_id, pv.version, pv.release, pv.arch,
               s.id AS server_id, s.hostname, s.beheergroep,
               sp.install_time
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        JOIN package_versions pv ON pv.id = sp.package_version_id
        WHERE pv.package_id = %s
        ORDER BY s.hostname
    """, (package_id,))

    by_os = {}
    for row in rows:
        by_os.setdefault(row["os"], {}).setdefault(
            (row["version"], row["release"]), []).append(row)

    os_groups = []
    for os_name, versions in sorted(by_os.items()):
        latest_id = latest_ids.get(os_name)

        vlist = []
        for vkey in sorted(versions, key=rpmver.vr_key, reverse=True):
            vrows = versions[vkey]
            vlist.append({
                "version": vkey[0],
                "release": vkey[1],
                "arch": vrows[0]["arch"],
                "is_latest": any(r["version_id"] == latest_id for r in vrows),
                "servers": clean([{
                    "id": r["server_id"],
                    "hostname": r["hostname"],
                    "beheergroep": r["beheergroep"],
                    "inventory_status": r["inventory_status"],
                    "install_time": r["install_time"],
                } for r in vrows]),
            })
        os_groups.append({
            "os": os_name,
            "drifting": len(vlist) > 1,
            "versions": vlist,
        })

    return jsonify({
        "id": pkg[0]["id"],
        "name": pkg[0]["name"],
        "os_groups": os_groups,
    })


@app.get("/api/drift")
def drift():
    limit, offset = page_params()
    where = ["pd.version_count > 1"]
    params = []
    os_filter = request.args.get("os")
    if os_filter:
        where.append("pd.os = %s")
        params.append(os_filter)
    q = request.args.get("q")
    if q:
        where.append("p.name ILIKE %s")
        params.append(f"%{q}%")
    beheergroep = request.args.get("beheergroep")
    if beheergroep:
        # Groups with at least one active server in this beheergroep;
        # "latest" itself stays fleet-wide.
        where.append("""EXISTS (
            SELECT 1 FROM server_packages sp
            JOIN servers s ON s.id = sp.server_id
            JOIN package_versions pv ON pv.id = sp.package_version_id
            WHERE pv.package_id = pd.package_id
              AND s.os = pd.os
              AND s.inventory_status = 'ACTIVE'
              AND s.beheergroep = %s
        )""")
        params.append(beheergroep)

    where_sql = " AND ".join(where)
    total = db.query(f"""
        SELECT COUNT(*) AS n
        FROM package_drift pd
        JOIN packages p ON p.id = pd.package_id
        WHERE {where_sql}
    """, params)[0]["n"]

    groups = clean(db.query(f"""
        SELECT pd.package_id, p.name, pd.os, pd.behind_count
        FROM package_drift pd
        JOIN packages p ON p.id = pd.package_id
        WHERE {where_sql}
        ORDER BY pd.behind_count DESC, p.name, pd.os
        LIMIT %s OFFSET %s
    """, params + [limit, offset]))

    # Version spread for just this page of groups.
    if groups:
        keys = tuple((g["package_id"], g["os"]) for g in groups)
        spread = db.query("""
            SELECT pv.package_id, s.os, pv.version, pv.release,
                   sp.is_latest, MIN(pv.arch) AS arch,
                   COUNT(*) AS server_count
            FROM server_packages sp
            JOIN servers s ON s.id = sp.server_id
            JOIN package_versions pv ON pv.id = sp.package_version_id
            WHERE s.inventory_status = 'ACTIVE'
              AND (pv.package_id, s.os) IN %s
            GROUP BY pv.package_id, s.os, pv.version, pv.release, sp.is_latest
        """, (keys,))

        by_key = {}
        for row in spread:
            by_key.setdefault((row["package_id"], row["os"]), []).append(row)

        for g in groups:
            versions = by_key.get((g["package_id"], g["os"]), [])
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

    return jsonify({"total": total, "limit": limit, "offset": offset, "items": groups})


@app.get("/api/runs")
def runs():
    return jsonify(clean(db.query(
        "SELECT * FROM inventory_runs ORDER BY id DESC LIMIT 100")))


if __name__ == "__main__":
    app.run(port=8000, debug=True)
