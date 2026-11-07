"""The control loop. One tick: advance, ingest, consume control events, act on approvals, decide, submit.

Time moves only when we call advance, and we call it only after finishing the previous tick. That is the
backpressure mechanism: the controller can never fall behind the world, and the loop's wall time is the
number to watch.
"""
from __future__ import annotations

import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

from .client import ApiError, SimClient, Unavailable
from .experiments import Experiments
from .faults import crash_point
from .governance import Governance
from .incidents import Incidents
from .ingest import Ingestor, SimClock
from .schedule import Scheduler
from .settings import Settings
from .store import Store, dumps

log = logging.getLogger("controller.loop")


def peak_rss_mb() -> float | None:
    try:
        import resource
        return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
    except ImportError:
        pass
    try:  # Windows, where the resource module does not exist
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD), ("PeakWorkingSetSize", ctypes.c_size_t),
                        ("WorkingSetSize", ctypes.c_size_t), ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPagedPoolUsage", ctypes.c_size_t), ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaNonPagedPoolUsage", ctypes.c_size_t), ("PagefileUsage", ctypes.c_size_t),
                        ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32 = ctypes.WinDLL("kernel32")
        psapi = ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e6
    except Exception:
        return None


def open_store(settings: Settings, state: dict, client: SimClient) -> Store:
    """Opens this world's database, or sets aside one that belongs to a different world.

    A simulator restart is a new world: our approvals and versions no longer exist there. Resuming would
    apply decisions to the wrong reality, so the old file is kept for audit and a new one started.
    """
    Path(settings.state_dir).mkdir(parents=True, exist_ok=True)
    path = str(Path(settings.state_dir) / f"{state['scenario']}.db")
    if os.path.exists(path):
        s = Store(path)
        _, last_event = s.cursor()
        known_minute = int(s.meta("last_minute") or 0)
        own_proposals = s.db.execute("SELECT COUNT(*) FROM proposal").fetchone()[0]
        new_world = last_event > state["last_event_id"] or known_minute > state["sim_minute"]
        if not new_world and own_proposals:
            new_world = any(not client.approvals(w) for (w,) in s.db.execute(
                "SELECT DISTINCT workflow_id FROM proposal WHERE approval_id IS NOT NULL"))
        s.close()
        if new_world:
            aside = f"{path}.old-{int(time.time())}"
            log.warning("database belongs to an earlier world; moving it to %s", aside)
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(path + suffix):
                    os.replace(path + suffix, aside + suffix)
    return Store(path)


def sync_workflows(store: Store, client: SimClient, gov: Governance, minute: int) -> dict[str, dict]:
    wfs = client.workflows()
    specs = {w["workflow_id"]: client.workflow(w["workflow_id"]) for w in wfs}
    with store.tx():
        for wid, spec in specs.items():
            store.db.execute("INSERT INTO workflow VALUES(?,?,?) ON CONFLICT(workflow_id) DO UPDATE SET spec=excluded.spec",
                             (wid, spec["tenant_id"], dumps(spec)))
    for wid in specs:
        gov.sync_config(wid, minute)
    return specs


def run(url: str, settings: Settings) -> int:
    """Survives a simulator outage by starting over from durable state, the same path as a crash restart."""
    outages = 0
    while True:
        try:
            return _run_until_finished(url, settings)
        except Unavailable as e:
            outages += 1
            if outages > settings.max_outage_restarts:
                log.critical("simulator unreachable after %s recoveries; giving up: %s", outages, e)
                return 2
            log.error("simulator unreachable (%s); resuming from saved state, attempt %s", e, outages)
            time.sleep(min(60.0, 2.0 * outages))


def _run_until_finished(url: str, settings: Settings) -> int:
    client = SimClient(url, settings.http_timeout_s, settings.http_max_retries)
    state = client.state()
    if state["finished"]:
        log.info("scenario already finished")
    store = open_store(settings, state, client)
    epoch = (datetime.fromisoformat(state["ts"]) - timedelta(minutes=state["sim_minute"])).isoformat()
    store.set_meta_default("epoch", epoch)
    store.set_meta_default("credits_start", str(state["credits_remaining"]))
    store.set_meta_default("end_minute", str(state["end_minute"]))
    store.set_meta_default("scenario", state["scenario"])

    minute = state["sim_minute"]
    gov = Governance(store, client, settings)
    specs = sync_workflows(store, client, gov, minute)
    clock = SimClock(epoch)
    ingestor = Ingestor(store, client, clock, settings)
    recovered = store.cursor()[0] >= 0
    t0 = time.perf_counter()
    stats = ingestor.catch_up()
    log.info("startup ingest: %s chunks, %s events, %.1fs", stats.chunks, stats.events, stats.seconds)
    if recovered:
        out = gov.reconcile(minute)
        log.warning("restarted at minute %s; reconciled %s", minute, out)
    elif stats.events:
        store.decide(minute, None, "bootstrap", f"Ingested {stats.events} history events in {stats.seconds:.1f}s.", {"chunks": stats.chunks})

    incidents = Incidents(store, client, gov, settings)
    incidents.credits_remaining = state["credits_remaining"]
    experiments = Experiments(store, gov, settings, state["end_minute"])
    scheduler = Scheduler(store, gov, settings)

    def decide(now: int) -> None:
        gov.consume_inbox(now)
        gov.poll_and_apply(now)
        incidents.tick(now, specs)
        with store.tx():
            scheduler.tick(now, specs, incidents.open_did_workflows())
            experiments.tick(now, specs, {w: incidents.blocked_arms(w) for w in specs})
        gov.flush(now, specs)

    finished = state["finished"]
    if not finished:
        decide(minute)
    advance_s = 0.0
    while not finished:
        t = time.perf_counter()
        try:
            res = client.advance(settings.tick_minutes, minute)
        except ApiError as e:
            if e.status == 409:  # the scenario is over
                break
            raise
        advance_s = time.perf_counter() - t
        if res["sim_minute"] < minute:
            log.critical("simulator went back in time (%s < %s): it restarted. Refusing to act on a new world.", res["sim_minute"], minute)
            store.decide(minute, None, "alert", "Simulator clock went backwards: treated as a new world. Stopped.", {"severity": "page"})
            return 3
        minute = res["sim_minute"]
        finished = res["finished"]
        crash_point("after_advance_before_ingest")
        stats = ingestor.catch_up()
        crash_point("after_ingest_before_decide")
        if stats.bad:
            store.decide(minute, None, "alert", f"{stats.bad} malformed events skipped and kept in bad_event.",
                         {"severity": "ticket", "look_at": "SELECT * FROM bad_event"})
        t = time.perf_counter()
        decide(minute)
        decide_s = time.perf_counter() - t
        store.set_meta("last_minute", str(minute))
        store.db.execute(
            "INSERT OR REPLACE INTO loop_metric VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (minute, stats.events, stats.duplicates, stats.bad, stats.seconds, decide_s, advance_s, 0, peak_rss_mb(), store.size_mb(),
             incidents.credits_remaining, client.retries))
        if minute % 1440 == 0:
            log.info("D%s done: wall %.0fs, peak rss %.0f MB, db %.0f MB", minute // 1440, time.perf_counter() - t0, peak_rss_mb() or 0, store.size_mb())

    final = client.state()
    store.decide(minute, None, "run_finished", f"Scenario finished. Credits left: {final['credits_remaining']}.",
                 {"wall_s": round(time.perf_counter() - t0, 1), "peak_rss_mb": peak_rss_mb(), "db_mb": store.size_mb()})
    log.info("finished: wall %.0fs, credits left %s, db %s", time.perf_counter() - t0, final["credits_remaining"], store.path)
    store.close()
    client.close()
    return 0
