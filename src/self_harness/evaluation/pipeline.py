"""Candidate-vs-incumbent evaluation runner.

Runs INSIDE the sandbox (`python -m self_harness.evaluation.pipeline`): loads
one harness version from the mounted artifact directory, executes the frozen
benchmark suite under fixed seeds, and writes per-task scores to the result
volume. The orchestrator never imports candidate code directly.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from ..core.agent import Agent
from ..core.harness_loader import HarnessLoader
from ..tracing.collector import MemorySink, TraceCollector
from .benchmarks.base import get_suite
from .metrics import GenerationMetrics


class EvaluationPipeline:
    def __init__(self, harness_version: str, artifacts_dir: Path,
                 suite: str, episodes_per_task: int):
        self.harness_version = harness_version
        self.artifacts_dir = artifacts_dir
        self.suite_name = suite
        self.episodes_per_task = episodes_per_task

    def run(self, model_call, tools) -> GenerationMetrics:
        loader = HarnessLoader(self.artifacts_dir)
        harness = loader.load(self.harness_version, model_call=model_call)
        collector = TraceCollector(MemorySink())

        metrics = GenerationMetrics(harness_version=self.harness_version)
        for task in get_suite(self.suite_name):
            scores = []
            for _ in range(self.episodes_per_task):
                agent = Agent(harness=harness, tools=tools, collector=collector)
                final_state = agent.run(task.prompt())
                scores.append(task.score(final_state))
            metrics.per_task_scores[task.task_id] = sum(scores) / len(scores)
        return metrics


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate one harness version in-sandbox")
    parser.add_argument("--harness-version", required=True)
    parser.add_argument("--artifacts-dir", required=True)
    parser.add_argument("--suite", default="longhorizon")
    parser.add_argument("--episodes-per-task", type=int, default=5)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()

    # Inside the sandbox the model endpoint is the only allowed egress;
    # tools operate on the task's mounted /workspace fixture.
    from ..orchestrator import build_model_call, build_task_tools

    pipeline = EvaluationPipeline(
        harness_version=args.harness_version,
        artifacts_dir=Path(args.artifacts_dir),
        suite=args.suite,
        episodes_per_task=args.episodes_per_task,
    )
    metrics = pipeline.run(model_call=build_model_call(), tools=build_task_tools())
    Path(args.output).write_text(json.dumps(metrics.per_task_scores, indent=2))


if __name__ == "__main__":
    main()
