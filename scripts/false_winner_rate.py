#!/usr/bin/env python
"""Measures the winner rule's error rates by simulation, using the same functions the controller uses.

An A/A run has identical arms, so every declared winner is false. The rule looks once a day and stops at the first win,
exactly as the controller does. Prints a table for the speed vs. false-winner trade-off in SUBMISSION.md.

    python scripts/false_winner_rate.py
"""
from __future__ import annotations

import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from controller import stats  # noqa: E402

ALPHA, MAX_LOOKS, MIN_ARM, MIN_EFFECT, DEFF = 0.05, 7, 300, 0.015, 1.10


def one_run(rng: random.Random, effect: float, k: int, per_look: int, base: float, alpha: float, min_effect: float, inflate: bool) -> int | None:
    """Returns the look at which a winner was declared, or None."""
    x, n = [0] * (k + 1), [0] * (k + 1)
    looks = MAX_LOOKS if inflate else 1
    for look in range(1, MAX_LOOKS + 1):
        for i in range(k + 1):
            p = base + (effect if i == 1 else 0.0)
            m = round(per_look * 2) if i == 0 else per_look
            x[i] += round(min(m, max(0, rng.gauss(m * p, (m * p * (1 - p)) ** 0.5))))
            n[i] += m
        diffs = {str(i): stats.stratified_diff([(x[i], n[i], x[0], n[0])], DEFF) for i in range(1, k + 1)}
        ready = {a: d for a, d in diffs.items() if d.na >= MIN_ARM and d.nb >= MIN_ARM}
        won = stats.holm_winners({a: d.z for a, d in ready.items()}, alpha, looks)
        if any(ready[a].estimate >= min_effect for a in won):
            return look
    return None


def rate(effect: float, trials: int, seed: int, **kw) -> tuple[float, float | None]:
    rng = random.Random(seed)
    hits = [one_run(rng, effect, **kw) for _ in range(trials)]
    won = [h for h in hits if h is not None]
    return len(won) / trials, (sum(won) / len(won) if won else None)


def main() -> None:
    trials = 4000
    per_look = 200  # about one arm's daily connected calls at 20% of a 1,000-a-day workflow
    base_kw = dict(k=3, per_look=per_look, base=0.54)
    print(f"{trials} runs each; 3 challengers, {per_look} trials per arm per day, base rate 0.54, up to {MAX_LOOKS} daily looks\n")
    print("Without correcting for daily looks (for comparison):")
    fp, _ = rate(0.0, trials, 1, alpha=ALPHA, min_effect=MIN_EFFECT, inflate=False, **base_kw)
    print(f"  A/A false-winner rate {fp:.3f}\n")
    print("| one-sided alpha | A/A false-winner rate | power at +5 pts | power at +8 pts | power at +12 pts | mean day of win at +8 |")
    print("|---|---|---|---|---|---|")
    for alpha in (0.01, 0.05, 0.10, 0.20):
        fp, _ = rate(0.0, trials, 2, alpha=alpha, min_effect=MIN_EFFECT, inflate=True, **base_kw)
        p5, _ = rate(0.05, trials // 2, 3, alpha=alpha, min_effect=MIN_EFFECT, inflate=True, **base_kw)
        p8, d8 = rate(0.08, trials // 2, 4, alpha=alpha, min_effect=MIN_EFFECT, inflate=True, **base_kw)
        p12, _ = rate(0.12, trials // 2, 5, alpha=alpha, min_effect=MIN_EFFECT, inflate=True, **base_kw)
        print(f"| {alpha:.2f} | {fp:.3f} | {p5:.2f} | {p8:.2f} | {p12:.2f} | {d8:.1f} |")


if __name__ == "__main__":
    main()
