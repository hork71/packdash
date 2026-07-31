import json
import os
import sys
from datetime import datetime
from io import StringIO

import psycopg2
from psycopg2.extras import execute_values

import drift
import oslevel


def parse_install_time(value):
    """Convert RPM install time to datetime.

    Two incoming formats, parsed by position because strptime is too
    slow for millions of entries:
      "3/29/22 3:53:58 PM CEST" (suma4)
      "20180323T18:16:34"       (suma5, xmlrpc DateTime)
    """

    if not value:
        return None

    try:
        if len(value) == 17 and value[8] == "T":
            return datetime(
                int(value[0:4]), int(value[4:6]), int(value[6:8]),
                int(value[9:11]), int(value[12:14]), int(value[15:17])
            )

        datepart, timepart, ampm = value.split(" ")[:3]
        month, day, year = datepart.split("/")
        hour, minute, second = timepart.split(":")

        uur = int(hour) % 12
        if ampm == "PM":
            uur += 12

        return datetime(
            2000 + int(year), int(month), int(day),
            uur, int(minute), int(second)
        )
    except (ValueError, IndexError):
        # Unexpected variant: let strptime have a go.
        return datetime.strptime(
            value.rsplit(" ", 1)[0],
            "%m/%d/%y %I:%M:%S %p"
        )


conn = psycopg2.connect(
    host=os.environ.get("PGHOST", "localhost"),
    database=os.environ.get("PGDATABASE", "extrap"),
    user=os.environ.get("PGUSER", "testuser"),
    password=os.environ.get("PGPASSWORD"),
)

conn.autocommit = False

cur = conn.cursor()


cur.execute("""
INSERT INTO inventory_runs(source)
VALUES(%s)
RETURNING id;
""", ("JSON Import",))

inventory_run_id = cur.fetchone()[0]

print(f"Inventory run {inventory_run_id}")


input_file = sys.argv[1] if len(sys.argv) > 1 else "xtra.json"

with open(input_file) as f:
    servers = json.load(f)


#
# A uuid must identify exactly one server; if the export lists the same
# uuid for multiple hostnames we cannot tell which data is right, so skip
# them all instead of letting the last one silently overwrite the rest.
#

hostnames_by_uuid = {}

for server in servers:
    if server["suma"] and server["uuid"]:
        hostnames_by_uuid.setdefault(server["uuid"], []).append(server["naam"])

duplicate_uuids = {
    uuid: names
    for uuid, names in hostnames_by_uuid.items()
    if len(names) > 1
}

for uuid, names in duplicate_uuids.items():
    print(f"WARNING: uuid {uuid} shared by {', '.join(names)} - skipped")


imported = [
    server for server in servers
    if server["suma"] and server["uuid"]
    and server["uuid"] not in duplicate_uuids
]


#
# Server upserts
#

for server in imported:

    cur.execute("""
    INSERT INTO servers (

        id,
        hostname,
        beheergroep,
        beheeremail,
        owner,
        servicelevel,
        os,
        osversie,
        os_release,
        suma,
        apiversie,

        inventory_status,
        last_seen,
        last_inventory_run

    )
    VALUES (
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
        'ACTIVE',
        NOW(),
        %s
    )

    ON CONFLICT (id)

    DO UPDATE SET

        hostname = EXCLUDED.hostname,
        beheergroep = EXCLUDED.beheergroep,
        beheeremail = EXCLUDED.beheeremail,
        owner = EXCLUDED.owner,
        servicelevel = EXCLUDED.servicelevel,
        os = EXCLUDED.os,
        osversie = EXCLUDED.osversie,
        os_release = EXCLUDED.os_release,
        suma = EXCLUDED.suma,
        apiversie = EXCLUDED.apiversie,

        inventory_status = 'ACTIVE',
        last_seen = NOW(),
        last_inventory_run = EXCLUDED.last_inventory_run;
    """,
    (
        server["uuid"],
        server.get("naam"),
        server["beheergroep"],
        server["beheeremail"],
        server["owner"],
        server["sl"],
        server["os"],
        server["osversie"],
        oslevel.os_release(server["os"], server["osversie"]),
        server["suma"],
        server["apiversie"],
        inventory_run_id
    ))


#
# Dimension caches: one query per table instead of several statements
# per package entry. New names/versions are bulk-inserted once.
#

cur.execute("SELECT id, name FROM packages")
package_ids = {name: pid for pid, name in cur.fetchall()}

new_names = set()
for server in imported:
    for pkg in server["extraPackages"]:
        if pkg["name"] not in package_ids:
            new_names.add(pkg["name"])

if new_names:
    execute_values(cur, """
        INSERT INTO packages(name)
        VALUES %s
        ON CONFLICT (name) DO NOTHING
    """, [(name,) for name in new_names], page_size=10000)

    cur.execute("SELECT id, name FROM packages")
    package_ids = {name: pid for pid, name in cur.fetchall()}


cur.execute("SELECT id, package_id, version, release, arch FROM package_versions")
version_ids = {(p, v, r, a): vid for vid, p, v, r, a in cur.fetchall()}

new_versions = set()
for server in imported:
    for pkg in server["extraPackages"]:
        key = (package_ids[pkg["name"]], pkg["version"], pkg["release"], pkg["arch"])
        if key not in version_ids:
            new_versions.add(key)

if new_versions:
    execute_values(cur, """
        INSERT INTO package_versions(package_id, version, release, arch)
        VALUES %s
        ON CONFLICT (package_id, version, release, arch) DO NOTHING
    """, list(new_versions), page_size=10000)

    cur.execute("SELECT id, package_id, version, release, arch FROM package_versions")
    version_ids = {(p, v, r, a): vid for vid, p, v, r, a in cur.fetchall()}


#
# Link server/package: COPY into a staging table in batches, then one
# set-based insert, instead of one INSERT per entry.
#

cur.execute("""
DELETE FROM server_packages
WHERE server_id = ANY(%s::uuid[])
""",
([server["uuid"] for server in imported],))

cur.execute("""
CREATE TEMP TABLE staging_server_packages (
    server_id UUID,
    package_version_id BIGINT,
    install_time TIMESTAMPTZ
) ON COMMIT DROP
""")

BATCH_SIZE = 200000

buffer = StringIO()
buffered = 0
entry_count = 0

for server in imported:
    server_id = server["uuid"]

    for pkg in server["extraPackages"]:
        version_id = version_ids[
            (package_ids[pkg["name"]], pkg["version"], pkg["release"], pkg["arch"])
        ]
        ts = parse_install_time(pkg["installtime"])
        ts_text = ts.isoformat(sep=" ") if ts else "\\N"

        buffer.write(f"{server_id}\t{version_id}\t{ts_text}\n")
        buffered += 1
        entry_count += 1

        if buffered >= BATCH_SIZE:
            buffer.seek(0)
            cur.copy_expert("COPY staging_server_packages FROM STDIN", buffer)
            buffer.seek(0)
            buffer.truncate(0)
            buffered = 0

if buffered:
    buffer.seek(0)
    cur.copy_expert("COPY staging_server_packages FROM STDIN", buffer)

cur.execute("""
INSERT INTO server_packages(

    server_id,
    package_version_id,
    install_time

)
SELECT DISTINCT ON (server_id, package_version_id)

    server_id,
    package_version_id,
    install_time

FROM staging_server_packages
ORDER BY server_id, package_version_id;
""")

print(f"{len(imported)} servers, {entry_count} package entries, "
      f"{len(new_names)} new packages, {len(new_versions)} new versions")


cur.execute("""
UPDATE servers

SET inventory_status='MISSING'

WHERE

    inventory_status='ACTIVE'

    AND last_inventory_run <> %s;
""",
(inventory_run_id,))


group_count, drifting_count = drift.materialize(cur)

print(f"Drift materialized: {group_count} package/os groups, {drifting_count} drifting")


cur.execute("""
UPDATE inventory_runs

SET

    completed_at = NOW(),
    server_count = %s

WHERE id=%s;
""",
(
    len(servers),
    inventory_run_id
))

conn.commit()

cur.close()
conn.close()

print("Non-Compliant import compleet.")
