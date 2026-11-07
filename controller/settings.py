"""Central configuration. Every tunable lives here and can be overridden by environment variable.

Nothing in this file may describe a specific scenario: no workflow IDs, variant names or dates.
Tenant preferences are the one per-client input, and they come from what clients told us in writing.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields


# Requests a client has asked for that are stricter than the policy the simulator enforces.
# Kestrel's operations supervisor: "Two requests a day, maximum, and never before 11:00."
# Supported keys: max_requests_per_day, not_before_hour, no_auto_apply (send every change for approval).
DEFAULT_TENANT_PREFS = {
    "kestrel_finance": {"max_requests_per_day": 2, "not_before_hour": 11},
}


@dataclass(frozen=True)
class Settings:
    state_dir: str = "state"
    tick_minutes: int = 15
    http_timeout_s: float = 120.0
    http_max_retries: int = 6
    keep_call_log_for_history: bool = False
    max_outage_restarts: int = 30

    # Experiments
    alpha_family: float = 0.05          # one-sided family-wise false-winner rate per dimension
    max_looks: int = 7                  # planned daily looks; sets the Pocock inflation
    min_arm_samples: int = 300          # no test on an arm with fewer target-metric trials
    min_effect: float = 0.015           # smaller lifts are not worth a supervisor's attention
    control_share: float = 0.40         # champion's share of traffic while experimenting
    holdout_share: float = 0.15         # old champion's share after promotion, for decay detection
    drop_arm_z: float = -2.0            # an arm this far behind champion stops receiving traffic
    design_effect: float = 1.10         # repeat dials on one lead are not independent
    guard_confidence_z: float = 0.84    # one-sided 80% for guardrail non-inferiority
    decay_long_z: float = -2.8          # winner last 24h vs holdout over the whole run
    decay_z: float = -2.24              # winner vs concurrent holdout, one-sided p of about 0.0125
    min_days_left_to_start: int = 2     # do not start an experiment that cannot conclude

    # Governance
    apply_retry_cooldown_ticks: int = 8 # wait this long before regenerating a rejected change

    # Detection
    incident_window_hours: int = 6
    incident_min_connected: int = 100
    incident_z: float = 5.0             # counts are small and windows overlap, so z must be conservative
    incident_z_after_change: float = 3.5  # a prompt changed in the last day is already a prime suspect
    hall_min_ratio_after_change: float = 2.0
    hall_min_ratio: float = 2.5         # flagged rate must be at least this multiple of baseline
    did_window_hours: int = 12
    did_min_dials: int = 150
    did_z: float = -3.5
    did_min_rel_drop: float = 0.20

    # Incident response
    validation_n: int = 400             # 4 credits; SE near 0.97 is about 0.0085
    verify_min_connected: int = 300
    credit_share_multiplier: float = 1.5  # a workflow may use this multiple of its equal share

    # Schedule
    schedule_gamma: float = 2.0
    schedule_min_weight: float = 0.5
    schedule_max_weight: float = 2.0
    schedule_min_pickup_gain: float = 0.002
    cohort_min_share: float = 0.08

    tenant_prefs: dict = field(default_factory=lambda: dict(DEFAULT_TENANT_PREFS))

    @staticmethod
    def from_env() -> "Settings":
        """CTRL_<FIELD_NAME> overrides a field; values are parsed as JSON when possible."""
        kw = {}
        for f in fields(Settings):
            raw = os.environ.get("CTRL_" + f.name.upper())
            if raw is None:
                continue
            try:
                kw[f.name] = json.loads(raw)
            except ValueError:
                kw[f.name] = raw
        return Settings(**kw)
