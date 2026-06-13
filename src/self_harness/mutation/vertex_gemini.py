"""Gemini-on-Vertex-AI mutation engine.

The load-bearing GCP integration: complete execution traces — frequently
hundreds of thousands of tokens per generation — are fed into Gemini's
long context together with the current harness source. The model returns a
full rewrite plus a falsifiable hypothesis. No summarization stage exists by
design: the failure signal motivating a patch is often a single anomalous
tool result buried mid-trace, and survives only in the raw spans.
"""

from __future__ import annotations

import json
import logging

from ..tracing.analyzer import AnalysisReport
from ..tracing.schema import TraceEvent
from .engine import HarnessPatch, MutationEngine
from .prompts import MUTATION_SYSTEM_PROMPT, build_mutation_request

logger = logging.getLogger(__name__)

# Conservative chars-per-token estimate for budget enforcement.
CHARS_PER_TOKEN = 4


class VertexGeminiMutationEngine(MutationEngine):
    def __init__(
        self,
        project_id: str,
        location: str = "us-central1",
        model: str = "gemini-2.5-pro",
        max_trace_tokens: int = 900_000,
        temperature: float = 0.7,
    ):
        import vertexai
        from vertexai.generative_models import GenerativeModel

        vertexai.init(project=project_id, location=location)
        self.model = GenerativeModel(model, system_instruction=MUTATION_SYSTEM_PROMPT)
        self.max_trace_tokens = max_trace_tokens
        self.temperature = temperature

    def propose(
        self,
        current_spec_json: str,
        current_source: str,
        traces: list[TraceEvent],
        report: AnalysisReport,
    ) -> HarnessPatch | None:
        request = build_mutation_request(
            spec_json=current_spec_json,
            source=current_source,
            report_summary=report.summary(),
            serialized_traces=self._serialize_traces(traces),
        )

        response = self.model.generate_content(
            request,
            generation_config={
                "temperature": self.temperature,
                "response_mime_type": "application/json",
            },
        )
        return self._parse_patch(response.text)

    def _serialize_traces(self, traces: list[TraceEvent]) -> str:
        """Serialize spans newest-run-last; drop oldest runs to fit budget."""
        lines = [
            json.dumps(
                {"run": e.run_id, "step": e.step, "kind": e.kind.value,
                 "dt": round(e.duration_s, 2), "payload": e.payload},
                default=str,
            )
            for e in traces
        ]
        budget_chars = self.max_trace_tokens * CHARS_PER_TOKEN
        out, used = [], 0
        for line in reversed(lines):  # Keep the most recent evidence.
            if used + len(line) > budget_chars:
                break
            out.append(line)
            used += len(line) + 1
        return "\n".join(reversed(out))

    @staticmethod
    def _parse_patch(text: str) -> HarnessPatch | None:
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            logger.warning("mutation model returned non-JSON output; skipping generation")
            return None

        # Surface any indirect-prompt-injection signal (THREAT_MODEL.md T2)
        # before deciding on the patch — the alert matters even when the
        # response carries no usable patch.
        security_detected = bool(data.get("securityDetected") or data.get("security_detected"))
        security_explanation = data.get("securityExplanation") or data.get("security_explanation", "")
        if security_detected:
            logger.critical("mutation engine flagged trace security threat: %s", security_explanation)

        if data.get("no_change"):
            return None
        required = {"hypothesis", "spec_json", "harness_source"}
        if not required.issubset(data):
            logger.warning("mutation output missing fields %s", required - data.keys())
            return None
        return HarnessPatch(
            hypothesis=data["hypothesis"],
            spec_json=data["spec_json"],
            harness_source=data["harness_source"],
            rationale=data.get("rationale", ""),
            security_detected=security_detected,
            security_explanation=security_explanation,
        )
