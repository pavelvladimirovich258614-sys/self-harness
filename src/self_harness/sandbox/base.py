"""Sandbox interface and execution-result contract.

Invariant: mutated harness code never executes in the orchestrator process.
Every candidate runs the benchmark suite inside an isolated environment with
hard CPU / memory / wall-clock quotas and no credentials. A candidate that
hangs, crashes, or violates limits is discarded — the result object records
the violation as evaluation signal, never as an orchestrator exception.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass, field


@dataclass
class SandboxResult:
    completed: bool                 # False on timeout / OOM / crash / escape attempt
    per_task_scores: dict[str, float] = field(default_factory=dict)
    wall_clock_s: float = 0.0
    violation: str = ""             # Reason for forced termination, if any
    logs_tail: str = ""             # Last lines of candidate stdout/stderr


@dataclass
class SandboxLimits:
    cpu: str = "2"
    memory: str = "4Gi"
    wall_clock_s: int = 1800
    network: str = "deny"


class Sandbox(abc.ABC):
    @abc.abstractmethod
    def evaluate(
        self,
        harness_version: str,
        artifacts_dir: str,
        suite: str,
        episodes_per_task: int,
        limits: SandboxLimits,
        seed: int = 0,
    ) -> SandboxResult:
        """Run the benchmark suite for one candidate harness in isolation.

        Candidate and incumbent must be evaluated with identical `seed`,
        `episodes_per_task`, and `limits` — the promotion gate assumes
        matched conditions.

        The sandbox mounts the candidate's artifact directory read-only,
        executes `python -m self_harness.evaluation.pipeline` inside the
        container, and harvests a per-task score file from a result volume.
        """
