"""Trace analyzer: turns raw spans into the mutation engine's evidence base.

Three analytics families, all computable either in-memory (MemorySink) or as
BigQuery SQL at scale:

  1. Failure clustering   — group ERROR spans by tool / error class / step depth.
  2. Call-graph extraction — tool-calling transition matrix per harness version.
  3. Convergence metrics  — success rate, mean steps, mean latency per generation.

The report is intentionally *supplementary*: the mutation engine receives the
raw traces too. Aggregates point at the problem; raw traces contain the
mid-trace failure signal aggregates would wash out.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field

from .schema import SpanKind, TraceEvent


@dataclass
class AnalysisReport:
    harness_version: str
    total_runs: int = 0
    success_rate: float = 0.0
    mean_steps: float = 0.0
    failure_clusters: dict[str, int] = field(default_factory=dict)
    tool_transition_matrix: dict[str, dict[str, int]] = field(default_factory=dict)

    def summary(self) -> str:
        top_failures = sorted(self.failure_clusters.items(), key=lambda kv: -kv[1])[:5]
        return (
            f"harness={self.harness_version} runs={self.total_runs} "
            f"success_rate={self.success_rate:.3f} mean_steps={self.mean_steps:.1f} "
            f"top_failures={top_failures}"
        )


class TraceAnalyzer:
    def analyze(self, events: list[TraceEvent], harness_version: str) -> AnalysisReport:
        report = AnalysisReport(harness_version=harness_version)
        runs: dict[str, list[TraceEvent]] = defaultdict(list)
        for e in events:
            if e.harness_version == harness_version:
                runs[e.run_id].append(e)

        report.total_runs = len(runs)
        if not runs:
            return report

        successes, steps, failures = 0, [], Counter()
        transitions: dict[str, Counter] = defaultdict(Counter)

        for run_events in runs.values():
            run_events.sort(key=lambda e: e.step)
            prev_tool: str | None = None
            for e in run_events:
                if e.kind == SpanKind.RUN_END:
                    successes += int(bool(e.payload.get("success")))
                    steps.append(int(e.payload.get("steps", 0)))
                elif e.kind == SpanKind.ERROR:
                    # Cluster key: error class + step-depth bucket.
                    err = str(e.payload.get("error", "unknown")).split("(")[0]
                    failures[f"{err}@depth_{e.step // 10 * 10}"] += 1
                elif e.kind == SpanKind.TOOL_CALL:
                    tool = str(e.payload.get("tool"))
                    if "error" in str(e.payload.get("result_preview", "")):
                        failures[f"tool_error:{tool}"] += 1
                    if prev_tool is not None:
                        transitions[prev_tool][tool] += 1
                    prev_tool = tool

        report.success_rate = successes / report.total_runs
        report.mean_steps = sum(steps) / len(steps) if steps else 0.0
        report.failure_clusters = dict(failures)
        report.tool_transition_matrix = {k: dict(v) for k, v in transitions.items()}
        return report
