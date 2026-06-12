# self-harness

> **The agent harness as a learnable artifact.**
> Orchestration code, prompts, tools, and control logic around an LLM are not static —
> they are mutable, versioned, benchmarked artifacts rewritten by the system itself
> from its own execution traces.

Reference concept: [arxiv.org/abs/2606.09498](https://arxiv.org/abs/2606.09498)

---

```text
┌──────────────────────────────────────────────────────────────────────────┐
│                       SELF-OPTIMIZATION LOOP                             │
│                                                                          │
│   ┌─────────┐    traces    ┌──────────────┐   analysis   ┌────────────┐  │
│   │  Agent  │─────────────▶│   Telemetry  │─────────────▶│  Mutation  │  │
│   │ (run N) │              │  (BigQuery)  │              │   Engine   │  │
│   └────▲────┘              └──────────────┘              │ (Gemini)   │  │
│        │                                                 └─────┬──────┘  │
│        │ promote                                               │ patch   │
│   ┌────┴─────────┐   pass/fail   ┌───────────────┐   candidate │         │
│   │  Evaluation  │◀──────────────│    Sandbox    │◀────────────┘         │
│   │   Gateway    │               │  (GKE / OCI)  │                       │
│   └──────────────┘               └───────────────┘                       │
│                                                                          │
│   Agent (run N+1) executes with harness version H+1                      │
└──────────────────────────────────────────────────────────────────────────┘
```

---

## Thesis

Modern agent frameworks are written once and maintained by hand, while the
models inside them improve monthly. The harness becomes the bottleneck.
`self-harness` inverts the relationship: the harness — system prompts, tool
schemas, retry policies, context-management code, control flow — is treated
as a **versioned, benchmarked, mutable artifact**. After each batch of
long-horizon runs, the system analyzes its own execution traces and proposes
patches to its own orchestration layer. Patches are executed in an isolated
sandbox, scored against a frozen benchmark suite, and promoted only when they
demonstrate statistically significant improvement.

The agent accumulates not only knowledge and results, but **improvements to
its own architecture**.

---

## Why Google Cloud

The central hypothesis: harness self-optimization is bottlenecked by the
ability to reason over *very long* execution histories. A single long-horizon
agent run produces hundreds of thousands of tokens of traces — tool calls,
intermediate reasoning, failures, retries. Effective mutation requires a model
that ingests **entire run histories, not summaries**.

| Requirement                              | GCP Service              | Why it is load-bearing                                                            |
| ---------------------------------------- | ------------------------ | --------------------------------------------------------------------------------- |
| Whole-trace analysis (no lossy chunking)  | **Gemini on Vertex AI**  | 1M–2M token context window fits multiple complete long-horizon traces per request  |
| Safe execution of self-modified code      | **GKE**                  | Per-mutation ephemeral namespaces, gVisor isolation, hard resource quotas          |
| Trace storage, convergence analytics      | **BigQuery**             | SQL over millions of tool-call events; call-graph extraction; regression queries   |
| Harness artifact versioning               | **Cloud Storage / AR**   | Immutable, content-addressed harness versions with full lineage                    |

Mutation quality measurably degrades when traces are truncated or summarized —
the failure signal that motivates a patch is frequently buried mid-trace.
This makes the long-context mutation engine a *requirement*, not an
optimization, and makes Vertex AI the natural substrate for the research.

---

## Repository Layout

```text
self-harness/
├── README.md
├── pyproject.toml
├── .gitignore
├── configs/
│   ├── default.yaml              # Loop hyperparameters, model ids, thresholds
│   └── gcp.yaml                  # Project / region / dataset / cluster bindings
├── src/self_harness/
│   ├── __init__.py
│   ├── orchestrator.py           # Top-level self-optimization loop driver
│   ├── core/                     # The mutable subject: agent + dynamic harness
│   │   ├── agent.py              # Model-facing agent; harness-agnostic
│   │   ├── harness.py            # Harness artifact: prompts, tools, control flow
│   │   ├── harness_loader.py     # Dynamic (re)loading of harness versions
│   │   └── registry.py           # Versioned harness artifact store + lineage
│   ├── sandbox/                  # Isolated execution of mutated harnesses
│   │   ├── base.py               # Sandbox interface + execution result contract
│   │   ├── local_docker.py       # OCI-container sandbox for local iteration
│   │   └── gke_sandbox.py        # GKE Jobs: ephemeral namespace per mutation
│   ├── tracing/                  # Execution-trace capture and analysis
│   │   ├── schema.py             # Canonical trace event model (spans, tool calls)
│   │   ├── collector.py          # In-process trace recorder
│   │   ├── bigquery_sink.py      # Streaming export of traces to BigQuery
│   │   └── analyzer.py           # Failure clustering, call-graph & convergence stats
│   ├── mutation/                 # The mutation engine
│   │   ├── engine.py             # MutationEngine interface + patch contract
│   │   ├── vertex_gemini.py      # Gemini long-context trace→patch generation
│   │   ├── prompts.py            # Meta-prompts for self-analysis and rewriting
│   │   └── validators.py         # Static safety gates on generated patches
│   └── evaluation/               # Benchmark pipeline and promotion gateway
│       ├── pipeline.py           # Candidate-vs-incumbent evaluation runner
│       ├── metrics.py            # Success rate, cost, convergence metrics
│       ├── gating.py             # Statistical promotion / rollback policy
│       └── benchmarks/
│           ├── base.py           # Benchmark task interface
│           └── longhorizon.py    # Long-horizon multi-step task suite
├── infra/
│   ├── bigquery/traces_schema.json
│   └── gke/sandbox-job.yaml      # Hardened Job template for mutated harnesses
├── scripts/
│   └── run_loop.py               # Entry point: run K generations of the loop
└── tests/
    ├── test_harness_loader.py
    └── test_gating.py
```

---

## Module Responsibilities

### `core/` — the mutable subject

The only part of the system the mutation engine is allowed to rewrite.
`Harness` bundles everything traditionally hard-coded around a model: system
prompt, tool definitions, context-management policy, retry/termination logic.
`HarnessLoader` loads any registered harness version at runtime, so the agent
of generation *N+1* boots with the artifact produced by generation *N*.
`registry.py` keeps every version immutable and traceable to the trace batch
and patch that produced it — full lineage, full rollback.

### `tracing/` — the sensory system

Every model call, tool invocation, error, and token count is recorded as a
structured span (`schema.py`) and streamed to BigQuery (`bigquery_sink.py`).
`analyzer.py` runs SQL-level analytics: failure clustering, tool-calling
graph extraction, convergence curves across harness generations. Its output —
plus the raw traces themselves — is the input to the mutation engine.

### `mutation/` — the learning rule

`vertex_gemini.py` feeds *complete* execution traces (not summaries) into
Gemini's long context together with the current harness source code and asks
for a unified-diff patch plus a falsifiable hypothesis ("retry policy masks
tool schema errors; tighten schema, drop retries from 5 to 2").
`validators.py` statically rejects patches that touch anything outside
`core/`, import forbidden modules, or break the harness contract.

### `sandbox/` — the immune system

Mutated code never runs in the orchestrator process. `gke_sandbox.py` launches
each candidate harness as a GKE Job in an ephemeral namespace with gVisor,
no service-account token, deny-by-default egress, and hard CPU/memory/time
quotas. `local_docker.py` provides the same contract for laptop-scale
iteration. A candidate that escapes, hangs, or crashes is simply discarded.

### `evaluation/` — the selection pressure

`pipeline.py` runs candidate and incumbent harnesses on a frozen benchmark
suite under identical seeds and budgets. `gating.py` promotes a candidate only
on statistically significant improvement (paired bootstrap over per-task
scores) and demotes automatically on production regression. This prevents the
classic self-modification failure mode: drift toward harnesses that overfit
the mutation engine's preferences rather than task performance.

---

## The Loop, End to End

```python
for generation in range(cfg.generations):
    traces   = run_agent_batch(harness=registry.current(), tasks=train_tasks)   # 1. act
    sink.flush(traces)                                                          # 2. telemetry → BigQuery
    report   = analyzer.analyze(generation)                                     # 3. SQL analytics
    patch    = mutation_engine.propose(harness_src, traces, report)             # 4. Gemini rewrites the harness
    if not validators.check(patch): continue                                    # 5. static safety gates
    result   = sandbox.evaluate(patch, suite=frozen_benchmarks)                 # 6. isolated GKE evaluation
    if gating.should_promote(result):                                           # 7. statistical promotion
        registry.promote(patch, lineage=(generation, report.id))
```

---

## Quickstart

```bash
pip install -e ".[dev]"

# Local iteration (Docker sandbox, in-memory trace sink)
python scripts/run_loop.py --config configs/default.yaml

# GCP-native (BigQuery telemetry, Vertex mutation engine, GKE sandbox)
gcloud auth application-default login
python scripts/run_loop.py --config configs/gcp.yaml
```

---

## Research Questions

1. **Convergence** — does harness fitness improve monotonically across
   generations, or does it oscillate / collapse without human curation?
2. **Context-length ablation** — how does mutation quality degrade as traces
   are truncated from 1M tokens down to summary scale?
3. **Transfer** — do harness improvements learned on one task family transfer
   to unseen families, or is self-optimization inherently overfit-prone?
4. **Safety envelope** — what fraction of generated patches are rejected by
   static gates vs. sandbox failures vs. statistical gating, and how does that
   distribution shift over generations?
