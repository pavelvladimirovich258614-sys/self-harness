# self-harness

[![CI/CD: 60 Tests Passing](https://img.shields.io/badge/CI%2FCD-60_Tests_Passing-success)](#)
[![Security: Active Defense](https://img.shields.io/badge/Security-Active_Defense-blue)](THREAT_MODEL.md)
[![Sandbox: gVisor](https://img.shields.io/badge/Sandbox-gVisor_Isolated-informational)](THREAT_MODEL.md)

> **The agent harness as a learnable artifact.**
> Orchestration code, prompts, tools, and control logic around an LLM are not static —
> they are mutable, versioned, benchmarked artifacts rewritten by the system itself
> from its own execution traces.

Reference concept: [arxiv.org/abs/2606.09498](https://arxiv.org/abs/2606.09498)

---

## Enterprise-Grade Security (Active Defense)

Self-modifying agents are only fundable if the modification path is provably
contained. `self-harness` treats security as a first-class subsystem, not an
afterthought:

- **Active Defense / Agent SIEM.** The mutation engine doubles as an
  injection sensor. When it detects an indirect prompt injection in the trace
  corpus, the orchestrator **fast-fails** the candidate *before* static gates,
  registration, or sandbox execution, and streams a `SECURITY_ALERT` to
  **BigQuery** — the same table that holds execution traces becomes the agent
  SIEM (`infra/bigquery/queries/security_incidents.sql`). Hostile code is
  never persisted; the loop drops the poisoned generation and continues.
- **gVisor sandbox as the trust boundary.** All LLM-generated harness code
  runs inside ephemeral **GKE** Jobs under a gVisor runtime class, with zero
  credentials (`automountServiceAccountToken: false`), deny-all egress except
  cluster DNS and the model endpoint, read-only root filesystem, and hard
  CPU/memory/wall-clock quotas.
- **Static gates as a cost filter.** AST screening rejects forbidden imports
  and dynamic-execution primitives (`eval`/`exec`/`compile`/`__import__`)
  up front — explicitly *not* relied on as the security boundary.
- **Statistical promotion + human-in-the-loop.** A candidate replaces the
  incumbent only on a significant paired-bootstrap improvement; promotion to
  any externally connected agent requires human review of the diff and full
  lineage.

Full analysis: [`THREAT_MODEL.md`](THREAT_MODEL.md) (boundaries B1–B3, threats
T1–T6, Active Defense pipeline).

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
| Population-scale candidate evaluation     | **GKE**                  | K candidates per generation in K parallel ephemeral namespaces; gVisor isolation; GPU node pools for open-weight executors |
| Trace analytics at corpus scale           | **BigQuery**             | The analytics in `infra/bigquery/queries/` (failure clustering, tool-call graphs, retry-storm detection, convergence curves) as SQL over millions of span rows |
| Harness artifact versioning               | **Cloud Storage / AR**   | Immutable, content-addressed harness versions with full lineage                    |

Two of these are scale arguments with concrete shapes:

- **Long context is a correctness requirement, not a convenience.** The
  failure signal that motivates a patch is frequently a single anomalous tool
  result buried mid-trace; it does not survive summarization. The
  context-length ablation (matrix §1.1) measures exactly this.
- **GKE is a parallelism requirement.** The loop's sample efficiency scales
  with the number of candidate patches evaluated per generation
  (select-best-of-K instead of accept/reject-one). K parallel sandboxes ×
  hardened isolation × GPU serving for 27B-class open-weight executors is a
  cluster workload, not a workstation one.

---

## Repository Layout

```text
self-harness/
├── README.md
├── THREAT_MODEL.md               # Trust boundaries, threats T1-T6, residual risks
├── MODEL_TESTING_MATRIX.md       # Multi-model integration & testing plans
├── pyproject.toml
├── configs/
│   ├── default.yaml              # Loop hyperparameters, budget caps, thresholds
│   └── gcp.yaml                  # Project / region / dataset / cluster bindings
├── src/self_harness/
│   ├── orchestrator.py           # Self-optimization loop driver + budget kill switch
│   ├── budget.py                 # Spend guard: token caps, USD ceiling, run caps
│   ├── core/                     # The mutable subject: agent + dynamic harness
│   │   ├── agent.py              # Model-facing agent; harness-agnostic
│   │   ├── harness.py            # Harness artifact: prompts, tools, control flow
│   │   ├── harness_loader.py     # Dynamic (re)loading of harness versions
│   │   └── registry.py           # Versioned store + lineage + oscillation tabu
│   ├── sandbox/                  # Isolated execution of mutated harnesses
│   │   ├── base.py               # Sandbox interface + matched-conditions contract
│   │   ├── local_docker.py       # OCI-container sandbox for local iteration
│   │   └── gke_sandbox.py        # GKE Jobs: ephemeral namespace per mutation
│   ├── tracing/                  # Execution-trace capture and analysis
│   │   ├── schema.py             # Canonical trace event model (spans, tool calls)
│   │   ├── collector.py          # In-process trace recorder
│   │   ├── bigquery_sink.py      # Streaming export w/ retries + dead-letter
│   │   ├── analyzer.py           # In-memory analytics (single-generation scale)
│   │   └── bigquery_analyzer.py  # Same analytics as SQL at corpus scale
│   ├── mutation/                 # The mutation engine
│   │   ├── engine.py             # MutationEngine interface + patch contract
│   │   ├── vertex_gemini.py      # Gemini long-context trace→patch generation
│   │   ├── prompts.py            # Meta-prompts for self-analysis and rewriting
│   │   └── validators.py         # Static cost-filter gates (see THREAT_MODEL.md)
│   └── evaluation/               # Benchmark pipeline and promotion gateway
│       ├── pipeline.py           # In-sandbox runner; seeded, matched conditions
│       ├── metrics.py            # Success rate, cost, convergence metrics
│       ├── gating.py             # Statistical promotion / rollback policy
│       └── benchmarks/
│           ├── base.py           # Task interface + suite registry
│           ├── longhorizon.py    # FROZEN evaluation suite
│           └── training_pool.py  # Training tasks — disjoint from frozen suites
├── infra/
│   ├── bigquery/
│   │   ├── traces_schema.json
│   │   └── queries/              # Failure clusters, call graphs, retry storms,
│   │                             # convergence curves — the SQL evidence base
│   └── gke/sandbox-job.yaml      # Hardened Job + layered NetworkPolicies
├── scripts/
│   └── run_loop.py               # Entry point: run K generations of the loop
└── tests/                        # Loader, gating, validators, suite separation,
                                  # and the in-process end-to-end loop tests
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
structured span (`schema.py`) and streamed to BigQuery (`bigquery_sink.py`)
with bounded retries and a local dead-letter file — telemetry is
at-least-once, never silently dropped. Analytics exist at two scales:
`analyzer.py` aggregates in-process for single-generation runs, and
`bigquery_analyzer.py` runs the parameterized SQL in
`infra/bigquery/queries/` (failure clustering, tool-call transition graphs,
retry-storm detection, convergence curves) over the full span corpus. The
report — plus the raw traces themselves — is the input to the mutation
engine.

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
    budget.start_generation()                                                   # 0. spend guard / kill switch
    traces    = run_agent_batch(harness=registry.current(), tasks=TRAINING_POOL)  # 1. act (never on frozen suites)
    sink.flush(traces)                                                          # 2. telemetry → BigQuery (at-least-once)
    report    = analyzer.analyze(generation)                                    # 3. SQL analytics
    patch     = mutation_engine.propose(harness_src, traces, report)            # 4. Gemini rewrites the harness
    if not validators.check(patch) or registry.has_content(digest(patch)):      # 5. static gates + oscillation tabu
        continue
    cand, inc = sandbox.evaluate(patch, SEED), sandbox.evaluate(head, SEED)     # 6. paired, matched-conditions eval
    if gate.should_promote(inc, cand):                                          # 7. paired-bootstrap promotion
        registry.promote(patch, lineage=(generation, hypothesis))
```

The loop is validated end-to-end in-process (`tests/test_e2e_loop.py`): a
deterministic stub model drives real harness loading, trace collection,
analysis, mutation registration, tabu checks, and statistical gating —
including the promote path and the `NoopMutationEngine` control arm. Live
model endpoints and cluster backends are the granted-compute phase.

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

## Safety & Budget Controls

Full trust analysis in [`THREAT_MODEL.md`](THREAT_MODEL.md). The short form:

- **Static gates are a cost filter, not the security boundary.** AST
  screening (`mutation/validators.py`) rejects forbidden imports, dynamic
  execution (`eval`/`exec`/`compile`/`__import__`), and introspection escape
  hatches — but no security claim rests on it.
- **The boundary is the sandbox**: gVisor, zero credentials
  (`automountServiceAccountToken: false`), deny-all egress with exactly two
  NetworkPolicy exceptions (cluster DNS, the model endpoint), read-only
  rootfs, hard quotas. Results exit only via pod logs — a zero-privilege
  channel.
- **Prompt injection via traces is a named threat (T2)**, mitigated by
  layered gating plus a human diff review before any production promotion.
- **Active Defense**: the mutation engine doubles as an injection sensor —
  when it flags a hijack attempt in the trace corpus, the orchestrator
  fast-fails the candidate *before* gates or sandbox and streams a
  `SECURITY_ALERT` to BigQuery, which serves as the agent SIEM
  (`infra/bigquery/queries/security_incidents.sql`). See THREAT_MODEL.md
  §Active Defense.
- **Spend is hard-capped**: `budget.BudgetGuard` enforces per-generation
  mutation token caps, a sandbox run ceiling, and a cumulative USD kill
  switch checked before every paid operation; GCP billing alerts and Vertex
  quotas back it platform-side.

---

## KPIs & Milestones

Primary hypothesis test — every number reported against the
**`NoopMutationEngine` control arm** (identical loop, harness never changes),
so loop overhead and benchmark noise are subtracted by construction.

| KPI | Definition | Target |
|-----|------------|--------|
| K1 — Headline gain | Absolute success-rate delta, evolved vs. genesis harness, frozen executor, frozen suite | ≥ +10 p.p. within 100 generations, p < 0.05 vs. control |
| K2 — Loop efficiency | Patch funnel: static-gate pass rate → sandbox completion rate → promotion rate | Reported per generation; promotion rate ≥ 5% by M2 |
| K3 — Cost efficiency | USD per promoted patch (mutation tokens + sandbox runs, from `GenerationMetrics`) | Monotone decreasing across the program |
| K4 — Context dependency | Patch acceptance & K1 gain at 100% / 50% / 10% / summary trace budgets | Measurable degradation curve (the long-context claim, tested) |
| K5 — Transfer ratio | Held-out-suite gain ÷ in-domain gain for the final harness | ≥ 0.3 (generalization, not overfit) |

Go/no-go milestones:

- **M1 (month 2):** loop live on cluster backends with BigQuery telemetry;
  control arm running; K2 funnel dashboard from `infra/bigquery/queries/`.
  *No-go if the funnel shows < 1% sandbox-completing patches.*
- **M2 (month 5):** first statistically significant promotion chain on the
  31B open-weight executor; K1 interim ≥ +5 p.p. *No-go if
  indistinguishable from control after 100 generations.*
- **M3 (month 9):** K4 ablation complete; K5 transfer measured on the
  held-out research suite; full lineage + SQL corpus published as the
  reproducibility artifact.

---

## Resource Estimate

Derivation, not a wishlist — counts follow from the experiment design above:

| Resource | Driver | Estimate |
|----------|--------|----------|
| Vertex AI, mutation tier | ~0.9M input tokens/generation × ~600 generations across arms (incl. K4 ablation reruns) | ~540M input + ~15M output tokens |
| Vertex AI, orchestration tier | Training batches: ~60 runs/generation × ~40 steps × ~2K tokens | ~3B flash-tier tokens |
| GKE GPU node pools | Open-weight serving: 2× A100-80GB-class for 27-31B executors, ~6 months at ~40% duty cycle | ~3,500 GPU-hours |
| GKE CPU (sandbox) | ~1,200 evaluation Jobs × 2 vCPU × ≤0.5 h | ~1,200 vCPU-hours burst |
| BigQuery | ~5K spans/run × ~70K runs ≈ 350M rows | < 1 TB storage; ~10 TB/month query volume |

Hard ceiling regardless of estimates: the in-loop `BudgetGuard` USD cap,
set per experiment arm and enforced before every paid call.

---

## Model Integration

Multi-model orchestration across the Google portfolio — mutation tier,
orchestration tier, sandboxed open-weight executors, and managed-runtime
baselines — is specified in [`MODEL_TESTING_MATRIX.md`](MODEL_TESTING_MATRIX.md),
including per-model roles, serving surfaces, and step-by-step testing plans.

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
