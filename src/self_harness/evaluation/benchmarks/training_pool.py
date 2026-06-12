"""Training task pool — DISJOINT from every frozen benchmark suite.

The methodological firewall of the project: the mutation engine learns
exclusively from traces produced on this pool. Frozen benchmark suites
(`longhorizon`, held-out transfer suites) are never executed during trace
collection, and their definitions and scores are never serialized into a
mutation prompt. Any overlap between this pool and a frozen suite is a
protocol violation; `tests/test_suite_separation.py` enforces disjointness.
"""

from __future__ import annotations

import json

from ...core.harness import AgentState
from .base import BenchmarkTask, register_suite


class LogPipelineRepair(BenchmarkTask):
    task_id = "training/log_pipeline_repair"

    def prompt(self) -> str:
        return (
            "The log-ingestion script in /workspace crashes on malformed UTF-8 "
            "lines. Reproduce the crash with the bundled fixture, fix the "
            "decoder, re-run ingestion to completion, and report TASK_COMPLETE."
        )

    def score(self, final_state: AgentState) -> float:
        if not final_state.done:
            return 0.0
        return 1.0 if "ingestion complete" in json.dumps(final_state.messages).lower() else 0.3


class SchemaMigration(BenchmarkTask):
    task_id = "training/schema_migration"

    def prompt(self) -> str:
        return (
            "Add a nullable `created_at` column to every table defined in "
            "/workspace/schema.sql, regenerate the ORM models, run the migration "
            "test, and report TASK_COMPLETE when it passes."
        )

    def score(self, final_state: AgentState) -> float:
        if not final_state.done:
            return 0.0
        return 1.0 if final_state.step < 45 else 0.7


class RateLimitedCrawl(BenchmarkTask):
    """Stresses pacing and retry discipline: the fetch tool enforces a rate
    limit and returns 429-style errors on bursts."""

    task_id = "training/rate_limited_crawl"

    def prompt(self) -> str:
        return (
            "Collect pages 1-15 via the rate-limited `fetch_page` tool, extract "
            "every title, write them sorted to /workspace/titles.txt, and report "
            "TASK_COMPLETE."
        )

    def score(self, final_state: AgentState) -> float:
        history = json.dumps(final_state.messages)
        collected = sum(1 for i in range(1, 16) if f"page_{i}" in history)
        return collected / 15.0


register_suite("training", [LogPipelineRepair, SchemaMigration, RateLimitedCrawl])
