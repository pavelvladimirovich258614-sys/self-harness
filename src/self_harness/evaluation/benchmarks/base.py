"""Benchmark task interface.

Suites are FROZEN per experiment: the mutation engine must never see
benchmark task definitions or per-task scores, only training-run traces.
This separation is the primary defense against self-optimization collapsing
into benchmark overfitting.
"""

from __future__ import annotations

import abc

from ...core.harness import AgentState


class BenchmarkTask(abc.ABC):
    task_id: str

    @abc.abstractmethod
    def prompt(self) -> str:
        """Task statement handed to the agent."""

    @abc.abstractmethod
    def score(self, final_state: AgentState) -> float:
        """Deterministic score in [0, 1] computed from the final state."""


_SUITES: dict[str, list[type[BenchmarkTask]]] = {}


def register_suite(name: str, tasks: list[type[BenchmarkTask]]) -> None:
    _SUITES[name] = tasks


def get_suite(name: str) -> list[BenchmarkTask]:
    if name not in _SUITES:
        raise KeyError(f"unknown benchmark suite: {name}")
    return [cls() for cls in _SUITES[name]]


from . import longhorizon  # noqa: E402,F401 — registers the default suite
