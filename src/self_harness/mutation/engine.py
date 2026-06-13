"""Mutation engine interface and patch contract.

A patch is not just new code: it carries a falsifiable hypothesis about why
the change should improve performance. The hypothesis is stored in lineage
and checked post-hoc by the evaluation pipeline, which keeps the loop honest —
mutations that "work" for reasons other than their stated hypothesis are a
red flag for benchmark overfitting.
"""

from __future__ import annotations

import abc
from dataclasses import dataclass

from ..tracing.analyzer import AnalysisReport
from ..tracing.schema import TraceEvent


@dataclass
class HarnessPatch:
    hypothesis: str        # Falsifiable claim, e.g. "retries mask schema errors"
    spec_json: str         # New serialized HarnessSpec (prompts, tools, policies)
    harness_source: str    # New harness.py source (control policy)
    rationale: str = ""    # Trace evidence cited by the mutation model
    # Indirect-prompt-injection signal (THREAT_MODEL.md T2). When the mutation
    # model reports a hijack attempt embedded in the trace corpus, it is
    # surfaced here for logging and telemetry regardless of whether a usable
    # patch was produced.
    security_detected: bool = False
    security_explanation: str = ""


class MutationEngine(abc.ABC):
    @abc.abstractmethod
    def propose(
        self,
        current_spec_json: str,
        current_source: str,
        traces: list[TraceEvent],
        report: AnalysisReport,
    ) -> HarnessPatch | None:
        """Analyze a generation's traces and propose a harness rewrite.

        Return None when the evidence does not support any change — a no-op
        generation is preferable to a noise-driven mutation.
        """


class NoopMutationEngine(MutationEngine):
    """Control arm for ablations: the loop runs, the harness never changes."""

    def propose(self, current_spec_json, current_source, traces, report):  # noqa: D102
        return None
