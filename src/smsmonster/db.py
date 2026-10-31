"""SQLite persistence (single connection, lock-guarded; safe across worker threads)."""
from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS modems (
    id TEXT PRIMARY KEY,
    name TEXT,
    phone_number TEXT,
    iccid TEXT, imsi TEXT, model TEXT, firmware TEXT,
    first_seen TEXT, last_seen TEXT
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL DEFAULT 'sms',
    direction TEXT NOT NULL,
    modem_id TEXT,
    number TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    error TEXT,
    parts_total INTEGER DEFAULT 1,
    parts_received INTEGER DEFAULT 1,
    attempts INTEGER DEFAULT 0,
    reference INTEGER,
    created_at TEXT NOT NULL,
    received_at TEXT,
    sent_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(status);
CREATE TABLE IF NOT EXISTS sms_parts (
    modem_id TEXT, number TEXT, ref INTEGER, total INTEGER, seq INTEGER,
    text TEXT, received_at TEXT, created_at TEXT,
    PRIMARY KEY (modem_id, number, ref, seq)
);
CREATE TABLE IF NOT EXISTS ignored_devices (
    device TEXT PRIMARY KEY,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhooks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    type TEXT NOT NULL DEFAULT 'webhook',
    url TEXT NOT NULL,
    secret TEXT,
    events TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            self._migrate()

    def _migrate(self) -> None:
        cols = {r["name"] for r in self._conn.execute("PRAGMA table_info(webhooks)")}
        if "type" not in cols:
            self._conn.execute("ALTER TABLE webhooks ADD COLUMN type TEXT NOT NULL DEFAULT 'webhook'")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _q(self, sql: str, params: tuple = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, params).fetchall()]

    def _one(self, sql: str, params: tuple = ()) -> dict | None:
        rows = self._q(sql, params)
        return rows[0] if rows else None

    def _exec(self, sql: str, params: tuple = ()) -> int:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    # ---- modems ----
    def upsert_modem(self, modem_id: str, **fields) -> dict:
        ts = now_iso()
        cols = {k: v for k, v in fields.items() if k in ("iccid", "imsi", "model", "firmware") and v}
        self._exec(
            "INSERT INTO modems (id, first_seen, last_seen) VALUES (?, ?, ?) ON CONFLICT(id) DO UPDATE SET last_seen=excluded.last_seen",
            (modem_id, ts, ts),
        )
        if cols:
            sets = ", ".join(f"{k}=?" for k in cols)
            self._exec(f"UPDATE modems SET {sets} WHERE id=?", (*cols.values(), modem_id))
        return self.get_modem(modem_id) or {}

    def get_modem(self, modem_id: str) -> dict | None:
        return self._one("SELECT * FROM modems WHERE id=?", (modem_id,))

    def list_modems(self) -> list[dict]:
        return self._q("SELECT * FROM modems ORDER BY first_seen")

    def update_modem_settings(self, modem_id: str, name: str | None, phone_number: str | None) -> dict | None:
        self._exec("UPDATE modems SET name=?, phone_number=? WHERE id=?", (name, phone_number, modem_id))
        return self.get_modem(modem_id)

    # ---- ignored serial devices ----
    def list_ignored_devices(self) -> list[dict]:
        return self._q("SELECT * FROM ignored_devices ORDER BY created_at")

    def ignore_device(self, device: str) -> None:
        self._exec("INSERT OR IGNORE INTO ignored_devices (device, created_at) VALUES (?, ?)", (device, now_iso()))

    def unignore_device(self, device: str) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM ignored_devices WHERE device=?", (device,)).rowcount > 0

    # ---- messages ----
    def insert_message(self, **m) -> dict:
        m.setdefault("kind", "sms")
        m.setdefault("created_at", now_iso())
        cols = ", ".join(m)
        qs = ", ".join("?" for _ in m)
        rid = self._exec(f"INSERT INTO messages ({cols}) VALUES ({qs})", tuple(m.values()))
        return self.get_message(rid)  # type: ignore[return-value]

    def get_message(self, mid: int) -> dict | None:
        return self._one("SELECT * FROM messages WHERE id=?", (mid,))

    def update_message(self, mid: int, **fields) -> dict | None:
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE messages SET {sets} WHERE id=?", (*fields.values(), mid))
        return self.get_message(mid)

    def delete_message(self, mid: int) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM messages WHERE id=?", (mid,)).rowcount > 0

    def list_messages(self, direction: str | None = None, modem_id: str | None = None, number: str | None = None,
                      status: str | None = None, kind: str | None = None, search: str | None = None,
                      before_id: int | None = None, limit: int = 50) -> list[dict]:
        where, params = [], []
        for col, val in (("direction", direction), ("modem_id", modem_id), ("number", number), ("status", status), ("kind", kind)):
            if val:
                where.append(f"{col}=?")
                params.append(val)
        if search:
            where.append("(text LIKE ? OR number LIKE ?)")
            params += [f"%{search}%", f"%{search}%"]
        if before_id:
            where.append("id<?")
            params.append(before_id)
        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return self._q(sql, tuple(params))

    def message_stats(self) -> dict:
        rows = self._q("SELECT direction, status, COUNT(*) AS n FROM messages WHERE kind='sms' GROUP BY direction, status")
        stats = {"received": 0, "sent": 0, "failed": 0, "queued": 0}
        for r in rows:
            if r["direction"] == "in":
                stats["received"] += r["n"]
            elif r["status"] in ("queued", "sending"):
                stats["queued"] += r["n"]
            elif r["status"] in stats:
                stats[r["status"]] += r["n"]
        return stats

    def claim_next_outgoing(self, modem_id: str) -> dict | None:
        """Atomically move the oldest queued message for this modem (or unassigned) to 'sending'."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT id FROM messages WHERE direction='out' AND status='queued' AND (modem_id=? OR modem_id IS NULL OR modem_id='') ORDER BY id LIMIT 1",
                    (modem_id,),
                ).fetchone()
                if not row:
                    self._conn.execute("COMMIT")
                    return None
                self._conn.execute("UPDATE messages SET status='sending', modem_id=?, attempts=attempts+1 WHERE id=?", (modem_id, row["id"]))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise
        return self.get_message(row["id"])

    def requeue_stuck(self, modem_id: str) -> None:
        self._exec("UPDATE messages SET status='queued' WHERE direction='out' AND status='sending' AND modem_id=?", (modem_id,))

    # ---- concatenated parts ----
    def add_part(self, modem_id: str, number: str, ref: int, total: int, seq: int, text: str, received_at: str | None) -> list[dict] | None:
        """Store one part; return all parts (ordered) once complete, else None."""
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO sms_parts (modem_id, number, ref, total, seq, text, received_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                (modem_id, number, ref, total, seq, text, received_at, now_iso()),
            )
            parts = self._q("SELECT * FROM sms_parts WHERE modem_id=? AND number=? AND ref=? ORDER BY seq", (modem_id, number, ref))
            if len(parts) >= total:
                self._conn.execute("DELETE FROM sms_parts WHERE modem_id=? AND number=? AND ref=?", (modem_id, number, ref))
                return parts
            return None

    def expired_part_groups(self, older_than_iso: str) -> list[list[dict]]:
        groups = self._q("SELECT modem_id, number, ref FROM sms_parts GROUP BY modem_id, number, ref HAVING MAX(created_at) < ?", (older_than_iso,))
        out = []
        for g in groups:
            parts = self._q("SELECT * FROM sms_parts WHERE modem_id=? AND number=? AND ref=? ORDER BY seq", (g["modem_id"], g["number"], g["ref"]))
            self._exec("DELETE FROM sms_parts WHERE modem_id=? AND number=? AND ref=?", (g["modem_id"], g["number"], g["ref"]))
            out.append(parts)
        return out

    # ---- webhooks ----
    def list_webhooks(self) -> list[dict]:
        return [self._wh(r) for r in self._q("SELECT * FROM webhooks ORDER BY id")]

    def add_webhook(self, url: str, events: list[str], secret: str | None, enabled: bool = True, type: str = "webhook") -> dict:
        rid = self._exec("INSERT INTO webhooks (type, url, secret, events, enabled, created_at) VALUES (?,?,?,?,?,?)",
                         (type, url, secret, json.dumps(events), 1 if enabled else 0, now_iso()))
        return self._wh(self._one("SELECT * FROM webhooks WHERE id=?", (rid,)))  # type: ignore[arg-type]

    def update_webhook(self, wid: int, **fields) -> dict | None:
        if "events" in fields:
            fields["events"] = json.dumps(fields["events"])
        if "enabled" in fields:
            fields["enabled"] = 1 if fields["enabled"] else 0
        if fields:
            sets = ", ".join(f"{k}=?" for k in fields)
            self._exec(f"UPDATE webhooks SET {sets} WHERE id=?", (*fields.values(), wid))
        row = self._one("SELECT * FROM webhooks WHERE id=?", (wid,))
        return self._wh(row) if row else None

    def delete_webhook(self, wid: int) -> bool:
        with self._lock:
            return self._conn.execute("DELETE FROM webhooks WHERE id=?", (wid,)).rowcount > 0

    @staticmethod
    def _wh(r: dict) -> dict:
        r = dict(r)
        r["events"] = json.loads(r["events"])
        r["enabled"] = bool(r["enabled"])
        return r
