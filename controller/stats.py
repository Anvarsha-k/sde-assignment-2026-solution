"""Pure, deterministic statistics. No I/O, no randomness, no state.

The winner rule combines four protections:
  * Pocock inflation: looking daily would otherwise inflate false positives far above the nominal rate.
  * Holm step-down: several challengers are compared with one champion.
  * A minimum effect: a real but tiny lift is not worth a supervisor's attention.
  * Guardrail non-inferiority: the other KPI must be shown not to fall by more than the policy allows.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from statistics import NormalDist
from typing import Iterable

_N = NormalDist()

# Pocock constants for a two-sided 5% test with K equally spaced looks (Jennison & Turnbull, table 2.1).
_POCOCK_RATIO = {1: 1.960, 2: 2.178, 3: 2.289, 4: 2.361, 5: 2.413, 6: 2.453, 7: 2.485, 8: 2.512, 9: 2.535,
                 10: 2.555, 12: 2.592, 15: 2.626, 20: 2.672}


def pocock_inflation(looks: int) -> float:
    """Multiplier on a single-look critical value that keeps the error rate roughly constant over K looks."""
    k = max(1, looks)
    key = max((x for x in _POCOCK_RATIO if x <= k), default=1)
    return _POCOCK_RATIO[key] / 1.960


def critical_z(alpha_one_sided: float, looks: int) -> float:
    return _N.inv_cdf(1 - alpha_one_sided) * pocock_inflation(looks)


@dataclass(frozen=True)
class Diff:
    """Estimate of rate(a) - rate(b)."""
    estimate: float
    se: float
    na: int
    nb: int

    @property
    def z(self) -> float:
        return self.estimate / self.se if self.se > 0 else 0.0

    def lower(self, z: float) -> float:
        return self.estimate - z * self.se

    def as_dict(self) -> dict:
        return {"estimate": round(self.estimate, 5), "se": round(self.se, 5), "z": round(self.z, 3),
                "n_a": self.na, "n_b": self.nb}


def stratified_diff(strata: Iterable[tuple[int, int, int, int]], design_effect: float = 1.0) -> Diff:
    """Mantel-Haenszel style difference over strata of (xa, na, xb, nb).

    Arms are compared only inside a stratum where both ran, so a change of traffic mix or a supervisor
    edit between versions cannot create a Simpson's-paradox winner. A stratum with one arm has zero
    weight: an arm that never ran contributes nothing.
    """
    sw = swd = sv = 0.0
    na = nb = 0
    for xa, a, xb, b in strata:
        if a <= 0 or b <= 0:
            continue
        w = a * b / (a + b)
        pa, pb = xa / a, xb / b
        # Agresti-Caffo style smoothing keeps the variance positive at extreme rates.
        qa, qb = (xa + 1) / (a + 2), (xb + 1) / (b + 2)
        var = qa * (1 - qa) / a + qb * (1 - qb) / b
        sw += w
        swd += w * (pa - pb)
        sv += w * w * var
        na += a
        nb += b
    if sw == 0:
        return Diff(0.0, 0.0, na, nb)
    return Diff(swd / sw, math.sqrt(sv * design_effect) / sw, na, nb)


def one_sample_z(x: int, n: int, p0: float) -> float:
    """z of an observed rate against a known baseline. Positive means above baseline."""
    if n <= 0 or not 0 < p0 < 1:
        return 0.0
    return (x / n - p0) / math.sqrt(p0 * (1 - p0) / n)


def two_sample_z(xa: int, na: int, xb: int, nb: int) -> float:
    return stratified_diff([(xa, na, xb, nb)]).z


def holm_winners(z_by_arm: dict[str, float], alpha: float, looks: int) -> set[str]:
    """Holm step-down at the sequential boundary. Returns arms significantly above the champion."""
    ordered = sorted(z_by_arm, key=lambda a: -z_by_arm[a])
    m = len(ordered)
    won: set[str] = set()
    for i, arm in enumerate(ordered):
        if z_by_arm[arm] >= critical_z(alpha / (m - i), looks):
            won.add(arm)
        else:
            break
    return won


def samples_per_arm(p: float, delta: float, alpha: float = 0.05, power: float = 0.8) -> int:
    """Fixed-horizon sample size for a two-proportion test; used for reporting and planning."""
    p2 = min(max(p + delta, 1e-6), 1 - 1e-6)
    pbar = (p + p2) / 2
    z = _N.inv_cdf(1 - alpha) + _N.inv_cdf(power)
    return math.ceil(z * z * 2 * pbar * (1 - pbar) / (delta * delta))


def total_variation(a: dict[str, float], b: dict[str, float]) -> float:
    sa, sb = sum(a.values()) or 1.0, sum(b.values()) or 1.0
    return 0.5 * sum(abs(a.get(k, 0) / sa - b.get(k, 0) / sb) for k in set(a) | set(b))
