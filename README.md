# packdash — package tracker

Tracks "extra" (non-SUMA-channel) packages per server and shows version
drift: servers running an older build of a package than the newest one
seen on the same OS.

## Components

- `setup.sql` — database schema + indexes (PostgreSQL, database `extrap`, role `testuser`)
- `import.py` — imports `xtra.json` (SUSE Manager + PuppetDB export) into the database
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

## Notes

- Drift is computed per `(package, OS)` group over ACTIVE servers only, so
  SLES and RedHat builds of the same package are never compared.
- Version ordering uses the rpmvercmp algorithm (`rpmver.py`), so
  `6.0.45 > 6.0.9` and `1.2~rc1 < 1.2`.
- Servers with `suma: false` or an empty uuid are skipped by `import.py`
  and do not appear in the tracker.
