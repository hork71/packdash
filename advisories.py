"""Fetch security advisory (errata + CVE) data for the fleet's newest
drifting package versions, so the frontend can show "upgrading fixes
these CVEs" next to each drift row — the ammunition for making the
case that an upgrade is imperative.

Runs as a separate pipeline step after import.py (which materializes
drift):

    suma.py -> xtra.json -> import.py -> advisories.py

Network calls to SUMA are live and can be slow or flaky, so a failure
on one package_version is logged and skipped rather than aborting the
whole run — a bad advisory fetch should never block using the freshly
imported inventory data. Scope is drifting groups only (package_drift
rows where version_count > 1): a package everyone already has the
newest version of has nobody left to convince.

Caching: a package_version's identity never changes, so once it has
been checked (recorded in package_version_advisory_check) it is not
re-checked automatically. Pass --refresh to force re-checking
everything, e.g. if SUSE later fills in CVE data that was initially
empty.

FIELD-NAME CAVEAT: the exact struct fields read from
packages.listProvidingErrata / errata.getDetails / errata.listCves are
taken from the documented Spacewalk/SUSE Manager XML-RPC API and
defended with .get(..., fallback) where the field name might vary by
version. This has NOT been verified against a live SUSE Manager
response. Run with --dry-run first and check the printed output
(especially advisory_type/severity — SUSE advisories do not always
carry a clean severity field the way Red Hat's do) before trusting it.
"""

import argparse
import os
import re
from concurrent.futures import ThreadPoolExecutor

import psycopg2
from psycopg2.extras import execute_values

import sumaclient

WORKERS = 10

_SEVERITY_RE = re.compile(r'\b(critical|important|moderate|low)\b', re.IGNORECASE)


def guess_severity(synopsis):
    """SUSE errata don't carry a clean severity enum the way Red Hat's
    do; this is a best-effort heuristic scanning the synopsis text.
    Verify against real advisory text — may need adjusting."""
    if not synopsis:
        return None
    m = _SEVERITY_RE.search(synopsis)
    return m.group(1).capitalize() if m else None


def target_versions(cur, refresh):
    """Distinct latest_version_id of drifting package_drift groups that
    have not been checked yet (or all of them, with --refresh)."""
    already_checked = (
        "" if refresh else
        "AND pd.latest_version_id NOT IN "
        "(SELECT package_version_id FROM package_version_advisory_check)"
    )
    cur.execute(f"""
        SELECT DISTINCT pd.latest_version_id
        FROM package_drift pd
        WHERE pd.version_count > 1
        {already_checked}
    """)
    return [r[0] for r in cur.fetchall()]


def resolve_source(cur, package_version_id, sources_by_apiversie):
    """Find a channel package id + source to query for this version.

    Any ACTIVE-or-not server carrying the version with a known
    suma_package_id will do (the version's identity is what we're
    asking about, not any one server's current status). If more than
    one source has reported it, prefer the migration target — same
    tie-break as suma.py's build_suma_lookup.
    """
    cur.execute("""
        SELECT DISTINCT sp.suma_package_id, s.apiversie
        FROM server_packages sp
        JOIN servers s ON s.id = sp.server_id
        WHERE sp.package_version_id = %s
          AND sp.suma_package_id IS NOT NULL
    """, (package_version_id,))

    candidates = [
        (suma_pid, sources_by_apiversie[apiversie])
        for suma_pid, apiversie in cur.fetchall()
        if apiversie in sources_by_apiversie
    ]
    if not candidates:
        return None, None

    candidates.sort(key=lambda c: c[1]['rank'], reverse=True)
    return candidates[0]


def fetch_advisories_for_target(source, suma_package_id):
    """packages.listProvidingErrata -> list of advisory_name strings."""
    def call(client):
        return client.packages.listProvidingErrata(source['session'], suma_package_id)

    errata_list = sumaclient.call_with_retry(source, call)
    names = []
    for erratum in errata_list or []:
        name = erratum.get('advisory_name') or erratum.get('advisory')
        if name:
            names.append(name)
    return names


def fetch_errata_details(source, advisory_name):
    """errata.getDetails + errata.listCves, combined into one dict."""
    def get_details(client):
        return client.errata.getDetails(source['session'], advisory_name)

    def list_cves(client):
        return client.errata.listCves(source['session'], advisory_name)

    details = sumaclient.call_with_retry(source, get_details) or {}
    cves = sumaclient.call_with_retry(source, list_cves) or []

    synopsis = details.get('synopsis') or details.get('advisory_synopsis')
    issue_date = sumaclient.parse_xmlrpc_datetime(
        details.get('issue_date') or details.get('date'))

    return {
        'advisory_name': advisory_name,
        'advisory_id': str(details.get('advisory_id') or details.get('id') or '') or None,
        'advisory_type': details.get('advisory_type') or details.get('type'),
        'synopsis': synopsis,
        'severity': guess_severity(synopsis),
        'issue_date': issue_date,
        'suma_source': source['name'],
        'cves': list(cves),
    }


def run(cur, sources, refresh, dry_run):
    targets = target_versions(cur, refresh)
    if not targets:
        print("No new drifting versions to check for advisories.")
        return

    sources_by_apiversie = {
        s['apiversie']: {**s, 'rank': i} for i, s in enumerate(sources)
    }

    # Phase 1 (sequential, local DB reads only): resolve a channel
    # package id + source per target, and ask what advisories provide
    # that exact package build.
    checked_rows = []              # (package_version_id, suma_pid, source_name)
    version_advisory_names = {}    # package_version_id -> [advisory_name, ...]
    to_fetch = {}                  # advisory_name -> source (first source seen wins;
                                    # advisory content doesn't depend on which mirror)

    for package_version_id in targets:
        suma_pid, source = resolve_source(cur, package_version_id, sources_by_apiversie)
        if not source:
            checked_rows.append((package_version_id, None, None))
            continue

        try:
            names = fetch_advisories_for_target(source, suma_pid)
        except Exception as e:
            print(f"WARNING: listProvidingErrata failed for package_version "
                  f"{package_version_id} (suma_package_id {suma_pid} via "
                  f"{source['name']}): {e}")
            continue  # not marked checked - retried next run

        checked_rows.append((package_version_id, suma_pid, source['name']))
        version_advisory_names[package_version_id] = names
        for name in names:
            to_fetch.setdefault(name, source)

    # Phase 2 (parallel, network-bound): fetch full details once per
    # distinct advisory name, regardless of how many targets share it.
    errata_rows = {}

    def fetch_one(item):
        name, source = item
        try:
            return fetch_errata_details(source, name)
        except Exception as e:
            print(f"WARNING: could not fetch advisory {name}: {e}")
            return None

    if to_fetch:
        with ThreadPoolExecutor(max_workers=WORKERS) as pool:
            for result in pool.map(fetch_one, to_fetch.items()):
                if result:
                    errata_rows[result['advisory_name']] = result

    link_rows = [
        (pvid, name)
        for pvid, names in version_advisory_names.items()
        for name in names
        if name in errata_rows
    ]

    if dry_run:
        print(f"[dry-run] {len(targets)} version(s) targeted, "
              f"{len(errata_rows)} advisory(ies) fetched, "
              f"{len(link_rows)} package/advisory links; nothing written.")
        for r in errata_rows.values():
            print(f"  {r['advisory_name']}  type={r['advisory_type']!r} "
                  f"severity={r['severity']!r} cves={r['cves']} "
                  f"source={r['suma_source']}")
            print(f"    synopsis: {r['synopsis']!r}")
        return

    # Phase 3 (sequential, local DB writes only): persist.
    if errata_rows:
        execute_values(cur, """
            INSERT INTO errata(advisory_name, advisory_id, advisory_type,
                                synopsis, severity, issue_date, suma_source)
            VALUES %s
            ON CONFLICT (advisory_name) DO UPDATE SET
                advisory_id = EXCLUDED.advisory_id,
                advisory_type = EXCLUDED.advisory_type,
                synopsis = EXCLUDED.synopsis,
                severity = EXCLUDED.severity,
                issue_date = EXCLUDED.issue_date,
                suma_source = EXCLUDED.suma_source,
                fetched_at = NOW()
        """, [
            (r['advisory_name'], r['advisory_id'], r['advisory_type'],
             r['synopsis'], r['severity'], r['issue_date'], r['suma_source'])
            for r in errata_rows.values()
        ])

        cve_rows = [
            (r['advisory_name'], cve)
            for r in errata_rows.values() for cve in r['cves']
        ]
        if cve_rows:
            execute_values(cur, """
                INSERT INTO errata_cves(advisory_name, cve)
                VALUES %s
                ON CONFLICT (advisory_name, cve) DO NOTHING
            """, cve_rows)

    if link_rows:
        execute_values(cur, """
            INSERT INTO package_version_errata(package_version_id, advisory_name)
            VALUES %s
            ON CONFLICT (package_version_id, advisory_name) DO NOTHING
        """, link_rows)

    if checked_rows:
        execute_values(cur, """
            INSERT INTO package_version_advisory_check(
                package_version_id, suma_package_id, suma_source
            )
            VALUES %s
            ON CONFLICT (package_version_id) DO UPDATE SET
                checked_at = NOW(),
                suma_package_id = EXCLUDED.suma_package_id,
                suma_source = EXCLUDED.suma_source
        """, checked_rows)

    print(f"Advisories: {len(targets)} version(s) checked, "
          f"{len(errata_rows)} advisory(ies) fetched, "
          f"{len(link_rows)} package/advisory links")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--refresh", action="store_true",
        help="Re-check every drifting version, including ones already checked.")
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Fetch and print what would be stored, without writing to the database.")
    args = parser.parse_args()

    sources = sumaclient.load_suma_sources()
    for source in sources:
        client, session_key = sumaclient.connectSuma(source)
        source['client'] = client
        source['session'] = session_key
        source['apiversie'] = str(client.api.getVersion())

    conn = psycopg2.connect(
        host=os.environ.get("PGHOST", "localhost"),
        dbname=os.environ.get("PGDATABASE", "extrap"),
        user=os.environ.get("PGUSER", "testuser"),
        password=os.environ.get("PGPASSWORD"),
    )
    conn.autocommit = False
    cur = conn.cursor()

    try:
        run(cur, sources, args.refresh, args.dry_run)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        cur.close()
        conn.close()
        for source in sources:
            try:
                source['client'].auth.logout(source['session'])
            except Exception:
                pass


if __name__ == "__main__":
    main()
