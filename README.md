# packdash — package tracker

Tracks "extra" (non-SUMA-channel) packages per server and shows version
drift: servers running an older build of a package than the newest one
seen on the same OS.

## Components

- `suma.py` — collects the inventory from PuppetDB + one or more SUSE Manager
  endpoints and writes `xtra.json`
- `setup.sql` — database schema + indexes (PostgreSQL, database `extrap`, role `testuser`)
- `migrate_drift.sql` — one-time migration for databases created before drift materialization
- `migrate_drift_levels.sql` — one-time migration for per-OS-release drift levels
- `oslevel.py` — normalizes (os, osversie) to the drift level (RedHat/SUSE major, Ubuntu major.minor)
- `import.py` — imports `xtra.json` (SUSE Manager + PuppetDB export) into the database
- `drift.py` — materializes drift after each import (also runnable standalone)
- `app.py` / `db.py` / `rpmver.py` — Flask API (read-only) with pure-Python rpm version comparison
- `static/` — vanilla HTML/JS/CSS single-page frontend (no external libraries)

## Setup

```sh
python3 -m venv .venv
.venv/bin/pip install flask psycopg2-binary

psql -d postgres -c "CREATE ROLE testuser LOGIN;" -c "CREATE DATABASE extrap OWNER testuser;"
psql -U testuser -d extrap -f setup.sql

.venv/bin/python import.py     # loads xtra.json
.venv/bin/python app.py        # serves http://localhost:8000
```

Database connection settings for the app come from the standard `PGHOST`,
`PGDATABASE`, `PGUSER`, `PGPASSWORD` environment variables (defaults match
`import.py`: localhost / extrap / testuser).

## Upgrading an existing database

Databases created before drift materialization need the migration once:

```sh
psql -U testuser -d extrap -f migrate_drift.sql
psql -U testuser -d extrap -f migrate_drift_levels.sql
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
