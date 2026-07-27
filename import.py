import json
from datetime import datetime

import psycopg2
from psycopg2.extras import execute_values


def parse_install_time(value):
    """Convert RPM install time to datetime."""

    if not value:
        return None

    value = value.rsplit(" ", 1)[0]

    return datetime.strptime(
        value,
        "%m/%d/%y %I:%M:%S %p"
    )


conn = psycopg2.connect(
    host="localhost",
    database="extrap",
    user="testuser"
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


with open("xtra.json") as f:
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


for server in servers:
    if server["suma"] == False or server["uuid"] == "":
        continue

    if server["uuid"] in duplicate_uuids:
        continue

    hostname = server.get("naam")

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
        suma,
        apiversie,

        inventory_status,
        last_seen,
        last_inventory_run

    )
    VALUES (
        %s,%s,%s,%s,%s,%s,%s,%s,%s,%s,
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
        suma = EXCLUDED.suma,
        apiversie = EXCLUDED.apiversie,

        inventory_status = 'ACTIVE',
        last_seen = NOW(),
        last_inventory_run = EXCLUDED.last_inventory_run;
    """,
    (
        server["uuid"],
        hostname,
        server["beheergroep"],
        server["beheeremail"],
        server["owner"],
        server["sl"],
        server["os"],
        server["osversie"],
        server["suma"],
        server["apiversie"],
        inventory_run_id
    ))


    cur.execute("""
    DELETE FROM server_packages
    WHERE server_id=%s
    """,
    (server["uuid"],))

    for pkg in server["extraPackages"]:

        cur.execute("""
        INSERT INTO packages(name)
        VALUES(%s)
        ON CONFLICT(name)
        DO NOTHING
        RETURNING id;
        """,
        (pkg["name"],))

        row = cur.fetchone()

        if row:
            package_id = row[0]
        else:
            cur.execute("""
            SELECT id
            FROM packages
            WHERE name=%s
            """,
            (pkg["name"],))

            package_id = cur.fetchone()[0]


        cur.execute("""
        INSERT INTO package_versions(

            package_id,
            version,
            release,
            arch

        )
        VALUES(%s,%s,%s,%s)

        ON CONFLICT(
            package_id,
            version,
            release,
            arch
        )

        DO NOTHING

        RETURNING id;
        """,
        (
            package_id,
            pkg["version"],
            pkg["release"],
            pkg["arch"]
        ))

        row = cur.fetchone()

        if row:
            package_version_id = row[0]
        else:

            cur.execute("""
            SELECT id

            FROM package_versions

            WHERE

                package_id=%s
                AND version=%s
                AND release=%s
                AND arch=%s
            """,
            (
                package_id,
                pkg["version"],
                pkg["release"],
                pkg["arch"]
            ))

            package_version_id = cur.fetchone()[0]

        #
        # Link server/package
        #

        cur.execute("""
        INSERT INTO server_packages(

            server_id,
            package_version_id,
            install_time

        )

        VALUES(%s,%s,%s)

        ON CONFLICT(
            server_id,
            package_version_id
        )

        DO UPDATE SET

            install_time = EXCLUDED.install_time;
        """,
        (
            server["uuid"],
            package_version_id,
            parse_install_time(pkg["installtime"])
        ))


cur.execute("""
UPDATE servers

SET inventory_status='MISSING'

WHERE

    inventory_status='ACTIVE'

    AND last_inventory_run <> %s;
""",
(inventory_run_id,))


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
