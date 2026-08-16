# packdash — package tracker

Tracks "extra" (non-SUMA-channel) packages per server and shows version
drift: servers running an older build of a package than the newest one
seen on the same OS.

## Components

- `suma.py` — collects the inventory from PuppetDB + one or more SUSE Manager
  endpoints and writes `xtra.json`
- `sumaclient.py` — shared SUSE Manager XML-RPC connection/retry helpers,
  used by both `suma.py` and `advisories.py`
- `setup.sql` — database schema + indexes (PostgreSQL, database `extrap`, role `testuser`)
- `migrate_drift.sql` — one-time migration for databases created before drift materialization
- `migrate_drift_levels.sql` — one-time migration for per-OS-release drift levels
- `migrate_advisories.sql` — one-time migration for the SUMA package_id
  passthrough + security advisory tables
- `oslevel.py` — normalizes (os, osversie) to the drift level (RedHat/SUSE major, Ubuntu major.minor)
- `import.py` — imports `xtra.json` (SUSE Manager + PuppetDB export) into the database
- `drift.py` — materializes drift after each import (also runnable standalone)
- `advisories.py` — fetches security advisory/CVE data for the fleet's
  newest drifting package versions (run after `import.py`)
- `app.py` / `db.py` / `rpmver.py` — Flask API (read-only) with pure-Python rpm version comparison
- `static/` — vanilla HTML/JS/CSS single-page frontend (no external libraries)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install flask psycopg2-binary

psql -d postgres -c "CREATE ROLE testuser LOGIN;" -c "CREATE DATABASE extrap OWNER testuser;"
psql -U testuser -d extrap -f setup.sql

.venv/bin/python import.py     # loads xtra.json (or: import.py <file>)
.venv/bin/python app.py        # serves http://localhost:8000
```

Database connection settings for the app, importer and drift scripts all
come from the standard `PGHOST`, `PGDATABASE`, `PGUSER`, `PGPASSWORD`
environment variables (defaults: localhost / extrap / testuser).

The importer is built for full `listInstalledPackages` inventories: it
preloads the package/version dimension tables, bulk-inserts new ones, and
COPYs the server/package links through a staging table — ~3M package
entries import in a few minutes.

## Upgrading an existing database

Databases created before drift materialization need the migration once:

```sh
psql -U testuser -d extrap -f migrate_drift.sql
psql -U testuser -d extrap -f migrate_drift_levels.sql
psql -U testuser -d extrap -f migrate_advisories.sql
python import.py      # or: python drift.py (rebuild without importing)
```

## Collecting the inventory (suma.py)

`suma.py` reads its configuration from `.env` (never committed). Multiple
SUSE Manager endpoints — e.g. during a migration — are queried in one run:

```
SUMA_SOURCES=suma4,suma5
SUMA4_URL=https://suma4.example.com/rpc/api
SUMA5_URL=https://suma5.example.com/rpc/api
SUMA_USER=...          # shared; override per endpoint with SUMA4_USER etc.
SUMA_KEY=...
OUTPUT_FILE=xtra.json  # default
```

A server registered in more than one SUMA resolves to the registration
with the newest `last_checkin` (ties go to the last-listed source). If any
configured endpoint is unreachable the run aborts, so a half-blind run
never reaches `xtra.json`. Without `SUMA_SOURCES` the old single
`SUMA_URL` behaviour applies.

Each installed package entry carries a `package_id` field — the SUMA
channel package id (unrelated to this app's own `packages.id`), or `-1`
when the package is installed but not available in any subscribed
channel. `import.py` stores it per install as `server_packages.suma_package_id`
(mapping `-1`/absent to `NULL`), since two different SUMA instances may
report different channel ids for what this app considers the same
package version.

## Security advisories (advisories.py)

After `import.py` has materialized drift, run `advisories.py` to fetch
security advisory (errata) and CVE data for the newest version of every
*drifting* package (`package_drift.version_count > 1` — a package the
whole fleet already agrees on has nobody left to convince):

```sh
python advisories.py            # fetch + store
python advisories.py --dry-run  # fetch and print, write nothing — use this
                                 # first to sanity-check the output (see caveat below)
python advisories.py --refresh  # re-check versions already checked
```

For each target version it resolves a channel package id + SUMA source
(any server that reported that version with a `suma_package_id`;
preferring the migration target when more than one source has it), calls
`packages.listProvidingErrata`, then `errata.getDetails` + `errata.listCves`
per distinct advisory found (deduplicated once per advisory name, however
many package versions reference it). A version's identity never changes,
so once checked it is not re-checked automatically — `package_version_advisory_check`
records the check even when no advisory was found, so a clean version
isn't re-queried on every run.

A failure fetching one version or advisory is logged and skipped, not
fatal — this script should never block using freshly imported inventory
data over a flaky SUMA call. It is a separate step from `import.py`
(not invoked automatically) for the same reason: live network calls
don't belong inside a bulk-import transaction.

**Caveat:** the exact struct field names read from `listProvidingErrata`
/ `getDetails` are taken from the documented Spacewalk/SUSE Manager
XML-RPC API and defended with fallbacks, but have not been verified
against a live SUSE Manager response. SUSE advisories also don't always
carry a clean severity field the way Red Hat's do — `severity` is a
best-effort guess from the synopsis text (`advisories.py`'s
`guess_severity`) and may come back `null`. Run with `--dry-run` first
and check the printed output before trusting it in production.

## Notes

- Drift is computed per `(package, OS, OS release)` level over ACTIVE
  servers only, so builds from different distributions or major releases
  (RedHat 7/8/9, Ubuntu 18.04–24.04, SLES 12/15) are never compared. It is
  materialized at import time (`package_drift` + `server_packages.is_latest`),
  so API requests never scan the full inventory; list endpoints paginate
  (`limit`/`offset`, default 50, max 200) and return `{total, items}`.
- Version ordering uses the rpmvercmp algorithm (`rpmver.py`), so
  `6.0.45 > 6.0.9` and `1.2~rc1 < 1.2`.
- Servers with `suma: false` or an empty uuid are skipped by `import.py`
  and do not appear in the tracker.
