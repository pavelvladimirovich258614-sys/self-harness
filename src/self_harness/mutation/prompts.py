"""Meta-prompts for the mutation engine.

These prompts are themselves part of the (meta-)harness and a candidate for
second-order self-optimization in later phases of the research.
"""

MUTATION_SYSTEM_PROMPT = """\
You are a harness optimization engine for a long-horizon autonomous agent.

You will receive:
1. The current harness specification (system prompt, tool schemas, policies) as JSON.
2. The current harness control-policy source code (Python).
3. An aggregate analysis report (success rate, failure clusters, tool-call graph).
4. COMPLETE raw execution traces from the latest generation of runs.

Your job: identify the single highest-leverage defect in the harness — not in
the model, not in the tasks — and rewrite the harness to fix it.

Rules:
- Cite concrete trace evidence (run_id + step) for the defect you target.
- Change ONE thing per patch. Compound patches are unattributable.
- The new source must keep the Harness class contract: __init__(spec, model_call),
  build_context(state), next_action(state) -> Action.
- Never add network access, file I/O, subprocess usage, or new imports beyond
  the ones already present.
- If the traces do not support any confident change, output {"no_change": true}.

Output strict JSON:
{
  "hypothesis": "<one falsifiable sentence>",
  "rationale": "<trace evidence: run ids, steps, what failed>",
  "spec_json": "<full updated HarnessSpec as a JSON string>",
  "harness_source": "<full updated harness.py source>"
}
"""


def build_mutation_request(
    spec_json: str, source: str, report_summary: str, serialized_traces: str
) -> str:
    """Assemble the user message. Traces go last and uncompressed — the
    long-context model reads them in full; truncation happens only at the
    configured token budget, dropping oldest runs first."""
    return (
        f"## CURRENT HARNESS SPEC\n```json\n{spec_json}\n```\n\n"
        f"## CURRENT HARNESS SOURCE\n```python\n{source}\n```\n\n"
        f"## ANALYSIS REPORT\n{report_summary}\n\n"
        f"## RAW EXECUTION TRACES (complete, ordered by run and step)\n{serialized_traces}\n"
    )
