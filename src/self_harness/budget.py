"""Budget guard: hard spend controls for the autonomous loop.

An unsupervised mutate-evaluate loop is an unbounded API spender by default.
The guard enforces three caps and acts as the kill switch:

  * per-generation mutation token budget (the long-context calls dominate cost);
  * cumulative USD ceiling for the whole experiment;
  * sandbox run count ceiling (GPU-hour proxy).

The orchestrator consults the guard before every paid operation and aborts
the loop — not the process — the moment any ceiling is reached. All spend
estimates are recorded into GenerationMetrics for the convergence reports.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

CHARS_PER_TOKEN = 4


@dataclass
class BudgetConfig:
    max_total_usd: float = 500.0
    max_mutation_tokens_per_generation: int = 1_000_000
    max_sandbox_runs: int = 500
    mutation_usd_per_mtoken: float = 2.50   # Blended in/out rate for the mutation tier
    sandbox_usd_per_run: float = 1.00       # Amortized GPU + node cost per evaluation


@dataclass
class BudgetGuard:
    config: BudgetConfig = field(default_factory=BudgetConfig)
    total_usd: float = 0.0
    sandbox_runs: int = 0
    generation_mutation_tokens: int = 0
    generation_usd: float = 0.0

    def start_generation(self) -> None:
        self.generation_mutation_tokens = 0
        self.generation_usd = 0.0

    def allow_mutation_call(self, request_chars: int) -> bool:
        tokens = request_chars // CHARS_PER_TOKEN
        if self.generation_mutation_tokens + tokens > self.config.max_mutation_tokens_per_generation:
            logger.warning("budget: per-generation mutation token cap reached")
            return False
        return not self.exhausted

    def record_mutation_call(self, request_chars: int, response_chars: int) -> None:
        tokens = (request_chars + response_chars) // CHARS_PER_TOKEN
        usd = tokens / 1e6 * self.config.mutation_usd_per_mtoken
        self.generation_mutation_tokens += tokens
        self.generation_usd += usd
        self.total_usd += usd

    def allow_sandbox_run(self) -> bool:
        if self.sandbox_runs >= self.config.max_sandbox_runs:
            logger.warning("budget: sandbox run cap reached")
            return False
        return not self.exhausted

    def record_sandbox_run(self) -> None:
        self.sandbox_runs += 1
        self.generation_usd += self.config.sandbox_usd_per_run
        self.total_usd += self.config.sandbox_usd_per_run

    @property
    def exhausted(self) -> bool:
        """Kill-switch condition: cumulative USD ceiling reached."""
        if self.total_usd >= self.config.max_total_usd:
            logger.error(
                "budget: KILL SWITCH — cumulative spend %.2f USD >= cap %.2f USD",
                self.total_usd, self.config.max_total_usd,
            )
            return True
        return False
