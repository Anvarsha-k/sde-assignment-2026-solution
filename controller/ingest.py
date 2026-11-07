"""Event ingestion: chunk in, aggregates out, cursor forward, all in one transaction.

Exactly-once effect comes from that transaction, not from a dedupe table. A crash before COMMIT leaves
no trace and the chunk is fetched again; a crash after COMMIT means the cursor already moved past it.
event_id is strictly increasing, so anything at or below the cursor is a duplicate and is counted and
skipped.

Raw events are reduced to a cube of counts. Its size depends on the number of workflows, hours,
segments and variants, not on the number of calls, so a long run does not grow the analytical store
in proportion to traffic. Memory is bounded per chunk and by the set of campaigns still dialling.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime

import httpx

from .client import ApiError, SimClient
from .faults import crash_point
from .settings import Settings
from .store import Store, dumps

log = logging.getLogger("controller.ingest")
CONTROL_TYPES = {"config_changed", "approval_decided"}
MAX_CHUNK_ATTEMPTS = 5


@dataclass
class IngestStats:
    chunks: int = 0
    events: int = 0
    duplicates: int = 0
    bad: int = 0
    seconds: float = 0.0


class SimClock:
    """Converts event timestamps to simulated minutes. Anchored on /sim/state, never on a fixed date."""

    def __init__(self, epoch_iso: str):
        self.epoch = datetime.fromisoformat(epoch_iso)
        self._hour_cache: dict[str, int] = {}

    def minute(self, ts: str) -> int:
        # Timestamps within one hour share a prefix, so the expensive parse happens once per hour.
        key = ts[:13]
        base = self._hour_cache.get(key)
        if base is None:
            base = int((datetime.fromisoformat(key + ":00:00" + ts[19:]) - self.epoch).total_seconds() // 60)
            self._hour_cache[key] = base
        return base + int(ts[14:16])


class Ingestor:
    def __init__(self, store: Store, client: SimClient, clock: SimClock, settings: Settings):
        self.store, self.client, self.clock, self.settings = store, client, clock, settings
        self._reload_caches()

    def _reload_caches(self) -> None:
        self.segments: dict[str, int] = {r[1]: r[0] for r in self.store.db.execute("SELECT segment_id,features FROM segment")}
        self.leads: dict[str, dict[str, int]] = {}
        self.cadence_days: dict[str, int] = {}
        for wid, spec in self.store.workflow_specs().items():
            self.cadence_days[wid] = len(spec.get("cadence", [])) or 3
        for r in self.store.db.execute("SELECT campaign_id,lead_id,segment_id FROM lead_dim"):
            self.leads.setdefault(r[0], {})[r[1]] = r[2]

    def _segment_id(self, features: dict) -> int:
        key = dumps(features)
        sid = self.segments.get(key)
        if sid is None:
            sid = self.store.db.execute("INSERT INTO segment(features) VALUES(?)", (key,)).lastrowid
            self.segments[key] = sid
        return sid

    def catch_up(self) -> IngestStats:
        """Processes every chunk after the cursor, in order."""
        stats = IngestStats()
        start = time.perf_counter()
        last_chunk, _ = self.store.cursor()
        for meta in self.client.list_chunks(after=last_chunk):
            for attempt in range(MAX_CHUNK_ATTEMPTS):
                try:
                    self._ingest_chunk(meta, stats)
                    break
                except (httpx.TransportError, IOError, ApiError) as e:
                    # The transaction rolled back, so caches may hold rows that no longer exist.
                    log.warning("chunk %s failed (%s); refetching", meta["chunk"], e)
                    self._reload_caches()
                    time.sleep(min(5.0, 0.2 * 2 ** attempt))
            else:
                raise IOError(f"chunk {meta['chunk']} failed {MAX_CHUNK_ATTEMPTS} times")
        stats.seconds = time.perf_counter() - start
        return stats

    def _ingest_chunk(self, meta: dict, stats: IngestStats) -> None:
        n, is_history = meta["chunk"], meta.get("kind") == "history"
        last_chunk, last_event = self.store.cursor()
        if n <= last_chunk:
            return
        cube: dict[tuple, list[int]] = {}
        leads_rows, calls_rows, campaigns, control, bad = [], [], [], [], []
        seen = dup = 0
        loads, minute_of = json.loads, self.clock.minute
        keep_calls = not is_history or self.settings.keep_call_log_for_history
        new_segments = False

        with self.store.tx():  # segment inserts join this transaction
            for raw in self.client.stream_chunk(n):
                seen += 1
                try:
                    ev = loads(raw)
                    eid, typ = ev["event_id"], ev["type"]
                    if eid <= last_event:
                        dup += 1
                        continue
                    if typ == "call":
                        v = ev["variant"]
                        wid = ev["workflow_id"]
                        mins = minute_of(ev["ts"])
                        seg = self.leads.get(ev["campaign_id"], {}).get(ev["lead_id"], -1)
                        key = (wid, mins // 60, seg, ev["config_version"], v["welcome_message"], v["voice"],
                               v["caller_id"], v["prompt_version"])
                        c = cube.get(key)
                        if c is None:
                            c = cube[key] = [0, 0, 0, 0]
                        con = ev["connected"]
                        c[0] += 1
                        if con:
                            c[1] += 1
                            if ev["engaged"]:
                                c[2] += 1
                            if ev["qa"]["hallucination_flag"]:
                                c[3] += 1
                        if keep_calls:
                            calls_rows.append((eid, mins, wid, ev["person_id"], ev["lead_id"], ev["campaign_id"], ev["attempt_no"],
                                               ev["config_version"], ev["cohort_id"], v["welcome_message"], v["voice"],
                                               v["caller_id"], v["prompt_version"], int(con), int(ev["engaged"]),
                                               ev.get("disposition")))
                    elif typ == "lead":
                        seg = self._segment_id(ev["features"])
                        self.leads.setdefault(ev["campaign_id"], {})[ev["lead_id"]] = seg
                        leads_rows.append((ev["campaign_id"], ev["lead_id"], seg))
                    elif typ == "campaign_created":
                        campaigns.append((ev["campaign_id"], ev["workflow_id"], ev["leads"],
                                          ev["config_version_at_creation"], minute_of(ev["ts"])))
                    elif typ in CONTROL_TYPES:
                        control.append((eid, typ, raw.decode() if isinstance(raw, bytes) else raw))
                    else:
                        bad.append((n, f"unknown type {typ}", str(raw)[:2000]))
                    last_event = eid
                except (ValueError, KeyError, TypeError) as e:
                    # One corrupt line must not stop the loop or block every later chunk.
                    bad.append((n, repr(e), str(raw)[:2000]))

            crash_point("mid_chunk_before_commit")
            if seen != meta.get("events", seen):
                raise IOError(f"chunk {n}: expected {meta['events']} lines, got {seen}")  # rolls back; refetched

            db = self.store.db
            db.executemany(
                "INSERT INTO kpi_cube VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT DO UPDATE SET "
                "dials=dials+excluded.dials, connected=connected+excluded.connected, "
                "engaged=engaged+excluded.engaged, hallucinations=hallucinations+excluded.hallucinations",
                [(*k, *v) for k, v in cube.items()])
            db.executemany("INSERT OR IGNORE INTO lead_dim VALUES(?,?,?)", leads_rows)
            db.executemany("INSERT OR IGNORE INTO call_log VALUES(" + ",".join("?" * 16) + ")", calls_rows)
            db.executemany("INSERT OR IGNORE INTO campaign VALUES(?,?,?,?,?)", campaigns)
            db.executemany("INSERT OR IGNORE INTO inbox(event_id,type,payload) VALUES(?,?,?)", control)
            db.executemany("INSERT INTO bad_event(chunk,reason,raw) VALUES(?,?,?)", bad)
            for wid in {c[1] for c in campaigns}:
                self._evict_finished_campaigns(wid)
            db.execute("INSERT INTO ingest_cursor(id,last_chunk,last_event_id) VALUES(1,?,?) "
                       "ON CONFLICT(id) DO UPDATE SET last_chunk=excluded.last_chunk, "
                       "last_event_id=excluded.last_event_id", (n, last_event))
        stats.chunks += 1
        stats.events += seen - dup - len(bad)
        stats.duplicates += dup
        stats.bad += len(bad)

    def _evict_finished_campaigns(self, wid: str) -> None:
        # The cadence ends a campaign after one entry per day; two days of margin cover late retries.
        keep = self.cadence_days.get(wid, 3) + 2
        rows = self.store.db.execute(
            "SELECT campaign_id FROM campaign WHERE workflow_id=? ORDER BY created_minute DESC LIMIT -1 OFFSET ?",
            (wid, keep)).fetchall()
        for (cid,) in rows:
            if cid in self.leads:
                self.leads.pop(cid)
                self.store.db.execute("DELETE FROM lead_dim WHERE campaign_id=?", (cid,))
