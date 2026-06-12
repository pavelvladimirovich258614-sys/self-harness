"""Top-level self-optimization loop.

One generation, end to end:

    1. ACT        — run the agent batch on training tasks under HEAD harness.
    2. TELEMETRY  — flush execution traces to the configured sink (BigQuery).
    3. ANALYZE    — failure clustering, call graphs, convergence stats.
    4. MUTATE     — Gemini reads whole traces + harness source, emits a patch.
    5. VALIDATE   — static gates: syntax, imports, Harness contract.
    6. SANDBOX    — frozen benchmark suite in an isolated GKE Job / container.
    7. GATE       — paired-bootstrap promotion decision; HEAD moves or stays.

The orchestrator itself is NOT a mutation target: the trust boundary is that
loop control, validation, sandboxing, and gating stay fixed while everything
inside `core/` evolves.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from .core.harness import HarnessSpec
from .core.harness_loader import HarnessLoader
from .core.registry import HarnessRegistry, Lineage
from .evaluation.gating import GateConfig, PromotionGate
from .evaluation.metrics import GenerationMetrics
from .mutation.engine import MutationEngine
from .mutation.validators import PatchValidator
from .sandbox.base import Sandbox, SandboxLimits
from .tracing.analyzer import TraceAnalyzer
from .tracing.collector import MemorySink, TraceCollector

logger = logging.getLogger(__name__)


def load_config(path: str | Path) -> dict[str, Any]:
    path = Path(path)
    config = yaml.safe_load(path.read_text())
    if "extends" in config:
        base = load_config(path.parent / config.pop("extends"))
        config = _deep_merge(base, config)
    return config


def _deep_merge(base: dict, override: dict) -> dict:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def build_model_call() -> Callable[[list[dict]], dict]:
    """Bind the task-executing model endpoint (Vertex AI in the GCP profile)."""

    def model_call(messages: list[dict]) -> dict:
        raise NotImplementedError(
            "Bind a model endpoint: Vertex AI GenerativeModel for the GCP "
            "profile, or a local server for offline iteration."
        )

    return model_call


def build_task_tools() -> dict[str, Callable[..., Any]]:
    """Tool registry for benchmark fixtures (filesystem, test runner, etc.)."""
    return {}


class Orchestrator:
    def __init__(
        self,
        config: dict[str, Any],
        mutation_engine: MutationEngine,
        sandbox: Sandbox,
        artifacts_dir: Path = Path("artifacts"),
    ):
        self.config = config
        self.registry = HarnessRegistry(artifacts_dir)
        self.loader = HarnessLoader(artifacts_dir)
        self.analyzer = TraceAnalyzer()
        self.validator = PatchValidator()
        self.mutation_engine = mutation_engine
        self.sandbox = sandbox
        promo = config["evaluation"]["promotion"]
        self.gate = PromotionGate(GateConfig(
            min_relative_gain=promo["min_relative_gain"],
            significance_alpha=promo["significance_alpha"],
            bootstrap_samples=promo["bootstrap_samples"],
        ))
        self._ensure_genesis(artifacts_dir)
        self._incumbent_metrics: GenerationMetrics | None = None

    def run(self) -> None:
        for generation in range(self.config["loop"]["generations"]):
            head = self.registry.current()
            logger.info("generation %d: HEAD=%s", generation, head)

            # 1-2. ACT + TELEMETRY
            sink = MemorySink()  # GCP profile: swap for BigQuerySink
            collector = TraceCollector(sink)
            self._run_training_batch(head, collector)

            # 3. ANALYZE
            report = self.analyzer.analyze(sink.events, harness_version=head)
            logger.info("analysis: %s", report.summary())

            # 4. MUTATE
            spec_json, source = self._read_artifact(head)
            patch = self.mutation_engine.propose(spec_json, source, sink.events, report)
            if patch is None:
                logger.info("generation %d: no mutation proposed", generation)
                continue

            # 5. VALIDATE
            if not self.validator.check(patch):
                logger.info("generation %d: patch rejected by static gates", generation)
                continue

            candidate = self.registry.register(
                patch.spec_json, patch.harness_source,
                Lineage(parent_version=head, generation=generation,
                        trace_batch_id=head, hypothesis=patch.hypothesis),
            )

            # 6. SANDBOX
            result = self.sandbox.evaluate(
                harness_version=candidate,
                artifacts_dir=str(self.registry.artifacts_dir),
                suite=self.config["evaluation"]["suite"],
                episodes_per_task=self.config["evaluation"]["episodes_per_task"],
                limits=SandboxLimits(**{
                    k: v for k, v in self.config["sandbox"].items()
                    if k in ("cpu", "memory", "wall_clock_s", "network")
                }),
            )
            if not result.completed:
                logger.info("candidate %s discarded: %s", candidate, result.violation)
                continue

            # 7. GATE
            candidate_metrics = GenerationMetrics(
                harness_version=candidate, per_task_scores=result.per_task_scores
            )
            incumbent_metrics = self._incumbent_metrics or self._baseline_metrics(head)
            if self.gate.should_promote(incumbent_metrics, candidate_metrics):
                self.registry.promote(candidate)
                self._incumbent_metrics = candidate_metrics
                logger.info(
                    "PROMOTED %s (hypothesis: %s)", candidate, patch.hypothesis
                )
            else:
                logger.info("candidate %s did not clear the promotion gate", candidate)

    # -- helpers ----------------------------------------------------------

    def _run_training_batch(self, head: str, collector: TraceCollector) -> None:
        from .core.agent import Agent
        from .evaluation.benchmarks.base import get_suite

        harness = self.loader.load(head, model_call=build_model_call())
        tools = build_task_tools()
        # Training tasks are drawn from a pool disjoint from the frozen
        # benchmark suite; the default reuses the suite prompts only as a
        # placeholder until a dedicated training pool is configured.
        for task in get_suite(self.config["evaluation"]["suite"]):
            for _ in range(self.config["loop"]["runs_per_generation"] // 3 or 1):
                Agent(
                    harness=harness, tools=tools, collector=collector,
                    max_steps=self.config["agent"]["max_steps"],
                ).run(task.prompt())

    def _baseline_metrics(self, head: str) -> GenerationMetrics:
        result = self.sandbox.evaluate(
            harness_version=head,
            artifacts_dir=str(self.registry.artifacts_dir),
            suite=self.config["evaluation"]["suite"],
            episodes_per_task=self.config["evaluation"]["episodes_per_task"],
            limits=SandboxLimits(),
        )
        metrics = GenerationMetrics(
            harness_version=head, per_task_scores=result.per_task_scores
        )
        self._incumbent_metrics = metrics
        return metrics

    def _read_artifact(self, version: str) -> tuple[str, str]:
        version_dir = self.registry.artifacts_dir / version
        spec_json = (version_dir / "spec.json").read_text()
        source_file = version_dir / "harness.py"
        source = source_file.read_text() if source_file.exists() else ""
        return spec_json, source

    def _ensure_genesis(self, artifacts_dir: Path) -> None:
        genesis = artifacts_dir / "genesis"
        if not genesis.exists():
            genesis.mkdir(parents=True)
            (genesis / "spec.json").write_text(HarnessSpec().model_dump_json(indent=2))
