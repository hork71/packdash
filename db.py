"""Per-request PostgreSQL connection for the Flask app.

Connection settings come from the standard PG* environment variables,
falling back to the same local defaults import.py uses.
"""

import os

import psycopg2
import psycopg2.extras
from flask import g


def get_conn():
    if "db_conn" not in g:
        g.db_conn = psycopg2.connect(
            host=os.environ.get("PGHOST", "localhost"),
            dbname=os.environ.get("PGDATABASE", "extrap"),
            user=os.environ.get("PGUSER", "testuser"),
            password=os.environ.get("PGPASSWORD"),
        )
    return g.db_conn


def close_conn(exc=None):
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


def query(sql, params=()):
    """Run a read-only query, return a list of dict rows."""
    with get_conn().cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        return cur.fetchall()
