"""SQLite persistence.

One row per submission plus an append-only event log; the images themselves stay on
disk.  Connections are opened per operation rather than shared, which keeps the module
safe to call from the request handlers and from the processing workers alike.

Schema changes go through numbered migrations keyed on `PRAGMA user_version`, so the
production file is upgraded in place at startup instead of being recreated.

Every status change that two actors could race on — a reviewer and the WordPress plugin
accepting the same file, a reject landing while the photo is still being processed — is
a conditional UPDATE whose row count says who won.  Reading the status first and writing
it afterwards is exactly the window in which a dossier gets transmitted twice.  The same
goes for the operation lock (`locked_since`), held while a transmission or a
recalculation is touching a dossier's images.
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
DECIDED_STATUSES = (ACCEPTED, REJECTED, ERROR)

# Columns update() may write.  The names are interpolated into SQL, so they are checked
# against this list rather than trusted because every caller happens to be internal.
COLUMNS = frozenset({
    "status", "source_ref", "customer", "photo_report", "signature_report",
    "photo_score", "signature_score", "reviewer", "reviewer_note", "decided_at",
    "forward_status", "error", "fingerprint", "locked_since", "lock_operation",
})

MIGRATIONS = (
    # 1 — the original single table.  IF NOT EXISTS keeps it a no-op on the files that
    # predate migrations, which carry this exact table at user_version 0.
    """
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
    """,
    # 2 — duplicate detection, the per-dossier operation lock and the audit trail.
    """
    ALTER TABLE submissions ADD COLUMN fingerprint TEXT NOT NULL DEFAULT '';
    ALTER TABLE submissions ADD COLUMN locked_since TEXT NOT NULL DEFAULT '';
    ALTER TABLE submissions ADD COLUMN lock_operation TEXT NOT NULL DEFAULT '';
    CREATE INDEX IF NOT EXISTS submissions_fingerprint ON submissions(fingerprint);
    CREATE TABLE IF NOT EXISTS events (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        submission_id TEXT NOT NULL,
        at            TEXT NOT NULL,
        actor         TEXT NOT NULL DEFAULT '',
        action        TEXT NOT NULL,
        detail        TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS events_submission ON events(submission_id, id);
    """,
)
SCHEMA_VERSION = len(MIGRATIONS)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=15)
    connection.row_factory = sqlite3.Row
    try:
        yield connection
        connection.commit()
    finally:
        connection.close()


def init(path: Path) -> int:
    """Bring the file up to SCHEMA_VERSION; returns the version it started from."""
    with connect(path) as connection:
        # WAL lets the dashboard read while a worker writes its results.  The mode is
        # persistent, so it is set once here rather than on every connection.
        connection.execute("PRAGMA journal_mode=WAL")
        start = connection.execute("PRAGMA user_version").fetchone()[0]
        for version, script in enumerate(MIGRATIONS, start=1):
            if version > start:
                # executescript() commits first, so each migration and its version bump
                # land together or not at all.
                connection.executescript(f"BEGIN;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
    return start


# ── Submissions ─────────────────────────────────────────────────────────────────────
def create(path: Path, submission_id: str, source_ref: str, customer: dict, fingerprint: str = "") -> None:
    stamp = now()
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO submissions (id, created_at, updated_at, status, source_ref, customer, fingerprint)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (submission_id, stamp, stamp, PROCESSING, source_ref,
             json.dumps(customer, ensure_ascii=False), fingerprint),
        )


def _assignments(fields: dict) -> tuple[str, tuple]:
    unknown = set(fields) - COLUMNS
    if unknown:
        raise ValueError(f"unknown submission column(s): {', '.join(sorted(unknown))}")
    fields["updated_at"] = now()
    return ", ".join(f"{name} = ?" for name in fields), tuple(fields.values())


def update(path: Path, submission_id: str, **fields) -> None:
    if not fields:
        return
    assignments, values = _assignments(fields)
    with connect(path) as connection:
        connection.execute(f"UPDATE submissions SET {assignments} WHERE id = ?", (*values, submission_id))


def transition(
    path: Path, submission_id: str, from_statuses: tuple[str, ...], require_unlocked: bool = False, **fields,
) -> bool:
    """Apply `fields` only while the row is still in one of `from_statuses`.

    Returns False when another actor moved the row first, which the caller reports
    instead of overwriting a decision it never saw.  With `require_unlocked` it also
    refuses while a transmission or a recalculation holds the dossier.
    """
    assignments, values = _assignments(fields)
    placeholders = ",".join("?" * len(from_statuses))
    lock = " AND locked_since = ''" if require_unlocked else ""
    with connect(path) as connection:
        cursor = connection.execute(
            f"UPDATE submissions SET {assignments} WHERE id = ? AND status IN ({placeholders}){lock}",
            (*values, submission_id, *from_statuses),
        )
        return cursor.rowcount == 1


def claim(path: Path, submission_id: str, operation: str) -> bool:
    """Take the per-dossier operation lock: True for exactly one concurrent caller.

    Transmission, re-crop and rotation all rewrite or read the prepared images; holding
    the lock means an accept can never ship a photo that a re-crop is replacing.
    """
    stamp = now()
    with connect(path) as connection:
        cursor = connection.execute(
            "UPDATE submissions SET locked_since = ?, lock_operation = ?, updated_at = ?"
            " WHERE id = ? AND status = ? AND locked_since = ''",
            (stamp, operation, stamp, submission_id, PENDING),
        )
        return cursor.rowcount == 1


def release(path: Path, submission_id: str) -> None:
    with connect(path) as connection:
        connection.execute(
            "UPDATE submissions SET locked_since = '', lock_operation = '' WHERE id = ?", (submission_id,)
        )


def release_interrupted(path: Path) -> list[tuple[str, str]]:
    """Clear locks left by a process that died mid-operation; returns (id, operation).

    Called at startup, when nothing can be in flight.  For an interrupted transmission,
    whether Make received the call is unknown, so the dossier goes back to the queue
    with that said plainly rather than being marked either way.
    """
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT id, lock_operation FROM submissions WHERE locked_since != ''"
        ).fetchall()
        stamp = now()
        for row in rows:
            fields = {"locked_since": "", "lock_operation": "", "updated_at": stamp}
            if row["lock_operation"] == "transmission":
                fields["forward_status"] = (
                    "transmission interrompue par un redémarrage : vérifiez la réception côté Make avant de renvoyer"
                )
            assignments = ", ".join(f"{name} = ?" for name in fields)
            connection.execute(f"UPDATE submissions SET {assignments} WHERE id = ?", (*fields.values(), row["id"]))
    return [(row["id"], row["lock_operation"]) for row in rows]


def get(path: Path, submission_id: str) -> dict | None:
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM submissions WHERE id = ?", (submission_id,)).fetchone()
    return _decode(row) if row else None


def find_duplicate(path: Path, fingerprint: str) -> dict | None:
    """The live submission already holding these exact files, if any.

    Rejected and failed dossiers do not count: sending the same files again after a
    refusal is a deliberate new attempt, not a retry of the first call.
    """
    if not fingerprint:
        return None
    with connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM submissions WHERE fingerprint = ? AND status NOT IN (?, ?)"
            " ORDER BY created_at DESC LIMIT 1",
            (fingerprint, REJECTED, ERROR),
        ).fetchone()
    return _decode(row) if row else None


def with_status(path: Path, status: str) -> list[str]:
    with connect(path) as connection:
        rows = connection.execute("SELECT id FROM submissions WHERE status = ?", (status,)).fetchall()
    return [row["id"] for row in rows]


def listing(
    path: Path, statuses: tuple[str, ...] = OPEN_STATUSES, limit: int = 200, query: str = ""
) -> list[dict]:
    """Return the newest dossiers for a queue view, optionally narrowed by a text search."""
    placeholders = ",".join("?" * len(statuses))
    filters = "status IN (" + placeholders + ")"
    params: list[object] = [*statuses]
    if query.strip():
        # Customer is JSON in SQLite. Searching it covers names, emails, and source
        # system customer data without introducing a dynamic SQL fragment.
        needle = f"%{query.strip()}%"
        filters += " AND (id LIKE ? OR source_ref LIKE ? OR customer LIKE ?)"
        params.extend((needle, needle, needle))
    with connect(path) as connection:
        # Timestamps are uniform ISO-8601 UTC strings, so they sort as text and the
        # (status, created_at) index serves the ORDER BY directly.
        rows = connection.execute(
            f"SELECT * FROM submissions WHERE {filters} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
    return [_decode(row) for row in rows]


def counts(path: Path) -> dict[str, int]:
    with connect(path) as connection:
        rows = connection.execute("SELECT status, COUNT(*) AS total FROM submissions GROUP BY status").fetchall()
    return {row["status"]: row["total"] for row in rows}


def ids(path: Path) -> set[str]:
    with connect(path) as connection:
        return {row["id"] for row in connection.execute("SELECT id FROM submissions").fetchall()}


def expired(path: Path, days: int, statuses: tuple[str, ...] = DECIDED_STATUSES) -> list[str]:
    """Ids in `statuses` untouched for longer than the retention window."""
    if days <= 0:
        return []
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
    placeholders = ",".join("?" * len(statuses))
    with connect(path) as connection:
        rows = connection.execute(
            f"SELECT id FROM submissions WHERE status IN ({placeholders}) AND updated_at < ?",
            (*statuses, cutoff),
        ).fetchall()
    return [row["id"] for row in rows]


def delete(path: Path, submission_id: str) -> None:
    """Remove the dossier and its history: retention applies to the audit trail too."""
    with connect(path) as connection:
        connection.execute("DELETE FROM events WHERE submission_id = ?", (submission_id,))
        connection.execute("DELETE FROM submissions WHERE id = ?", (submission_id,))


# ── Audit trail ─────────────────────────────────────────────────────────────────────
def log_event(path: Path, submission_id: str, action: str, actor: str = "", **detail) -> None:
    with connect(path) as connection:
        connection.execute(
            "INSERT INTO events (submission_id, at, actor, action, detail) VALUES (?, ?, ?, ?, ?)",
            (submission_id, now(), actor, action, json.dumps(detail, ensure_ascii=False, default=str)),
        )


def events(path: Path, submission_id: str) -> list[dict]:
    with connect(path) as connection:
        rows = connection.execute(
            "SELECT at, actor, action, detail FROM events WHERE submission_id = ? ORDER BY id",
            (submission_id,),
        ).fetchall()
    history = []
    for row in rows:
        entry = dict(row)
        try:
            entry["detail"] = json.loads(entry["detail"] or "{}")
        except json.JSONDecodeError:
            entry["detail"] = {}
        history.append(entry)
    return history


def _decode(row: sqlite3.Row) -> dict:
    record = dict(row)
    for column in ("customer", "photo_report", "signature_report"):
        try:
            record[column] = json.loads(record[column] or "{}")
        except json.JSONDecodeError:
            record[column] = {}
        # Templates call .get() on all three: a single row holding a JSON string or list
        # here used to take the whole dashboard down with it.
        if not isinstance(record[column], dict):
            record[column] = {"raw": record[column]} if column == "customer" else {}
    return record
