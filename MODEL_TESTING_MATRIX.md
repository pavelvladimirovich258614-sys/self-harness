# MODEL_TESTING_MATRIX

> Multi-model orchestration plan for the `self-harness` framework.
> Scope: full-spectrum integration of the Google model portfolio — frontier
> reasoning, high-efficiency orchestration, open-weight sandboxed agents, and
> managed agentic runtimes — into a single self-optimizing loop.
>
> Global status: **all entries [NOT YET TESTED / PENDING GCP COMPUTE GRANT]**.
> No benchmark numbers appear in this document by design; the matrix defines
> the experiments the requested credits will fund.

```text
┌─────────────────────────────────────────────────────────────────────────────┐
│  ROLE TOPOLOGY                                                              │
│                                                                             │
│  [HEAVYWEIGHTS]      Gemini 3.x Pro/Flash ──▶ Mutation Engine (rewrite)     │
│  [SPEEDSTERS]        Gemini Flash-Lite    ──▶ Orchestration / triage / eval │
│  [OPEN WEIGHTS]      Gemma family on GPU  ──▶ Target agents inside sandbox  │
│  [MANAGED AGENTS]    Antigravity / Deep   ──▶ External baselines &          │
│                      Research runtimes        benchmark task sources        │
└─────────────────────────────────────────────────────────────────────────────┘
```

> ---

## Summary Matrix

| # | Model | Tier | Pipeline Role | Serving Surface | Status |
|---|-------|------|---------------|-----------------|--------|
| 1 | Gemini 3.1 Pro | Heavyweight | Primary Mutation Engine | Vertex AI | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 2 | Gemini 3.5 Flash | Heavyweight | Code-rewrite specialist / patch synthesis | Vertex AI | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 3 | Gemini 3.1 Flash-Lite | Speedster | Orchestration, tool-calling, log triage, eval gating | Vertex AI | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 4 | Gemma 4 31B Dense | Open Weights | Sandboxed target agent (capability ceiling) | GKE + GPU (Compute Engine) | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 5 | Gemma 4 26B MoE | Open Weights | Sandboxed target agent (throughput arm) | GKE + GPU (Compute Engine) | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 6 | Gemma 4 12B | Open Weights | Sandboxed target agent (multimodal arm) | GKE + GPU (Compute Engine) | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 7 | Antigravity Agent Preview | Managed Agent | External baseline + benchmark task source | Managed runtime | NOT YET TESTED / PENDING GCP COMPUTE GRANT |
| 8 | Gemini Deep Research Max Preview | Managed Agent | Long-horizon research baseline | Managed runtime | NOT YET TESTED / PENDING GCP COMPUTE GRANT |

> ---

## 1. THE HEAVYWEIGHTS — Advanced Reasoning & Mutation

Models in this tier read *complete* execution-trace corpora — not summaries —
together with the current harness source, and emit falsifiable patch
hypotheses. This is the load-bearing tier: mutation quality is the variable
the entire research program measures.

### 1.1 Gemini 3.1 Pro

```text
model_id        : gemini-3.1-pro            (Vertex AI)
class           : frontier reasoning, advanced agentic capabilities
context regime  : whole-generation trace batches, multi-run
integration     : src/self_harness/mutation/vertex_gemini.py
config binding  : mutation.engine = "vertex_gemini", mutation.model
```

**Pipeline role.** Primary Mutation Engine. Consumes the full raw-span corpus
of a generation from BigQuery (`bigquery_trace_telemetry` path), correlates
failure clusters across runs, and produces a single-change harness rewrite
plus a falsifiable hypothesis recorded in artifact lineage.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Enable the model on Vertex AI; bind `configs/gcp.yaml → mutation.model`;
   verify the strict-JSON patch contract against `mutation/validators.py` gates.
2. Run 100 generations of the loop on the frozen `longhorizon` suite with a
   fixed target agent; log every patch, gate decision, and promotion to
   `harness_telemetry.generation_metrics`.
3. Context-length ablation: re-run with trace budgets at 100%, 50%, 10%, and
   summary-only; measure patch acceptance rate and per-generation success-rate
   delta to quantify the long-context dependency.
4. Publish convergence curves (success rate vs. generation) and the
   static-gate / sandbox-failure / statistical-gate rejection distribution.

### 1.2 Gemini 3.5 Flash

```text
model_id        : gemini-3.5-flash          (Vertex AI)
class           : frontier coding performance, high-throughput
context regime  : single-run traces + harness source
integration     : src/self_harness/mutation/vertex_gemini.py (second engine arm)
```

**Pipeline role.** Code-rewrite specialist. Receives the defect localization
produced by 3.1 Pro and synthesizes the concrete `harness.py` patch; also
serves as the low-latency arm in a two-stage mutation A/B (Pro-only vs.
Pro-localize + Flash-rewrite) to measure cost/quality trade-offs.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Deploy as a second `MutationEngine` instance; route via a config flag
   (`mutation.engine_arm = pro | flash | staged`).
2. Replay 500 archived trace batches through both arms offline; score patch
   validity rate (static gates) and sandbox completion rate per arm.
3. Run 50 live generations per arm under identical seeds; compare promotion
   rate, per-promotion gain, and $/promoted-patch.
4. Select the production arm by Pareto front (gain vs. cost) and freeze it
   for the open-weights experiments in Section 3.

> ---

## 2. THE SPEEDSTERS — High Efficiency & Orchestration

High-volume, latency-sensitive call sites where frontier reasoning is wasted
spend: routine tool-calling, log triage, and evaluation plumbing.

### 2.1 Gemini 3.1 Flash-Lite

```text
model_id        : gemini-3.1-flash-lite     (Vertex AI)
class           : high-efficiency, low-latency
call sites      : core/agent.py (tool-calling), tracing/analyzer.py (triage),
                  evaluation/pipeline.py (LLM-assisted scoring rubric)
```

**Pipeline role.** Three duties: (a) default executor for routine
orchestration steps inside benchmark episodes; (b) first-pass error triage —
classify ERROR spans and discard noise before the expensive Pro-tier trace
analysis; (c) deterministic-rubric scoring assistant inside the Evaluation
Pipeline and pre-checks for gating.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Bind as `agent.model` for benchmark fixtures; verify tool-call schema
   fidelity over 1,000 synthetic tool-use episodes (target: malformed-call
   rate measured, not assumed).
2. Insert as triage stage between BigQuery and the Mutation Engine; measure
   trace-volume reduction vs. information loss (does Pro-tier patch quality
   drop when triage filters spans?).
3. Run the full evaluation suite with Flash-Lite scoring vs. pure
   deterministic scoring; require ≥0.95 agreement before enabling.
4. Record per-call latency and cost into `generation_metrics` to establish
   the orchestration cost baseline for the grant report.

> ---

## 3. THE OPEN WEIGHTS — Sandboxed Edge Agents

Central hypothesis of this tier: **a harness written by frontier models
(Gemini 3.x) materially raises the success rate of small open-weight models
(Gemma) on long-horizon tasks** — i.e., harness intelligence partially
substitutes for parameter count. Target agents execute exclusively inside the
GKE sandbox (`gke_sandbox_evaluator` path) on GPU node pools, under the
standard isolation profile (gVisor, deny-all egress, no credentials).

### 3.1 Gemma 4 31B Dense

```text
deployment      : GKE GPU node pool (Compute Engine A-series), vLLM serving
isolation       : infra/gke/sandbox-job.yaml profile + in-cluster endpoint
role arm        : capability ceiling of the open-weights cohort
```

**Pipeline role.** Strongest open-weight target agent. Defines the upper
bound of harness-driven improvement within the Gemma cohort; primary subject
of the headline experiment (genesis harness vs. generation-N harness).

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Provision a GPU node pool; deploy behind an in-cluster vLLM endpoint
   allowlisted in the sandbox NetworkPolicy as the sole permitted egress.
2. Baseline: 200 episodes per benchmark task under the genesis harness;
   record success rate, mean steps, token spend.
3. Evolve: run the full self-optimization loop with Gemini 3.x mutating the
   harness while Gemma 4 31B remains the frozen executor, 100 generations.
4. Report paired baseline-vs-final deltas with bootstrap confidence
   intervals; archive the full harness lineage as the reproducibility artifact.

### 3.2 Gemma 4 26B MoE

```text
deployment      : GKE GPU node pool, expert-parallel serving
role arm        : throughput arm — active-parameter efficiency under load
```

**Pipeline role.** Tests whether harness improvements learned against the
dense model transfer to a sparse architecture with different failure modes
(routing-induced inconsistency across long contexts), and whether MoE
inference economics change the optimal harness retry/context policies.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Deploy with expert-parallel serving on the same node-pool footprint as
   3.1; equalize tokens/sec budget across arms.
2. Zero-shot transfer: evaluate the final 31B-evolved harness on the MoE
   model with no further mutation; measure retained gain fraction.
3. Continued evolution: 50 additional generations specialized to the MoE
   executor; compare specialized vs. transferred harness.
4. Decompose harness diffs between the two lineages to identify which policy
   classes (retry, context, termination) are architecture-specific.

### 3.3 Gemma 4 12B (unified encoder-free multimodal)

```text
deployment      : GKE GPU node pool (single-accelerator class)
role arm        : multimodal + small-model arm
```

**Pipeline role.** Two questions: (a) the floor — how far down the parameter
scale does harness-driven improvement remain significant; (b) multimodal
traces — benchmark tasks that emit screenshots/plots into the trace stream,
testing whether the mutation loop handles mixed-modality evidence.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Deploy on single-GPU nodes; extend `tracing/schema.py` payloads with
   GCS-referenced image artifacts for multimodal spans.
2. Repeat the Section 3.1 baseline/evolve protocol at 12B scale; compare the
   relative gain against 31B to chart gain-vs-scale.
3. Add two image-grounded long-horizon tasks to a *separate* frozen suite;
   run 50 generations with multimodal traces fed to the Mutation Engine.
4. Report whether multimodal evidence changes patch targeting (e.g., patches
   citing visual spans) vs. text-only ablation.

> ---

## 4. SPECIALIZED MANAGED AGENTS — Native Executors

Google's managed agentic runtimes serve two functions: **external baselines**
(how does a hand-maintained production harness compare to an evolved one on
identical tasks?) and **benchmark task sources** (task families these runtimes
are tuned for become held-out suites for the self-harness loop).

### 4.1 Antigravity Agent Preview

```text
surface         : managed agentic runtime, isolated Linux sandbox execution
framework role  : external baseline for sandboxed code-execution tasks
```

**Pipeline role.** Reference point for the GKE sandbox arm: identical
long-horizon coding tasks run (a) under Antigravity's native harness and
(b) under the evolved self-harness with a Gemma executor. The comparison
isolates the value of harness evolution against a production-grade,
hand-engineered control loop.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Mirror the `longhorizon` coding suite into Antigravity-executable task
   definitions; verify scoring parity between both execution surfaces.
2. Run the suite under the native runtime; capture success rate, wall-clock,
   and step counts as the managed-baseline row.
3. Run the identical suite under the final evolved harness (Section 3 output)
   and under the genesis harness.
4. Publish the three-way comparison: managed baseline vs. genesis vs. evolved.

### 4.2 Gemini Deep Research Max Preview

```text
surface         : managed long-horizon research runtime
framework role  : baseline + task source for research-style episodes
```

**Pipeline role.** Supplies the research-task family (multi-source synthesis,
citation-grounded reporting) as a held-out generalization suite — a task
distribution the harness never trains on — and provides the managed-runtime
baseline for that family.

**Status:** `[NOT YET TESTED / PENDING GCP COMPUTE GRANT]`

**Testing Plan**
1. Define a 20-task research suite with deterministic, citation-checkable
   scoring; register it as a frozen held-out suite in
   `evaluation/benchmarks/`.
2. Capture the managed-runtime baseline on all 20 tasks.
3. Evaluate harnesses evolved on *coding* tasks (Section 3) against this
   held-out research suite — the transfer measurement, with zero
   research-task training exposure.
4. Report transfer ratio (held-out gain / in-domain gain) as the headline
   generalization metric of the research program.

> ---

## Cross-Cutting Protocol

| Control | Enforcement |
|---------|-------------|
| Frozen benchmark suites | Mutation Engine never observes benchmark definitions or per-task scores |
| Identical seeds & budgets | Candidate vs. incumbent always evaluated under matched conditions |
| Isolation invariant | All open-weight executors run under gVisor, deny-all egress, zero credentials |
| Statistical gating | Paired bootstrap, α = 0.05, minimum relative gain threshold |
| Full lineage | Every promoted harness traceable to trace batch, hypothesis, and gate decision |
| Telemetry of record | All runs stream spans to BigQuery; every figure in reports is reproducible by SQL |

> ---

## Compute Dependency Statement

Every row in this matrix is blocked on the same resource class: sustained
Vertex AI inference for the mutation tier, GPU node pools for the open-weight
executor tier, and BigQuery capacity for trace analytics. The experiments are
designed, the framework is implemented and tested at the unit level
(`tests/`), and the loop runs end-to-end on local backends. The grant
converts this matrix from a plan into data.
