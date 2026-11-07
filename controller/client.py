"""HTTP client for the simulator. The only module that speaks HTTP.

Retry policy is decided per operation, because retrying is only safe when a repeat cannot cause harm:

  reads (GET)            retried on network errors, 429 and 5xx
  submit / apply / auto  also retried: the proposal ID and the approval state make repeats harmless,
                         and the caller reconciles when a repeat reports 'already done'
  advance                retried only after reading /sim/state, because the first call may have landed
  LLM / validation       never retried after the request may have reached the server: they spend credits
"""
from __future__ import annotations

import logging
import random
import time
from typing import Iterator

import httpx

log = logging.getLogger("controller.client")
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class ApiError(Exception):
    def __init__(self, status: int, code: str, detail):
        super().__init__(f"{status} {code}: {detail}")
        self.status, self.code, self.detail = status, code, detail


class Unavailable(Exception):
    """The simulator stayed unreachable through every retry."""


class SimClient:
    def __init__(self, url: str, timeout_s: float = 120.0, max_retries: int = 6, sleep=time.sleep,
                 transport: httpx.BaseTransport | None = None):
        self._http = httpx.Client(base_url=url, timeout=timeout_s, transport=transport)
        self._max_retries = max_retries
        self._sleep = sleep
        self.retries = 0

    def close(self) -> None:
        self._http.close()

    def _backoff(self, attempt: int) -> None:
        # Full jitter: stops many workers from retrying in lockstep.
        self.retries += 1
        self._sleep(random.uniform(0, min(30.0, 0.25 * 2 ** attempt)))

    def _request(self, method: str, path: str, retry: bool = True, **kw):
        last: Exception | None = None
        for attempt in range(self._max_retries + 1 if retry else 1):
            try:
                r = self._http.request(method, path, **kw)
            except httpx.TransportError as e:
                last = e
                log.warning("transport error on %s %s: %s", method, path, e)
                if retry and attempt < self._max_retries:
                    self._backoff(attempt)
                continue
            if r.status_code in RETRYABLE_STATUS and retry and attempt < self._max_retries:
                log.warning("%s on %s %s, retrying", r.status_code, method, path)
                self._backoff(attempt)
                continue
            if r.status_code >= 400:
                try:
                    body = r.json()
                except ValueError:
                    body = {"error": "non_json", "detail": r.text[:300]}
                raise ApiError(r.status_code, body.get("error", "unknown"), body.get("detail"))
            return r
        raise Unavailable(f"{method} {path}: {last}")

    # ---- reads
    def state(self) -> dict:
        return self._request("GET", "/sim/state").json()

    def list_chunks(self, after: int = -1) -> list[dict]:
        """Both kinds, in order. Chunk numbers are global across history and live events."""
        out = []
        for kind in ("history", "events"):
            out += self._request("GET", "/events/chunks", params={"kind": kind, "after": after}).json()
        return sorted(out, key=lambda m: m["chunk"])

    def stream_chunk(self, n: int) -> Iterator[bytes]:
        """Yields raw NDJSON lines without loading the chunk into memory first."""
        with self._http.stream("GET", f"/events/chunks/{n}") as r:
            if r.status_code >= 400:
                raise ApiError(r.status_code, "chunk_fetch_failed", n)
            for line in r.iter_lines():
                if line:
                    yield line

    def workflows(self) -> list[dict]:
        return self._request("GET", "/workflows").json()

    def workflow(self, wid: str) -> dict:
        return self._request("GET", f"/workflows/{wid}").json()

    def config(self, wid: str) -> dict:
        return self._request("GET", f"/workflows/{wid}/config").json()

    def config_history(self, wid: str) -> list[dict]:
        return self._request("GET", f"/workflows/{wid}/config/history").json()

    def approval(self, aid: str) -> dict:
        return self._request("GET", f"/approvals/{aid}").json()

    def approvals(self, wid: str) -> list[dict]:
        return self._request("GET", "/approvals", params={"workflow_id": wid}).json()

    # ---- time
    def advance(self, minutes: int, expected_minute: int) -> dict:
        """Advances time at most once, even if the response to the first call is lost."""
        for attempt in range(self._max_retries + 1):
            try:
                return self._request("POST", "/sim/advance", retry=False, json={"minutes": minutes}).json()
            except (Unavailable, ApiError) as e:
                if isinstance(e, ApiError) and e.status not in RETRYABLE_STATUS:
                    raise
                log.warning("advance failed (%s); checking whether it landed", e)
                self._backoff(attempt)
                try:
                    st = self.state()
                except Unavailable:
                    continue
                if st["sim_minute"] >= expected_minute + minutes:
                    return {"sim_minute": st["sim_minute"], "ts": st["ts"], "finished": st["finished"],
                            "new_chunks": []}
        raise Unavailable("advance kept failing")

    # ---- governance (repeats are harmless: see module docstring)
    def submit_approval(self, body: dict) -> dict:
        return self._request("POST", "/approvals", json=body).json()

    def apply(self, wid: str, approval_id: str) -> dict:
        return self._request("POST", f"/workflows/{wid}/apply", json={"approval_id": approval_id}).json()

    def auto_apply(self, wid: str, body: dict) -> dict:
        return self._request("PUT", f"/workflows/{wid}/config", json=body).json()

    # ---- credits (no blind retry)
    def suggest_fixes(self, wid: str, problem: str) -> dict:
        return self._request("POST", "/llm/suggest-fixes", retry=False,
                             json={"workflow_id": wid, "problem": problem}).json()

    def validate(self, wid: str, prompt_version: str, n: int) -> dict:
        return self._request("POST", "/validation/runs", retry=False,
                             json={"workflow_id": wid, "prompt_version": prompt_version, "n": n}).json()
