"""Long-horizon multi-step task suite.

Each task requires sustained tool use over dozens of steps with intermediate
verification — the regime where harness quality (context policy, retry
discipline, termination judgment) dominates raw model capability.
"""

from __future__ import annotations

import json

from ...core.harness import AgentState
from .base import BenchmarkTask, register_suite


class MultiFileRefactor(BenchmarkTask):
    task_id = "longhorizon/multi_file_refactor"

    def prompt(self) -> str:
        return (
            "Rename the function `compute` to `evaluate` across the project in "
            "/workspace, update all call sites and imports, run the test suite, "
            "and report TASK_COMPLETE only when all tests pass."
        )

    def score(self, final_state: AgentState) -> float:
        # Score on verified completion: terminated AND test run observed in history.
        if not final_state.done:
            return 0.0
        history = json.dumps(final_state.messages)
        return 1.0 if "tests passed" in history.lower() else 0.3


class DependencyUpgrade(BenchmarkTask):
    task_id = "longhorizon/dependency_upgrade"

    def prompt(self) -> str:
        return (
            "Upgrade the pinned `requests` dependency in /workspace to the latest "
            "compatible version, fix any resulting breakage, verify with the test "
            "suite, and report TASK_COMPLETE."
        )

    def score(self, final_state: AgentState) -> float:
        if not final_state.done:
            return 0.0
        return 1.0 if final_state.step < 40 else 0.7  # Efficiency-weighted


class FlakyToolRecovery(BenchmarkTask):
    """Stresses retry policy: tools fail intermittently by construction."""

    task_id = "longhorizon/flaky_tool_recovery"

    def prompt(self) -> str:
        return (
            "Collect the contents of records 1-20 via the `fetch_record` tool "
            "(which fails intermittently), assemble them into a single sorted "
            "report, and report TASK_COMPLETE."
        )

    def score(self, final_state: AgentState) -> float:
        history = json.dumps(final_state.messages)
        recovered = sum(1 for i in range(1, 21) if f"record_{i}" in history)
        return recovered / 20.0


register_suite("longhorizon", [MultiFileRefactor, DependencyUpgrade, FlakyToolRecovery])
