"""SQLite wrapper: connection setup, transactions, the audit log and common reads.

One process, one connection, one writer. WAL with synchronous=NORMAL survives a killed process with no
data loss. A power failure can lose the last commits but never corrupts the file, and the cursor rolls
back with its data, so the lost chunks are simply replayed.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

log = logging.getLogger("controller.store")
SCHEMA_VERSION = "2"


def dumps(o: Any) -> str:
    return json.dumps(o, sort_keys=True, separators=(",", ":"))


class Store:
    def __init__(self, path: str):
        self.path = path
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.execute("PRAGMA cache_size=-65536")
        self.db.executescript(Path(__file__).with_name("schema.sql").read_text())
        self.set_meta_default("schema_version", SCHEMA_VERSION)
        found = self.meta("schema_version")
        if found != SCHEMA_VERSION:
            raise RuntimeError(f"database schema {found} does not match code schema {SCHEMA_VERSION}")
        self._in_tx = False

    def close(self) -> None:
        self.db.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """Atomic unit. Nested use joins the outer transaction."""
        if self._in_tx:
            yield self.db
            return
        self.db.execute("BEGIN IMMEDIATE")
        self._in_tx = True
        try:
            yield self.db
            self.db.execute("COMMIT")
        except BaseException:
            self.db.execute("ROLLBACK")
            raise
        finally:
            self._in_tx = False

    # ---- meta
    def meta(self, key: str) -> str | None:
        r = self.db.execute("SELECT value FROM run_meta WHERE key=?", (key,)).fetchone()
        return r[0] if r else None

    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT INTO run_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                        (key, value))

    def set_meta_default(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO run_meta VALUES(?,?)", (key, value))

    # ---- audit
    def decide(self, minute: int, workflow_id: str | None, kind: str, summary: str, evidence: Any = None,
               **links) -> int:
        cols = ["minute", "workflow_id", "kind", "summary", "evidence", *links]
        vals = [minute, workflow_id, kind, summary, dumps(evidence or {}), *links.values()]
        if kind == "alert":
            severity = (evidence or {}).get("severity", "ticket").upper()
            log.error("ALERT %s workflow=%s %s look_at=%s", severity, workflow_id, summary, (evidence or {}).get("look_at", "decision table"))
        q = f"INSERT INTO decision({','.join(cols)}) VALUES({','.join('?' * len(cols))})"
        return self.db.execute(q, vals).lastrowid

    # ---- common reads
    def cursor(self) -> tuple[int, int]:
        r = self.db.execute("SELECT last_chunk,last_event_id FROM ingest_cursor").fetchone()
        return (r[0], r[1]) if r else (-1, 0)

    def workflow_specs(self) -> dict[str, dict]:
        return {r["workflow_id"]: json.loads(r["spec"]) for r in self.db.execute("SELECT * FROM workflow")}

    def latest_config(self, workflow_id: str) -> tuple[int, dict]:
        r = self.db.execute(
            "SELECT version,config FROM config_version WHERE workflow_id=? AND config IS NOT NULL "
            "ORDER BY version DESC LIMIT 1", (workflow_id,)).fetchone()
        return r["version"], json.loads(r["config"])

    def segments(self) -> dict[int, dict]:
        return {r[0]: json.loads(r[1]) for r in self.db.execute("SELECT segment_id,features FROM segment")}

    def cohorts(self, workflow_id: str) -> list[dict]:
        return [{"id": r["cohort_id"], "where": json.loads(r["where_clause"])} for r in self.db.execute(
            "SELECT * FROM cohort WHERE workflow_id=? ORDER BY ordinal", (workflow_id,))]

    def size_mb(self) -> float:
        total = 0
        for suffix in ("", "-wal"):
            try:
                total += os.path.getsize(self.path + suffix)
            except OSError:
                pass
        return total / 1e6
