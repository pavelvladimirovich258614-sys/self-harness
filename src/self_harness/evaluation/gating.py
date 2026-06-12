"""Statistical promotion / rollback policy.

A candidate harness replaces the incumbent only when a paired bootstrap over
per-task scores shows a significant improvement above a minimum relative
gain. This single gate is what separates self-optimization from random walk:
without it, the loop drifts toward harnesses that please the mutation engine
rather than the task distribution.
"""

from __future__ import annotations

import random
from dataclasses import dataclass

from .metrics import GenerationMetrics


@dataclass
class GateConfig:
    min_relative_gain: float = 0.03
    significance_alpha: float = 0.05
    bootstrap_samples: int = 10_000


class PromotionGate:
    def __init__(self, config: GateConfig | None = None, seed: int = 0):
        self.config = config or GateConfig()
        self._rng = random.Random(seed)

    def should_promote(self, incumbent: GenerationMetrics, candidate: GenerationMetrics) -> bool:
        tasks = sorted(set(incumbent.per_task_scores) & set(candidate.per_task_scores))
        if not tasks:
            return False

        inc = incumbent.score_vector(tasks)
        cand = candidate.score_vector(tasks)
        deltas = [c - i for c, i in zip(cand, inc)]

        mean_inc = sum(inc) / len(inc)
        mean_delta = sum(deltas) / len(deltas)
        if mean_inc > 0 and mean_delta / mean_inc < self.config.min_relative_gain:
            return False
        if mean_inc == 0 and mean_delta <= 0:
            return False

        # Paired bootstrap: P(mean delta <= 0) under resampling.
        n = len(deltas)
        worse = sum(
            1
            for _ in range(self.config.bootstrap_samples)
            if sum(self._rng.choice(deltas) for _ in range(n)) / n <= 0
        )
        p_value = worse / self.config.bootstrap_samples
        return p_value < self.config.significance_alpha

    def should_rollback(self, baseline: GenerationMetrics, production: GenerationMetrics) -> bool:
        """Demote HEAD when live performance regresses below the score the
        candidate was promoted on (with the same significance machinery,
        direction reversed)."""
        return self.should_promote(incumbent=production, candidate=baseline)
