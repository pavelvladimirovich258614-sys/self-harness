"""Per-generation metrics: the fitness function of the evolutionary loop."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class GenerationMetrics:
    harness_version: str
    per_task_scores: dict[str, float] = field(default_factory=dict)
    mean_steps: float = 0.0
    mean_wall_clock_s: float = 0.0
    estimated_cost_usd: float = 0.0

    @property
    def mean_score(self) -> float:
        if not self.per_task_scores:
            return 0.0
        return sum(self.per_task_scores.values()) / len(self.per_task_scores)

    def score_vector(self, task_order: list[str]) -> list[float]:
        """Aligned per-task scores for paired statistical comparison."""
        return [self.per_task_scores.get(t, 0.0) for t in task_order]
