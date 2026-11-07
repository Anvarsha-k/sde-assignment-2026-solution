"""Deterministic crash points for recovery tests. Inactive unless CTRL_CRASH_POINTS is set.

Random kills rarely land in the few milliseconds around an outside call, which is the window that matters
most. A crash point exits the process hard at exactly that moment, once, as a kill -9 would: no cleanup and
no flush. A marker file stops it firing again after the restart.
"""
from __future__ import annotations

import os
from pathlib import Path

_POINTS = frozenset(p for p in os.environ.get("CTRL_CRASH_POINTS", "").split(",") if p)
_DIR = os.environ.get("CTRL_STATE_DIR", "state")
EXIT_CODE = 137


def crash_point(name: str) -> None:
    if name not in _POINTS:
        return
    marker = Path(_DIR) / f".crashed-{name}"
    if not marker.exists():
        marker.touch()
        os._exit(EXIT_CODE)
