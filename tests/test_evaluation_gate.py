"""CRITICAL: Science — the promotion gate is the scientific control.

A candidate harness may replace the incumbent ONLY on a statistically
significant improvement (paired bootstrap over per-task scores, above a
minimum effect size). These tests pin the two decisive regimes:

  * a noisy, small edge that is NOT significant (p well above alpha) -> REJECT;
  * a consistent edge that IS significant (p well below alpha)       -> ACCEPT.

The bootstrap is seeded, so every assertion below is deterministic.
"""

import random

from self_harness.evaluation.gating import GateConfig, PromotionGate
from self_harness.evaluation.metrics import GenerationMetrics


def metrics(version: str, scores: dict[str, float]) -> GenerationMetrics:
    return GenerationMetrics(harness_version=version, per_task_scores=scores)


def bootstrap_p(incumbent, candidate, seed=0, samples=20000):
    """Independent re-implementation of the gate's paired-bootstrap p-value,
    used only to document the significance regime each fixture sits in."""
    tasks = sorted(set(incumbent) & set(candidate))
    deltas = [candidate[t] - incumbent[t] for t in tasks]
    rng = random.Random(seed)
    n = len(deltas)
    worse = sum(
        1 for _ in range(samples)
        if sum(rng.choice(deltas) for _ in range(n)) / n <= 0
    )
    return worse / samples


# --- Insignificant improvement -> REJECT -------------------------------------

def test_insignificant_candidate_is_rejected():
    incumbent = {f"t{i}": 0.5 for i in range(10)}
    # Mean edge is positive (clears the relative-gain floor) but high variance:
    # several tasks regress, so the bootstrap cannot rule out zero effect.
    candidate = dict(zip(
        [f"t{i}" for i in range(10)],
        [0.6, 0.6, 0.3, 0.7, 0.3, 0.7, 0.4, 0.7, 0.4, 0.7],
    ))

    # The relative-gain floor is NOT what rejects this — confirm it passes that
    # and is rejected by the significance test specifically.
    mean_gain = sum(candidate.values()) / 10 - 0.5
    assert mean_gain / 0.5 > GateConfig().min_relative_gain

    p = bootstrap_p(incumbent, candidate)
    assert p > GateConfig().significance_alpha  # not significant (~0.15)

    gate = PromotionGate(GateConfig(bootstrap_samples=10_000), seed=0)
    assert gate.should_promote(metrics("inc", incumbent), metrics("cand", candidate)) is False


# --- Significant improvement -> ACCEPT ---------------------------------------

def test_significant_candidate_is_promoted():
    incumbent = {f"t{i}": 0.5 for i in range(12)}
    # Small but consistent edge across nearly all tasks: significant.
    candidate = dict(zip(
        [f"t{i}" for i in range(12)],
        [0.62, 0.60, 0.58, 0.45, 0.61, 0.59, 0.46, 0.60, 0.58, 0.62, 0.47, 0.61],
    ))

    p = bootstrap_p(incumbent, candidate)
    assert p < GateConfig().significance_alpha  # significant (~0.01)

    gate = PromotionGate(GateConfig(bootstrap_samples=10_000), seed=0)
    assert gate.should_promote(metrics("inc", incumbent), metrics("cand", candidate)) is True


# --- Guard rails -------------------------------------------------------------

def test_outright_regression_is_rejected():
    incumbent = {f"t{i}": 0.8 for i in range(10)}
    candidate = {f"t{i}": 0.5 for i in range(10)}
    gate = PromotionGate(GateConfig(bootstrap_samples=5_000), seed=0)
    assert not gate.should_promote(metrics("inc", incumbent), metrics("cand", candidate))


def test_significant_but_sub_threshold_gain_is_rejected():
    # Statistically clean but economically trivial: +1% relative, below the
    # 5% minimum effect size -> rejected before the bootstrap even runs.
    incumbent = {f"t{i}": 0.50 for i in range(20)}
    candidate = {f"t{i}": 0.505 for i in range(20)}
    gate = PromotionGate(GateConfig(min_relative_gain=0.05, bootstrap_samples=5_000), seed=0)
    assert not gate.should_promote(metrics("inc", incumbent), metrics("cand", candidate))


def test_disjoint_task_sets_never_promote():
    gate = PromotionGate(seed=0)
    assert not gate.should_promote(metrics("inc", {"x": 1.0}), metrics("cand", {"y": 1.0}))


def test_rollback_mirrors_promotion_in_reverse():
    # A production harness that significantly regresses vs. its promotion
    # baseline should trigger rollback.
    baseline = {f"t{i}": 0.8 for i in range(12)}
    production = {f"t{i}": 0.55 for i in range(12)}
    gate = PromotionGate(GateConfig(bootstrap_samples=5_000), seed=0)
    assert gate.should_rollback(metrics("base", baseline), metrics("prod", production))
