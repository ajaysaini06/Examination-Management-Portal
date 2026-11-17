# database.py - connection handling, table creation and admin seeding
import os
import sqlite3

from flask import current_app, g
from werkzeug.security import generate_password_hash


def get_db():
    """Return one SQLite connection per request (stored on flask.g)."""
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row          # rows behave like dicts
        g.db.execute("PRAGMA foreign_keys = ON")  # SQLite needs this on every connection
    return g.db


def close_db(e=None):
    """Close the connection at the end of the request."""
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db(app):
    """Create the database from schema.sql if needed and seed the admin."""
    db_path = app.config["DATABASE"]
    os.makedirs(os.path.dirname(db_path), exist_ok=True)

    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys = ON")
    schema_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "schema.sql")
    with open(schema_file, encoding="utf-8") as f:
        conn.executescript(f.read())

    # Migration: older databases (phase 1-2) lack the status_override column
    cols = [r[1] for r in conn.execute("PRAGMA table_info(examinations)")]
    if "status_override" not in cols:
        conn.execute("ALTER TABLE examinations ADD COLUMN status_override INTEGER NOT NULL DEFAULT 0")

    # Seed the admin only if no admin exists yet
    admin = conn.execute("SELECT id FROM users WHERE role = 'admin'").fetchone()
    if admin is None:
        conn.execute(
            "INSERT INTO users (name, email, password_hash, role, status) "
            "VALUES (?, ?, ?, 'admin', 'Active')",
            (
                app.config["ADMIN_NAME"],
                app.config["ADMIN_EMAIL"],
                generate_password_hash(app.config["ADMIN_PASSWORD"]),
            ),
        )
    conn.commit()
    conn.close()
