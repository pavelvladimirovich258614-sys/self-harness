"""Top-level self-optimization loop.

One generation, end to end:

    1. ACT        — run the agent batch on TRAINING tasks under HEAD harness.
    2. TELEMETRY  — flush execution traces to the configured sink (BigQuery).
    3. ANALYZE    — failure clustering, call graphs, convergence stats.
    4. MUTATE     — engine reads whole traces + harness source, emits a patch
                    (up to loop.max_patch_attempts tries per generation).
    5. VALIDATE   — static gates + content-tabu (no re-evaluating an ancestor).
    6. SANDBOX    — frozen benchmark suite in an isolated GKE Job / container;
                    candidate AND incumbent evaluated under identical seed,
                    episode count, and limits in the same generation.
    7. GATE       — paired-bootstrap promotion decision; HEAD moves or stays.

Methodological firewall: training tasks (step 1) and frozen benchmark suites
(step 6) are disjoint pools; the mutation engine never observes benchmark
definitions or scores. Budget guard: every paid operation is pre-checked;
the loop aborts when the cumulative spend ceiling is hit.

The orchestrator itself is NOT a mutation target: loop control, validation,
sandboxing, gating, and budgeting stay fixed while everything inside `core/`
evolves.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable

import yaml

from .budget import BudgetConfig, BudgetGuard
from .core.harness import HarnessSpec
from .core.harness_loader import HarnessLoader
from .core.registry import HarnessRegistry, Lineage
from .evaluation.gating import GateConfig, PromotionGate
from .evaluation.metrics import GenerationMetrics
from .mutation.engine import HarnessPatch, MutationEngine
from .mutation.validators import PatchValidator
from .sandbox.base import Sandbox, SandboxLimits, SandboxResult
from .tracing.analyzer import TraceAnalyzer
from .tracing.collector import MemorySink, TraceCollector, TraceSink

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


def build_model_call(config: dict[str, Any] | None = None) -> Callable[[list[dict]], dict]:
    """Bind the task-executing model endpoint.

    `stub` — deterministic scripted model for end-to-end loop validation and
    CI: emits a tool call on the first turn and the termination keyword next.
    `vertex` — Vertex AI GenerativeModel binding (live endpoint).
    """
    model = (config or {}).get("agent", {}).get("model", "stub")

    if model == "stub":
        return _stub_model_call()

    if model == "vertex":
        gcp = (config or {}).get("gcp", {})

        def vertex_call(messages: list[dict]) -> dict:
            import vertexai
            from vertexai.generative_models import GenerativeModel

            vertexai.init(project=gcp.get("project_id"), location=gcp.get("location"))
            response = GenerativeModel(
                (config or {}).get("agent", {}).get("vertex_model", "gemini-2.5-flash")
            ).generate_content(str(messages))
            return {"content": response.text}

        return vertex_call

    raise ValueError(f"unknown agent.model binding: {model!r} (expected: stub | vertex)")


def _stub_model_call() -> Callable[[list[dict]], dict]:
    def stub(messages: list[dict]) -> dict:
        non_system = [m for m in messages if m.get("role") != "system"]
        if len(non_system) <= 1:
            return {"tool_call": {"name": "echo", "args": {"text": "probe"}}}
        return {"content": "TASK_COMPLETE"}

    return stub


def build_task_tools() -> dict[str, Callable[..., Any]]:
    """Tool registry for benchmark fixtures. The `echo` tool backs the stub
    model path; real fixtures (filesystem, test runner, rate-limited fetch)
    are mounted per-task inside the sandbox image."""
    return {"echo": lambda text="": {"echo": text}}


def build_trace_sink(config: dict[str, Any]) -> TraceSink:
    """Sink selection follows `tracing.sink`; the GCP profile streams to
    BigQuery, local profiles keep spans in memory."""
    if config.get("tracing", {}).get("sink") == "bigquery":
        from .tracing.bigquery_sink import BigQuerySink

        bq = config["tracing"]["bigquery"]
        return BigQuerySink(
            project_id=config["gcp"]["project_id"],
            dataset=bq["dataset"],
            table=bq["traces_table"],
        )
    return MemorySink()


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
        self.budget = BudgetGuard(BudgetConfig(**config.get("budget", {})))
        self.limits = SandboxLimits(**{
            k: v for k, v in config["sandbox"].items()
            if k in ("cpu", "memory", "wall_clock_s", "network")
        })
        self.seed = config["evaluation"].get("seed", 0)
        self._ensure_genesis(artifacts_dir)

    def run(self) -> None:
        for generation in range(self.config["loop"]["generations"]):
            if self.budget.exhausted:
                logger.error("loop aborted at generation %d: budget exhausted", generation)
                break
            self.budget.start_generation()
            head = self.registry.current()
            logger.info("generation %d: HEAD=%s", generation, head)

            # 1-2. ACT + TELEMETRY — training pool only, never the frozen suite.
            sink = build_trace_sink(self.config)
            local_events = MemorySink()  # In-process copy for analysis + mutation
            collector = TraceCollector(_TeeSink(sink, local_events))
            self._run_training_batch(head, collector)

            # 3. ANALYZE
            report = self.analyzer.analyze(local_events.events, harness_version=head)
            logger.info("analysis: %s", report.summary())

            # 4-5. MUTATE + VALIDATE, with bounded retries.
            patch = self._propose_valid_patch(head, local_events.events, report)
            if patch is None:
                continue

            candidate = self.registry.register(
                patch.spec_json, patch.harness_source,
                Lineage(parent_version=head, generation=generation,
                        trace_batch_id=head, hypothesis=patch.hypothesis),
            )

            # 6. SANDBOX — candidate and incumbent under matched conditions,
            # both freshly evaluated this generation (no stale baselines).
            candidate_result = self._sandbox_eval(candidate)
            if candidate_result is None or not candidate_result.completed:
                if candidate_result is not None:
                    logger.info("candidate %s discarded: %s", candidate, candidate_result.violation)
                continue
            incumbent_result = self._sandbox_eval(head)
            if incumbent_result is None or not incumbent_result.completed:
                logger.warning("incumbent re-evaluation failed; skipping gate this generation")
                continue

            # 7. GATE
            candidate_metrics = GenerationMetrics(
                harness_version=candidate,
                per_task_scores=candidate_result.per_task_scores,
                estimated_cost_usd=self.budget.generation_usd,
            )
            incumbent_metrics = GenerationMetrics(
                harness_version=head, per_task_scores=incumbent_result.per_task_scores
            )
            if self.gate.should_promote(incumbent_metrics, candidate_metrics):
                self.registry.promote(candidate)
                logger.info("PROMOTED %s (hypothesis: %s)", candidate, patch.hypothesis)
            else:
                logger.info("candidate %s did not clear the promotion gate", candidate)

        logger.info("loop finished: HEAD=%s, total spend %.2f USD",
                    self.registry.current(), self.budget.total_usd)

    # -- helpers ----------------------------------------------------------

    def _propose_valid_patch(self, head, events, report) -> HarnessPatch | None:
        spec_json, source = self._read_artifact(head)
        request_chars = len(spec_json) + len(source) + sum(
            len(str(e.payload)) for e in events
        )
        max_attempts = self.config["loop"].get("max_patch_attempts", 1)
        for attempt in range(max_attempts):
            if not self.budget.allow_mutation_call(request_chars):
                return None
            patch = self.mutation_engine.propose(spec_json, source, events, report)
            self.budget.record_mutation_call(
                request_chars,
                len(patch.harness_source) + len(patch.spec_json) if patch else 0,
            )
            if patch is None:
                logger.info("no mutation proposed (attempt %d)", attempt + 1)
                return None
            digest = self.registry.content_digest(patch.spec_json, patch.harness_source)
            if self.registry.has_content(digest):
                logger.info("patch rejected: content tabu (oscillation guard), attempt %d", attempt + 1)
                continue
            if self.validator.check(patch):
                return patch
            logger.info("patch rejected by static gates (attempt %d)", attempt + 1)
        return None

    def _sandbox_eval(self, version: str) -> SandboxResult | None:
        if not self.budget.allow_sandbox_run():
            return None
        self.budget.record_sandbox_run()
        return self.sandbox.evaluate(
            harness_version=version,
            artifacts_dir=str(self.registry.artifacts_dir),
            suite=self.config["evaluation"]["suite"],
            episodes_per_task=self.config["evaluation"]["episodes_per_task"],
            limits=self.limits,
            seed=self.seed,
        )

    def _run_training_batch(self, head: str, collector: TraceCollector) -> None:
        from .core.agent import Agent
        from .evaluation.benchmarks.base import get_suite

        harness = self.loader.load(head, model_call=build_model_call(self.config))
        tools = build_task_tools()
        training_suite = self.config.get("training", {}).get("suite", "training")
        for task in get_suite(training_suite):
            for _ in range(self.config["loop"]["runs_per_generation"] // 3 or 1):
                Agent(
                    harness=harness, tools=tools, collector=collector,
                    max_steps=self.config["agent"]["max_steps"],
                    payload_max_chars=self.config["agent"].get(
                        "trace_payload_max_chars", 65_536
                    ),
                ).run(task.prompt())
        collector.flush()

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


class _TeeSink:
    """Duplicate spans to the durable sink (BigQuery) and an in-process copy
    used for same-generation analysis and mutation prompting."""

    def __init__(self, primary: TraceSink, secondary: MemorySink):
        self.primary = primary
        self.secondary = secondary

    def write(self, events) -> None:
        self.primary.write(events)
        self.secondary.write(events)
