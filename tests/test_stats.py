"""Statistical rules, including the false-winner rate measured by simulation rather than assumed."""
import random

import pytest

from controller import stats


def test_stratified_diff_ignores_strata_where_only_one_arm_ran():
    # Only what really happened counts: an arm absent from a stratum adds nothing to the estimate.
    with_absent = stats.stratified_diff([(50, 100, 40, 100), (0, 0, 900, 1000)])
    without = stats.stratified_diff([(50, 100, 40, 100)])
    assert with_absent.estimate == without.estimate and with_absent.nb == without.nb


def test_stratified_diff_avoids_simpsons_paradox():
    # Arm A is better inside every stratum, but ran mostly in the low-rate stratum, so pooling reverses it.
    strata = [(90, 100, 800, 1000), (300, 1000, 20, 100)]
    pooled_a = (90 + 300) / 1100
    pooled_b = (800 + 20) / 1100
    assert pooled_a < pooled_b
    assert stats.stratified_diff(strata).estimate > 0


def test_design_effect_widens_uncertainty():
    rows = [(60, 100, 50, 100)]
    assert stats.stratified_diff(rows, 1.5).se > stats.stratified_diff(rows).se


def test_empty_strata_are_safe():
    assert stats.stratified_diff([]).z == 0.0


def test_critical_z_grows_with_looks_and_shrinking_alpha():
    assert stats.critical_z(0.05, 7) > stats.critical_z(0.05, 1)
    assert stats.critical_z(0.01, 7) > stats.critical_z(0.05, 7)


def test_holm_is_a_step_down():
    z = {"a": 4.0, "b": 1.0}
    assert stats.holm_winners(z, 0.05, 7) == {"a"}
    assert stats.holm_winners({"a": 1.0, "b": 0.5}, 0.05, 7) == set()


def test_holm_stops_at_first_failure():
    # b clears alpha/3. a at 1.9 misses alpha/2 (1.96), and testing stops there.
    assert stats.holm_winners({"a": 1.9, "b": 9.0, "c": 0.1}, 0.05, 1) == {"b"}


def test_samples_per_arm_matches_textbook_order_of_magnitude():
    n = stats.samples_per_arm(0.54, 0.05, alpha=0.025, power=0.8)
    assert 1400 < n < 1700


def test_total_variation():
    assert stats.total_variation({"a": 1}, {"b": 1}) == 1.0
    assert stats.total_variation({"a": 0.5, "b": 0.5}, {"a": 0.5, "b": 0.5}) == 0.0
    assert stats.total_variation({"a": 1, "b": 1}, {"a": 0.5, "b": 0.5}) == 0.0


def simulate_experiments(effect: float, trials: int, seed: int, k: int = 3, looks: int = 7, per_look: int = 240, base: float = 0.54):
    """Daily looks over k challengers. Returns how often any challenger is declared a winner."""
    rng = random.Random(seed)
    wins = 0
    for _ in range(trials):
        arm_x = [0] * (k + 1)
        arm_n = [0] * (k + 1)
        for _look in range(looks):
            for i in range(k + 1):
                p = base + (effect if i == 1 else 0.0)
                n = per_look * 2 if i == 0 else per_look
                # A normal draw for the binomial keeps this fast; accurate at these sample sizes.
                x = round(min(n, max(0, rng.gauss(n * p, (n * p * (1 - p)) ** 0.5))))
                arm_x[i] += x
                arm_n[i] += n
            zs = {}
            for i in range(1, k + 1):
                d = stats.stratified_diff([(arm_x[i], arm_n[i], arm_x[0], arm_n[0])], 1.10)
                if d.na >= 300 and d.nb >= 300:
                    zs[str(i)] = d
            won = stats.holm_winners({a: d.z for a, d in zs.items()}, 0.05, 7)
            if any(zs[a].estimate >= 0.015 for a in won):
                wins += 1
                break
    return wins / trials


def test_false_winner_rate_stays_near_nominal_under_daily_looks():
    # A/A: every arm is identical, so every declared winner is false. The design effect makes the test
    # a little conservative here because the simulated dials are independent.
    rate = simulate_experiments(effect=0.0, trials=2000, seed=11)
    assert rate <= 0.06, f"false-winner rate {rate:.3f}"


def test_real_lift_is_found_with_reasonable_power():
    rate = simulate_experiments(effect=0.08, trials=500, seed=12)
    assert rate >= 0.7, f"power {rate:.3f}"
