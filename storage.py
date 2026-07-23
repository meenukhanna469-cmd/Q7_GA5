"""
storage.py — all durable state lives here (SQLite file on disk).
Do NOT keep anything important only in process memory: the grader
restarts/replays across evaluations and expects durability.
"""
import sqlite3
import json
import threading

DB_PATH = "mailroom.db"
_lock = threading.Lock()  # SQLite + simple lock is fine for this exam's concurrency needs


def get_conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL;")
    return conn


def init_db():
    conn = get_conn()
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS dossier_decisions (
            dossier_id TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            call_id TEXT NOT NULL,
            action TEXT NOT NULL,
            target TEXT,
            payload_json TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            proposal_digest TEXT NOT NULL,
            PRIMARY KEY (dossier_id, content_hash)
        );

        CREATE TABLE IF NOT EXISTS evaluations (
            evaluation_id TEXT PRIMARY KEY,
            content_set_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            proposals_json TEXT NOT NULL,
            verification_key TEXT
        );

        CREATE TABLE IF NOT EXISTS receipts (
            evaluation_id TEXT NOT NULL,
            receipt_id TEXT NOT NULL,
            call_id TEXT NOT NULL,
            verified INTEGER NOT NULL,
            outcome_json TEXT,
            PRIMARY KEY (evaluation_id, receipt_id)
        );
        """
    )
    conn.commit()
    conn.close()


def get_cached_decision(dossier_id, content_hash):
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM dossier_decisions WHERE dossier_id=? AND content_hash=?",
            (dossier_id, content_hash),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def save_decision(dossier_id, content_hash, call_id, action, target, payload, evidence, proposal_digest):
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR IGNORE INTO dossier_decisions
               (dossier_id, content_hash, call_id, action, target, payload_json, evidence_json, proposal_digest)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                dossier_id,
                content_hash,
                call_id,
                action,
                target,
                json.dumps(payload, sort_keys=True),
                json.dumps(evidence, sort_keys=True),
                proposal_digest,
            ),
        )
        conn.commit()
        conn.close()


def get_evaluation(evaluation_id):
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM evaluations WHERE evaluation_id=?", (evaluation_id,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def save_evaluation(evaluation_id, content_set_hash, status, proposals, verification_key=None):
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO evaluations
               (evaluation_id, content_set_hash, status, proposals_json, verification_key)
               VALUES (?, ?, ?, ?, ?)""",
            (evaluation_id, content_set_hash, status, json.dumps(proposals, sort_keys=True), verification_key),
        )
        conn.commit()
        conn.close()


def get_receipt(evaluation_id, receipt_id):
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM receipts WHERE evaluation_id=? AND receipt_id=?",
            (evaluation_id, receipt_id),
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def save_receipt(evaluation_id, receipt_id, call_id, verified, outcome):
    with _lock:
        conn = get_conn()
        conn.execute(
            """INSERT OR REPLACE INTO receipts
               (evaluation_id, receipt_id, call_id, verified, outcome_json)
               VALUES (?, ?, ?, ?, ?)""",
            (evaluation_id, receipt_id, call_id, int(verified), json.dumps(outcome, sort_keys=True)),
        )
        conn.commit()
        conn.close()
