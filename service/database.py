"""SQLite persistence.

A single table is enough: one row per submission, the images themselves stay on disk.
Connections are opened per operation rather than shared, which keeps the module safe to
call from the request handlers and from the background worker thread alike.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

PENDING, PROCESSING, ACCEPTED, REJECTED, ERROR = (
    "pending", "processing", "accepted", "rejected", "error",
)
OPEN_STATUSES = (PROCESSING, PENDING)

SCHEMA = """
CREATE TABLE IF NOT EXISTS submissions (
    id              TEXT PRIMARY KEY,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    source_ref      TEXT NOT NULL DEFAULT '',
    customer        TEXT NOT NULL DEFAULT '{}',
    photo_report    TEXT NOT NULL DEFAULT '{}',
    signature_report TEXT NOT NULL DEFAULT '{}',
    photo_score     INTEGER NOT NULL DEFAULT 0,
    signature_score INTEGER NOT NULL DEFAULT 0,
    reviewer        TEXT NOT NULL DEFAULT '',
    reviewer_note   TEXT NOT NULL DEFAULT '',
    decided_at      TEXT NOT NULL DEFAULT '',
    forward_status  TEXT NOT NULL DEFAULT '',
    error           TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS submissions_status ON submissions(status, created_at);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        # WAL lets the dashboard read while a background worker writes its results.
        connection.execute("PRAGMA journal_mode=WAL")
        yield connection
        connection.commit()
    finally:
        connection.close()


def init(path: Path) -> None:
    with connect(path) as connection:
        connection.executescript(SCHEMA)


def create(path: Path, submission_id: str, source_ref: str, customer: dict) -> None:
    stamp = now()
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO submissions (id, created_at, updated_at, status, source_ref, customer)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (submission_id, stamp, stamp, PROCESSING, source_ref, json.dumps(customer, ensure_ascii=False)),
        )


def update(path: Path, submission_id: str, **fields) -> None:
    if not fields:
        return
    fields["updated_at"] = now()
    assignments = ", ".join(f"{name} = ?" for name in fields)
    with connect(path) as connection:
        connection.execute(
            f"UPDATE submissions SET {assignments} WHERE id = ?",
            (*fields.values(), submission_id),
        )


def get(path: Path, submission_id: str) -> dict | None:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    return _decode(row) if row else None


def listing(path: Path, statuses: tuple[str, ...] = OPEN_STATUSES, limit: int = 200) -> list[dict]:
    placeholders = ",".join("?" * len(statuses))
    with connect(path) as connection:
        rows = connection.execute(
            f"SELECT * FROM submissions WHERE status IN ({placeholders})"
            " ORDER BY datetime(created_at) DESC LIMIT ?",
            (*statuses, limit),
        ).fetchall()
    return [_decode(row) for row in rows]


def counts(path: Path) -> dict[str, int]:
    with connect(path) as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS total FROM submissions GROUP BY status").fetchall()
    return {row["status"]: row["total"] for row in rows}


def expired(path: Path, days: int) -> list[str]:
    """Ids of decided submissions past the retention window."""
    if days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id FROM submissions WHERE status IN (?, ?, ?) AND datetime(updated_at) < datetime(?)",
            (ACCEPTED, REJECTED, ERROR, cutoff),
        ).fetchall()
    return [row["id"] for row in rows]


def delete(path: Path, submission_id: str) -> None:
    with connect(path) as connection:
        connection.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))


def _decode(row: sqlite3.Row) -> dict:
    record = dict(row)
    for column in ("customer", "photo_report", "signature_report"):
        try:
            record[column] = json.loads(record[column] or "{}")
        except json.JSONDecodeError:
            record[column] = {}
    return record
