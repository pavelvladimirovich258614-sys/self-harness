#!/usr/bin/env python3
"""Long-context "needle in a haystack" benchmark for the Pro mutation engine.

Validates the central grant hypothesis (README §KPIs, K4 — context
dependency): a frontier long-context model can locate a single critical defect
buried in a ~500K-token execution trace and turn it into a falsifiable repair
hypothesis, and that ability degrades measurably as the trace context is
truncated.

Pipeline per run:

    1. Synthesize a giant trace (500+ steps, ~500K tokens of realistic spans).
    2. Inject ONE hidden critical error (the needle) at a configurable depth.
    3. Initialize the mutation engine for a Gemini Pro configuration.
    4. Feed the trace, measure latency, extract the hypothesis, check whether
       the needle was found, and estimate the API cost of the call.

Backends:
    --engine vertex   live Vertex AI Gemini Pro (requires GCP credentials)
    --engine mock     offline simulation that mirrors the engine's drop-oldest
                      truncation, so the ablation is demonstrable without GCP

Context ablation:
    --context-fraction f   caps the engine's trace budget at f × target tokens.
                           At f < (needle depth) the needle falls outside the
                           retained window and detection is expected to fail —
                           which is exactly the degradation curve K4 measures.

Pricing note: per-token rates are CLI flags. The defaults are placeholders and
MUST be confirmed against current Vertex AI pricing before any figure derived
here is quoted in the grant report.
"""

from __future__ import annotations

import argparse
import json
import logging
import random
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from self_harness.mutation.engine import HarnessPatch, MutationEngine
from self_harness.tracing.analyzer import AnalysisReport
from self_harness.tracing.schema import SpanKind, TraceEvent

logger = logging.getLogger("benchmark_pro_context")

CHARS_PER_TOKEN = 4  # Matches the engine's budgeting heuristic.

# The needle: one semantically distinctive critical defect. Detection is scored
# on whether the engine's hypothesis/rationale references these concepts.
NEEDLE_TOOL = "auth_service"
NEEDLE_ERROR = (
    "CRITICAL: auth_service silently truncated the bearer token to 8 chars; "
    "every downstream request after this step returns 403 but the harness "
    "retries instead of surfacing the auth failure"
)
NEEDLE_KEYWORDS = ["auth", "token", "truncat", "403"]
NEEDLE_DETECTION_THRESHOLD = 2  # distinct keywords required to count as "found"

# Filler vocabulary for realistic, incompressible-ish span payloads.
_FILLER_TOOLS = ["web_search", "read_file", "write_file", "run_tests",
                 "grep", "list_dir", "http_get", "parse_json", "db_query"]
_FILLER_WORDS = (
    "request response handler retry latency payload schema validate parse "
    "token cache miss hit timeout connection pool worker queue dispatch "
    "serialize deserialize index offset chunk buffer stream commit rollback"
).split()


# --------------------------------------------------------------------------- #
# Synthetic trace generation
# --------------------------------------------------------------------------- #

@dataclass
class NeedleSpec:
    run_id: str
    step: int
    char_offset_estimate: int


def _filler_blob(rng: random.Random, approx_chars: int) -> str:
    """Build a pseudo-log blob of roughly approx_chars characters."""
    out: list[str] = []
    size = 0
    while size < approx_chars:
        line = " ".join(rng.choice(_FILLER_WORDS) for _ in range(rng.randint(8, 16)))
        fragment = f'{{"lvl":"INFO","msg":"{line}","seq":{rng.randint(0, 1_000_000)}}}'
        out.append(fragment)
        size += len(fragment) + 1
    return "\n".join(out)


def synthesize_trace(
    target_tokens: int,
    steps: int,
    needle_depth: float,
    seed: int,
) -> tuple[list[TraceEvent], NeedleSpec]:
    """Generate a ~target_tokens trace of `steps` spans with the needle planted
    at `needle_depth` (0..1) of the way through."""
    rng = random.Random(seed)
    target_chars = target_tokens * CHARS_PER_TOKEN
    per_step_chars = max(256, target_chars // max(steps, 1))
    needle_step = max(1, int(steps * needle_depth))
    run_id = f"bench-{seed:06d}"

    events: list[TraceEvent] = [
        TraceEvent(run_id=run_id, harness_version="genesis", kind=SpanKind.RUN_START,
                   step=0, payload={"task": "long-horizon integration repair"})
    ]

    running_chars = 0
    needle_offset = 0
    for step in range(1, steps + 1):
        if step == needle_step:
            payload = {
                "tool": NEEDLE_TOOL,
                "args": {"endpoint": "/v2/refresh"},
                "result_preview": NEEDLE_ERROR,
            }
            kind = SpanKind.TOOL_CALL
            needle_offset = running_chars
        else:
            tool = rng.choice(_FILLER_TOOLS)
            payload = {
                "tool": tool,
                "args": {"q": rng.choice(_FILLER_WORDS), "n": rng.randint(1, 50)},
                "result_preview": _filler_blob(rng, per_step_chars),
            }
            kind = SpanKind.TOOL_CALL
        ev = TraceEvent(run_id=run_id, harness_version="genesis", kind=kind,
                        step=step, duration_s=round(rng.uniform(0.1, 4.0), 2),
                        payload=payload)
        events.append(ev)
        running_chars += len(json.dumps(payload, default=str))

    events.append(
        TraceEvent(run_id=run_id, harness_version="genesis", kind=SpanKind.RUN_END,
                   step=steps + 1, payload={"success": False, "steps": steps})
    )
    return events, NeedleSpec(run_id=run_id, step=needle_step,
                              char_offset_estimate=needle_offset)


# --------------------------------------------------------------------------- #
# Truncation model (shared by the mock backend and the ablation report)
# --------------------------------------------------------------------------- #

def serialize_with_budget(events: list[TraceEvent], max_trace_tokens: int) -> tuple[str, set[int]]:
    """Mirror VertexGeminiMutationEngine._serialize_traces: keep the most
    recent spans, dropping oldest first, until the token budget is hit.
    Returns the serialized text and the set of retained step indices."""
    lines = [
        (e.step, json.dumps(
            {"run": e.run_id, "step": e.step, "kind": e.kind.value,
             "dt": round(e.duration_s, 2), "payload": e.payload}, default=str))
        for e in events
    ]
    budget_chars = max_trace_tokens * CHARS_PER_TOKEN
    kept: list[tuple[int, str]] = []
    used = 0
    for step, line in reversed(lines):
        if used + len(line) > budget_chars:
            break
        kept.append((step, line))
        used += len(line) + 1
    kept.reverse()
    return "\n".join(line for _, line in kept), {s for s, _ in kept}


# --------------------------------------------------------------------------- #
# Mock Pro backend (offline)
# --------------------------------------------------------------------------- #

class MockProEngine(MutationEngine):
    """Offline stand-in that simulates long-context behavior: it "reads" the
    same retained window the real engine would, finds the needle only if the
    needle span survived truncation, and reports plausible latency/usage."""

    def __init__(self, max_trace_tokens: int, needle: NeedleSpec, seed: int = 0):
        self.max_trace_tokens = max_trace_tokens
        self.needle = needle
        self._rng = random.Random(seed)
        self.last_input_tokens = 0
        self.last_output_tokens = 0

    def propose(self, current_spec_json, current_source, traces, report) -> HarnessPatch | None:
        serialized, kept_steps = serialize_with_budget(traces, self.max_trace_tokens)
        self.last_input_tokens = len(serialized) // CHARS_PER_TOKEN
        # Simulate first-token + streaming latency proportional to context size.
        time.sleep(min(2.0, self.last_input_tokens / 1_000_000))
        found = self.needle.step in kept_steps
        if found:
            patch = HarnessPatch(
                hypothesis=("auth_service truncates the bearer token, so the harness "
                            "must surface 403s instead of retrying"),
                rationale=(f"run {self.needle.run_id} step {self.needle.step}: "
                           "token truncated to 8 chars, all subsequent requests 403"),
                spec_json='{"version":"v-mock","max_retries":1}',
                harness_source="class Harness:\n    pass\n",
            )
        else:
            patch = HarnessPatch(
                hypothesis="increase retry backoff to smooth transient tool errors",
                rationale="no single root cause located in the retained context window",
                spec_json='{"version":"v-mock","max_retries":5}',
                harness_source="class Harness:\n    pass\n",
            )
        self.last_output_tokens = (
            len(patch.hypothesis) + len(patch.rationale)
            + len(patch.spec_json) + len(patch.harness_source)
        ) // CHARS_PER_TOKEN
        return patch


# --------------------------------------------------------------------------- #
# Detection + cost
# --------------------------------------------------------------------------- #

def needle_found(patch: HarnessPatch | None) -> tuple[bool, list[str]]:
    if patch is None:
        return False, []
    text = f"{patch.hypothesis} {patch.rationale}".lower()
    hits = [kw for kw in NEEDLE_KEYWORDS if kw in text]
    return len(hits) >= NEEDLE_DETECTION_THRESHOLD, hits


def estimate_cost_usd(
    input_tokens: int,
    output_tokens: int,
    input_usd_per_mtoken: float,
    output_usd_per_mtoken: float,
    long_context_threshold: int,
    long_context_multiplier: float,
) -> float:
    in_rate = input_usd_per_mtoken
    out_rate = output_usd_per_mtoken
    if input_tokens > long_context_threshold:
        in_rate *= long_context_multiplier
        out_rate *= long_context_multiplier
    return input_tokens / 1e6 * in_rate + output_tokens / 1e6 * out_rate


# --------------------------------------------------------------------------- #
# Report
# --------------------------------------------------------------------------- #

@dataclass
class BenchmarkReport:
    engine: str
    model: str
    target_tokens: int
    steps: int
    needle_step: int
    context_fraction: float
    trace_tokens_total: int
    input_tokens: int
    output_tokens: int
    latency_s: float
    needle_found: bool
    keyword_hits: list[str] = field(default_factory=list)
    hypothesis: str = ""
    estimated_cost_usd: float = 0.0

    def render(self) -> str:
        status = "FOUND ✅" if self.needle_found else "MISSED ❌"
        return (
            "\n=== Long-Context Needle Benchmark ===\n"
            f"engine            : {self.engine} ({self.model})\n"
            f"trace             : {self.steps} steps, ~{self.trace_tokens_total:,} tokens total\n"
            f"needle depth      : step {self.needle_step}\n"
            f"context fraction  : {self.context_fraction:.2f} "
            f"({self.input_tokens:,} input tokens fed)\n"
            f"latency           : {self.latency_s:.2f} s\n"
            f"needle detection  : {status}  (keywords: {self.keyword_hits})\n"
            f"hypothesis        : {self.hypothesis}\n"
            f"output tokens     : {self.output_tokens:,}\n"
            f"estimated cost    : ${self.estimated_cost_usd:.4f}\n"
            "======================================\n"
        )


# --------------------------------------------------------------------------- #
# Engine wiring + main
# --------------------------------------------------------------------------- #

def build_engine(args, max_trace_tokens: int, needle: NeedleSpec) -> MutationEngine:
    if args.engine == "vertex":
        from self_harness.mutation.vertex_gemini import VertexGeminiMutationEngine

        return VertexGeminiMutationEngine(
            project_id=args.project,
            location=args.location,
            model=args.model,
            max_trace_tokens=max_trace_tokens,
            temperature=args.temperature,
        )
    return MockProEngine(max_trace_tokens=max_trace_tokens, needle=needle, seed=args.seed)


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--engine", choices=["vertex", "mock"], default="mock")
    p.add_argument("--model", default="gemini-2.5-pro",
                   help="Pro model id; override to match the grant's target model")
    p.add_argument("--project", default="", help="GCP project (vertex engine)")
    p.add_argument("--location", default="us-central1")
    p.add_argument("--temperature", type=float, default=0.4)

    p.add_argument("--target-tokens", type=int, default=500_000)
    p.add_argument("--steps", type=int, default=520)
    p.add_argument("--needle-depth", type=float, default=0.5,
                   help="Fractional position of the needle in the trace (0..1)")
    p.add_argument("--context-fraction", type=float, default=1.0,
                   help="Fraction of target tokens the engine is allowed to read")
    p.add_argument("--seed", type=int, default=1337)

    # Pricing (placeholders — confirm against current Vertex AI pricing).
    p.add_argument("--input-usd-per-mtoken", type=float, default=1.25)
    p.add_argument("--output-usd-per-mtoken", type=float, default=5.00)
    p.add_argument("--long-context-threshold", type=int, default=200_000)
    p.add_argument("--long-context-multiplier", type=float, default=2.0)

    p.add_argument("--output", default="", help="Optional path to write the JSON report")
    return p.parse_args()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
    args = parse_args()

    events, needle = synthesize_trace(
        target_tokens=args.target_tokens, steps=args.steps,
        needle_depth=args.needle_depth, seed=args.seed,
    )
    trace_tokens_total = sum(len(json.dumps(e.payload, default=str)) for e in events) // CHARS_PER_TOKEN
    max_trace_tokens = max(1, int(args.target_tokens * args.context_fraction))
    logger.info("synthesized %d spans (~%d tokens); needle at step %d; budget %d tokens (%.0f%%)",
                len(events), trace_tokens_total, needle.step, max_trace_tokens,
                args.context_fraction * 100)

    engine = build_engine(args, max_trace_tokens, needle)

    t0 = time.perf_counter()
    patch = engine.propose(
        current_spec_json="{}",
        current_source="class Harness: ...",
        traces=events,
        report=AnalysisReport(harness_version="genesis"),
    )
    latency_s = time.perf_counter() - t0

    found, hits = needle_found(patch)

    # Token accounting: prefer engine-reported usage (mock), else estimate.
    input_tokens = getattr(engine, "last_input_tokens", 0) or _estimate_input_tokens(
        events, max_trace_tokens)
    output_tokens = getattr(engine, "last_output_tokens", 0) or _estimate_output_tokens(patch)

    cost = estimate_cost_usd(
        input_tokens, output_tokens,
        args.input_usd_per_mtoken, args.output_usd_per_mtoken,
        args.long_context_threshold, args.long_context_multiplier,
    )

    report = BenchmarkReport(
        engine=args.engine, model=args.model,
        target_tokens=args.target_tokens, steps=args.steps,
        needle_step=needle.step, context_fraction=args.context_fraction,
        trace_tokens_total=trace_tokens_total,
        input_tokens=input_tokens, output_tokens=output_tokens,
        latency_s=round(latency_s, 3), needle_found=found, keyword_hits=hits,
        hypothesis=(patch.hypothesis if patch else ""),
        estimated_cost_usd=round(cost, 6),
    )

    print(report.render())
    if args.output:
        Path(args.output).write_text(json.dumps(asdict(report), indent=2))
        logger.info("report written to %s", args.output)


def _estimate_input_tokens(events: list[TraceEvent], max_trace_tokens: int) -> int:
    serialized, _ = serialize_with_budget(events, max_trace_tokens)
    return len(serialized) // CHARS_PER_TOKEN


def _estimate_output_tokens(patch: HarnessPatch | None) -> int:
    if patch is None:
        return 0
    return (len(patch.hypothesis) + len(patch.rationale)
            + len(patch.spec_json) + len(patch.harness_source)) // CHARS_PER_TOKEN


if __name__ == "__main__":
    main()
