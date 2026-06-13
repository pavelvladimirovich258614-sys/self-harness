# THREAT_MODEL

> Trust analysis for a system that rewrites its own orchestration code.
> Scope: the self-optimization loop, generated harness patches, the sandbox
> boundary, and spend control. Companion documents: `README.md` (architecture),
> `MODEL_TESTING_MATRIX.md` (experiment plan).

---

## Assets

| Asset | Compromise impact |
|-------|-------------------|
| GCP credentials / service accounts | Lateral movement, data exfiltration, billing abuse |
| API budget (Vertex AI, GPU hours) | Runaway autonomous spend |
| Benchmark integrity | Silent invalidation of every result in the program |
| Harness artifact lineage | Loss of reproducibility; untraceable promoted code |
| Trace corpus in BigQuery | Poisoned evidence base for future mutations |

---

## Trust Boundaries

```text
  TRUSTED                                    UNTRUSTED
  ────────────────────────────────────────   ─────────────────────────────────
  Orchestrator process (loop control,        Generated harness patches
  validation, gating, budget guard)          (model-written Python)

  B1: static gates ──────────────────────▶   AST screening before any execution

  GKE control plane, node OS                 Candidate pod contents

  B2: sandbox ───────────────────────────▶   gVisor + zero credentials +
                                             deny-all egress + hard quotas

  Mutation engine prompt construction        Trace payloads (tool outputs from
                                             task environments)
  B3: evidence channel ──────────────────▶   traces are DATA, never trusted
                                             instructions
```

**Position on static analysis (B1).** The AST gates (`mutation/validators.py`)
reject forbidden imports, dynamic-execution primitives (`eval`, `exec`,
`compile`, `__import__`), introspection escape hatches (`__globals__`,
`__subclasses__`, ...), and contract violations. AST screening of generated
code is **bypassable in principle** and is therefore classified as a *cost
filter*, not a security control: its job is to discard the bulk of broken or
hostile patches before paying for an evaluation run. No security claim rests
on B1.

**The security boundary is B2.** Candidate code executes only inside a GKE
Job in an ephemeral namespace with: gVisor runtime class (user-space kernel),
`automountServiceAccountToken: false` (zero credentials — the pod cannot call
any Google API), deny-all egress NetworkPolicy with exactly two exceptions
(cluster DNS, the model endpoint), read-only root filesystem, dropped
capabilities, non-root UID, Guaranteed-QoS resource limits, and
`activeDeadlineSeconds`. Results leave the pod exclusively via stdout logs
(`##SCORES##` marker) — a channel that requires no privileges. A candidate
that hangs, crashes, or probes the boundary is recorded as a failed
evaluation and discarded with its namespace.

**gVisor / GPU split.** gVisor wraps only the mutated harness code, which is
a CPU workload. Open-weight model serving (vLLM on GPU node pools) runs
*outside* gVisor on dedicated node pools: model weights are trusted
artifacts; only generated code is not. The sandbox reaches the model via an
in-cluster endpoint allowlisted in the NetworkPolicy. This avoids relying on
gVisor GPU passthrough and keeps the untrusted-code surface CPU-only.

---

## Threats and Mitigations

### T1 — Hostile or hallucinated patch attempts privileged operations
Generated code imports `os`/`subprocess`, opens sockets, or hides dynamic
execution behind `eval`.
**Mitigations:** B1 gates reject the obvious cases at zero cost; everything
that slips through executes inside B2 with no credentials, no egress, and no
writable filesystem. Worst case equals: one wasted, fully isolated
evaluation run.

### T2 — Prompt injection through the trace corpus
Tool outputs enter the mutation prompt verbatim. A compromised task
environment (or a benchmark fixture fetched from outside) can embed
instructions addressed to the mutation model — turning harness rewriting
into an attack channel.
**Mitigations:** (a) traces are framed as data in the mutation prompt, and
the requested output is a structured patch, not free-form action; (b) any
resulting patch still crosses B1, B2, and the statistical gate; (c) task
fixtures are version-pinned and built into the sandbox image, not fetched at
runtime; (d) **promotion policy**: automatic promotion moves the loop's
internal HEAD only — deploying an evolved harness to any production or
externally connected agent requires a human review of the patch diff and its
lineage; (e) **Active Defense** (below): the mutation engine is asked to
report injection attempts it detects in the trace corpus, and a flagged patch
is fast-failed before it can reach the gates or the sandbox, with the incident
written to the SIEM feed. Residual risk: subtle behavioral steering below
statistical detection; tracked as an open research question (see README
§Research Questions, safety envelope).

### T3 — Runaway spend by the autonomous loop
The mutate-evaluate cycle is an unbounded API spender by construction.
**Mitigations:** `budget.BudgetGuard` enforces a per-generation mutation
token cap, a sandbox run cap, and a cumulative USD kill switch checked
before every paid operation (`orchestrator.py`); spend per generation is
recorded into `GenerationMetrics`. Platform-side backstops: Vertex AI
quotas, GCP budget alerts with programmatic notification, and billing-level
caps configured on the research project — the in-process guard fails first,
the platform guard fails safe.

### T4 — Benchmark contamination (integrity, not confidentiality)
The mutation engine optimizing against the evaluation suite invalidates
every reported gain.
**Mitigations:** disjoint training pool vs. frozen suites enforced by test
(`tests/test_suite_separation.py`); benchmark definitions and per-task
scores are never serialized into mutation prompts; the candidate pod
receives task fixtures but the mutation engine never sees them.

### T5 — Oscillation / degenerate self-modification
The loop cycles A→B→A or drifts on noise rather than signal.
**Mitigations:** content-digest tabu in the registry (a patch identical to
any ancestor is never re-evaluated); single-change patch policy in the
mutation contract; paired same-generation evaluation of candidate and
incumbent under identical seed and limits; statistical gate with minimum
effect size; `NoopMutationEngine` control arm quantifies gate
false-positive rate.

### T6 — Telemetry loss or poisoning
Trace loss biases mutation evidence; fabricated spans poison it.
**Mitigations:** at-least-once delivery with bounded retries and a local
NDJSON dead-letter for offline backfill (`bigquery_sink.py`); spans are
written only by the trusted orchestrator process — candidate pods have no
BigQuery access (B2), so the evaluation sandbox cannot write telemetry at
all.

---

## Active Defense — BigQuery as an Agent SIEM

The threats above are mostly *passive* containment: keep generated code from
doing harm. Active Defense adds a *detect-and-respond* loop for the specific
case of indirect prompt injection (T2), turning the mutation engine itself
into a sensor and BigQuery into the SIEM of record.

```text
  ┌─────────────────────────────────────────────────────────────────────────┐
  │  1. ENCOUNTER   Agent hits an indirect prompt injection in the wild;     │
  │                 the adversarial string is captured verbatim in the trace.│
  │                                                                          │
  │  2. DETECT      Gemini Mutation Engine analyzes the trace, recognizes    │
  │                 the hijack attempt, and sets securityDetected = true     │
  │                 with a securityExplanation.                              │
  │                                                                          │
  │  3. FAST-FAIL   Orchestrator intercepts the flag the instant the patch   │
  │                 returns and KILLS the candidate — before static gates,   │
  │                 before registration, before the sandbox. No flagged code │
  │                 is ever written to disk, validated, or executed.         │
  │                                                                          │
  │  4. TELEMETRY   A SECURITY_ALERT event (lineage id + explanation +       │
  │                 generation, NOT the hostile code) is streamed to         │
  │                 BigQuery and surfaced by the SIEM query for alerting     │
  │                 and investigation.                                       │
  └─────────────────────────────────────────────────────────────────────────┘
```

| Stage | Component | Artifact |
|-------|-----------|----------|
| Detect | `mutation/vertex_gemini.py` | parses `securityDetected` / `securityExplanation`, logs CRITICAL |
| Fast-fail | `orchestrator.py` → `_fast_fail_security` | candidate dropped before B1/B2; no registration, no sandbox spend |
| Carry | `mutation/engine.py` | `HarnessPatch.security_detected` / `security_explanation` |
| Telemetry | `tracing/bigquery_sink.py` → `record_security_incident` | `SECURITY_ALERT` span with `event_type`, `lineage_id`, `security_explanation` |
| SIEM | `infra/bigquery/queries/security_incidents.sql` | one row per incident, newest first |

**Why fast-fail before the gates.** A flagged patch is the engine telling the
loop "this generation's evidence is poisoned." Continuing to validate or
sandbox it spends budget reasoning about adversary-controlled input and risks
laundering a steered patch through the normal path. Fast-fail makes the safe
action the cheap action: zero sandbox runs, zero artifacts, one telemetry row.

**What is NOT persisted.** The incident record stores the engine's
explanation and lineage metadata only — never the offending `mutatedCode` or
the raw injection string — so a hostile payload is not re-stored in the
telemetry plane where a future query might surface it unguarded.

**Why this needs GCP.** BigQuery is already the trace system of record; the
SIEM feed is the same table filtered by `kind = 'security_alert'`, so incident
detection, alerting, and correlation with the surrounding execution traces are
a single SQL surface — no separate logging stack, and incidents are joinable
against the full run history that produced them.

---

## Residual Risks (stated, not hidden)

1. AST gates do not constitute a sandbox; a sufficiently obfuscated payload
   reaches B2. Accepted: B2 is designed for exactly this case.
2. gVisor is not a hypervisor; kernel-class escapes, while historically
   rare, are not impossible. Compensation: zero credentials and no egress
   mean a successful escape lands in a node with nothing to steal and
   nowhere to send it; sandbox node pools are isolated from all other
   workloads.
3. Sub-statistical behavioral steering via T2 cannot be ruled out by
   gating alone. Compensation: human diff review before any production
   promotion; full lineage enables retroactive audit of every promoted
   patch.
