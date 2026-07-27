"""Package tracker API + static frontend.

Read-only over the database import.py fills; drift is computed at request
time from server_packages, grouped per (package, os) so SLES and RedHat
builds of the same package are never compared against each other.
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


#
# Drift computation
#

_DRIFT_SQL = """
SELECT
    p.id AS package_id,
    p.name,
    s.os,
    pv.version,
    pv.release,
    pv.arch,
    s.id AS server_id,
    s.hostname,
    s.beheergroep
FROM server_packages sp
JOIN servers s ON s.id = sp.server_id
JOIN package_versions pv ON pv.id = sp.package_version_id
JOIN packages p ON p.id = pv.package_id
WHERE s.inventory_status = 'ACTIVE'
"""


def drift_groups(beheergroep=None, os_filter=None, q=None):
    """Group active-server package rows by (package, os).

    Each group carries its versions ((version, release) -> server rows),
    the newest version per rpmvercmp, and a drifting flag. Filters narrow
    the server set first, so drift is judged within the filtered scope.
    """
    sql = _DRIFT_SQL
    params = []
    if beheergroep:
        sql += " AND s.beheergroep = %s"
        params.append(beheergroep)
    if os_filter:
        sql += " AND s.os = %s"
        params.append(os_filter)
    if q:
        sql += " AND p.name ILIKE %s"
        params.append(f"%{q}%")

    groups = {}
    for row in db.query(sql, params):
        key = (row["package_id"], row["os"])
        group = groups.setdefault(key, {
            "package_id": row["package_id"],
            "name": row["name"],
            "os": row["os"],
            "versions": {},
        })
        group["versions"].setdefault((row["version"], row["release"]), []).append(row)

    for group in groups.values():
        group["latest"] = max(group["versions"], key=rpmver.vr_key)
        group["drifting"] = len(group["versions"]) > 1

    return list(groups.values())


def group_summary(group):
    """JSON shape for one drift group, newest version first."""
    versions = []
    behind = 0
    for vkey in sorted(group["versions"], key=rpmver.vr_key, reverse=True):
        rows = group["versions"][vkey]
        is_latest = vkey == group["latest"]
        if not is_latest:
            behind += len(rows)
        versions.append({
            "version": vkey[0],
            "release": vkey[1],
            "arch": rows[0]["arch"],
            "server_count": len(rows),
            "is_latest": is_latest,
        })
    return {
        "package_id": group["package_id"],
        "name": group["name"],
        "os": group["os"],
        "versions": versions,
        "behind_count": behind,
    }


def behind_per_server():
    """server_id -> number of packages behind the newest in their os group."""
    behind = {}
    for group in drift_groups():
        if not group["drifting"]:
            continue
        for vkey, rows in group["versions"].items():
            if vkey != group["latest"]:
                for r in rows:
                    behind[r["server_id"]] = behind.get(r["server_id"], 0) + 1
    return behind


#
# API
#

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
        SELECT p.id AS package_id, p.name, COUNT(DISTINCT sp.server_id) AS n
        FROM packages p
        JOIN package_versions pv ON pv.package_id = p.id
        JOIN server_packages sp ON sp.package_version_id = pv.id
        JOIN servers s ON s.id = sp.server_id AND s.inventory_status = 'ACTIVE'
        GROUP BY p.id, p.name
        ORDER BY n DESC, p.name
        LIMIT 10
    """)
    last_run = db.query("SELECT * FROM inventory_runs ORDER BY id DESC LIMIT 1")

    groups = drift_groups()
    drifting = [g for g in groups if g["drifting"]]
    behind_servers = set()
    for g in drifting:
        for vkey, rows in g["versions"].items():
            if vkey != g["latest"]:
                behind_servers.update(r["server_id"] for r in rows)

    return jsonify({
        "status_counts": clean(status_counts),
        "os_counts": clean(os_counts),
        "beheergroep_counts": clean(beheergroep_counts),
        "package_count": package_count,
        "top_packages": clean(top_packages),
        "last_run": clean(last_run)[0] if last_run else None,
        "drifting_packages": len(drifting),
        "servers_behind": len(behind_servers),
    })


_SERVER_SORT = {
    "hostname": "s.hostname",
    "beheergroep": "s.beheergroep",
    "owner": "s.owner",
    "os": "s.os",
    "servicelevel": "s.servicelevel",
    "inventory_status": "s.inventory_status",
    "package_count": "package_count",
    "last_seen": "s.last_seen",
}


@app.get("/api/servers")
def servers():
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

    sort = _SERVER_SORT.get(request.args.get("sort", ""), "s.hostname")
    direction = "DESC" if request.args.get("dir") == "desc" else "ASC"

    sql = f"""
        SELECT s.*, COUNT(sp.package_version_id) AS package_count
        FROM servers s
        LEFT JOIN server_packages sp ON sp.server_id = s.id
        {"WHERE " + " AND ".join(where) if where else ""}
        GROUP BY s.id
        ORDER BY {sort} {direction}, s.hostname
    """
    rows = clean(db.query(sql, params))

    behind = behind_per_server()
    for row in rows:
        row["behind_count"] = behind.get(row["id"], 0)

    return jsonify(rows)


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
               sp.install_time
        FROM server_packages sp
        JOIN package_versions pv ON pv.id = sp.package_version_id
        JOIN packages p ON p.id = pv.package_id
        WHERE sp.server_id = %s
        ORDER BY p.name
    """, (server_id,)))

    latest_map = {(g["package_id"], g["os"]): g for g in drift_groups()}
    for pkg in packages:
        group = latest_map.get((pkg["package_id"], server["os"]))
        if group and (pkg["version"], pkg["release"]) != group["latest"]:
            pkg["is_latest"] = False
            pkg["latest_version"], pkg["latest_release"] = group["latest"]
        else:
            pkg["is_latest"] = True

    server["behind_count"] = sum(1 for p in packages if not p["is_latest"])
    return jsonify({"server": server, "packages": packages})


@app.get("/api/packages")
def packages():
    q = request.args.get("q")
    sql = """
        SELECT p.id, p.name,
               COUNT(DISTINCT pv.id) AS version_count,
               COUNT(DISTINCT sp.server_id) AS server_count
        FROM packages p
        LEFT JOIN package_versions pv ON pv.package_id = p.id
        LEFT JOIN server_packages sp ON sp.package_version_id = pv.id
    """
    params = []
    if q:
        sql += " WHERE p.name ILIKE %s"
        params.append(f"%{q}%")
    sql += " GROUP BY p.id, p.name ORDER BY p.name"
    rows = clean(db.query(sql, params))

    drifting_ids = {g["package_id"] for g in drift_groups() if g["drifting"]}
    for row in rows:
        row["has_drift"] = row["id"] in drifting_ids
    return jsonify(rows)


@app.get("/api/packages/<int:package_id>")
def package_detail(package_id):
    pkg = db.query("SELECT id, name FROM packages WHERE id = %s", (package_id,))
    if not pkg:
        abort(404)

    rows = db.query("""
        SELECT s.os, s.inventory_status,
               pv.version, pv.release, pv.arch,
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
        # "Latest" is judged among versions an ACTIVE server still runs.
        active = [v for v, vrows in versions.items()
                  if any(r["inventory_status"] == "ACTIVE" for r in vrows)]
        latest = max(active or versions, key=rpmver.vr_key)

        vlist = []
        for vkey in sorted(versions, key=rpmver.vr_key, reverse=True):
            vrows = versions[vkey]
            vlist.append({
                "version": vkey[0],
                "release": vkey[1],
                "arch": vrows[0]["arch"],
                "is_latest": vkey == latest,
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
            "drifting": len(versions) > 1,
            "versions": vlist,
        })

    return jsonify({
        "id": pkg[0]["id"],
        "name": pkg[0]["name"],
        "os_groups": os_groups,
    })


@app.get("/api/drift")
def drift():
    groups = drift_groups(
        beheergroep=request.args.get("beheergroep"),
        os_filter=request.args.get("os"),
        q=request.args.get("q"),
    )
    report = [group_summary(g) for g in groups if g["drifting"]]
    report.sort(key=lambda g: (-g["behind_count"], g["name"]))
    return jsonify(report)


@app.get("/api/runs")
def runs():
    return jsonify(clean(db.query(
        "SELECT * FROM inventory_runs ORDER BY id DESC LIMIT 100")))


if __name__ == "__main__":
    app.run(port=8000, debug=True)
