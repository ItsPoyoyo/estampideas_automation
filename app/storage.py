"""Additive SQLite storage for idempotency, item revisions, and the activity log."""

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from app.production_rules import ROUTES, ProductionFlow


def utc_now() -> datetime:
    return datetime.now(UTC)


def open_database(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path, timeout=10)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS orders (
            order_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, payload TEXT, revision INTEGER NOT NULL DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS order_batches (
            order_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, status TEXT NOT NULL,
            result TEXT, payload TEXT
        );
        CREATE TABLE IF NOT EXISTS order_events (
            id INTEGER PRIMARY KEY AUTOINCREMENT, order_id TEXT NOT NULL, item_id TEXT,
            kind TEXT NOT NULL, message TEXT NOT NULL, occurred_at TEXT NOT NULL,
            event_key TEXT UNIQUE
        );
        CREATE INDEX IF NOT EXISTS events_by_order ON order_events(order_id, id);
        CREATE TABLE IF NOT EXISTS item_updates (
            order_id TEXT NOT NULL, from_revision INTEGER NOT NULL,
            fingerprint TEXT NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL,
            result TEXT, PRIMARY KEY(order_id, from_revision)
        );
        CREATE TABLE IF NOT EXISTS production_flows (
            order_id TEXT PRIMARY KEY, state TEXT NOT NULL
        );
    """)
    db.execute("BEGIN IMMEDIATE")
    # Keep existing POC orders and their original fingerprints intact.
    for table, columns in {
        "orders": {"payload": "TEXT", "revision": "INTEGER NOT NULL DEFAULT 1"},
        "order_batches": {"payload": "TEXT"},
    }.items():
        existing = {column[1] for column in db.execute(f"PRAGMA table_info({table})")}
        for column, definition in columns.items():
            if column not in existing:
                db.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")
    db.commit()
    return db


def record_event(
    db, order_id: str, kind: str, message: str, *, item_id: str = "", key: str | None = None
):
    db.execute(
        "INSERT OR IGNORE INTO order_events (order_id,item_id,kind,message,occurred_at,event_key) VALUES (?,?,?,?,?,?)",
        (order_id, item_id, kind, message, utc_now().isoformat(), key),
    )


def item_result(row) -> dict:
    result = json.loads(row[0])
    result.pop("pending_update", None)
    result.pop("production", None)
    if row[1]:
        result["details"] = json.loads(row[1])
        result["revision"] = row[2]
    return result


def register_production(db, order_id: str, product_code: str):
    if product_code in ROUTES:
        db.execute(
            "INSERT OR IGNORE INTO production_flows (order_id,state) VALUES (?,?)",
            (order_id, ProductionFlow(product_code=product_code).model_dump_json()),
        )


def read_production(db, order_id: str) -> ProductionFlow | None:
    row = db.execute("SELECT state FROM production_flows WHERE order_id=?", (order_id,)).fetchone()
    return ProductionFlow.model_validate_json(row[0]) if row else None


def read_batch(db, order_id: str) -> dict | None:
    row = db.execute(
        "SELECT status,result,payload FROM order_batches WHERE order_id=?", (order_id,)
    ).fetchone()
    if not row:
        return None
    result = json.loads(row[1]) if row[1] else {"order_id": order_id, "cards": [], "total": 0}
    result["status"] = row[0]
    for index, item in enumerate(result["cards"]):
        current = db.execute(
            "SELECT result,payload,revision FROM orders WHERE order_id=? AND status='sent'",
            (item["preview"]["order_id"],),
        ).fetchone()
        if current and current[0]:
            result["cards"][index] = item_result(current)
            production = read_production(db, item["preview"]["order_id"])
            if production:
                result["cards"][index]["production"] = production.public()
            pending = db.execute(
                "SELECT payload,from_revision,status FROM item_updates WHERE order_id=? AND from_revision=? AND status IN ('unknown','pending')",
                (item["preview"]["order_id"], current[2]),
            ).fetchone()
            if pending:
                candidate = json.loads(pending[0])
                result["cards"][index]["pending_update"] = {
                    "revision": pending[1],
                    "state": pending[2],
                    "changes": {
                        key: candidate[key]
                        for key in result["cards"][index]["preview"].get("missing_keys", [])
                        if candidate.get(key) not in (None, "")
                    },
                }
    result["activities"] = [
        dict(zip(("id", "item_id", "kind", "message", "occurred_at"), event))
        for event in db.execute(
            "SELECT id,item_id,kind,message,occurred_at FROM order_events WHERE order_id=? ORDER BY id",
            (order_id,),
        )
    ]
    result["request"] = json.loads(row[2]) if row[2] else None
    return result
