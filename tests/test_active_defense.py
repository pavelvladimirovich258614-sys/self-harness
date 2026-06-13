"""CRITICAL: Active Defense — a security-flagged patch is fast-failed before
gates or sandbox, and the incident is written to the SIEM telemetry feed.

End-to-end assertion of THREAT_MODEL.md §Active Defense: the orchestrator
intercepts HarnessPatch.security_detected, drops the candidate immediately
(no registration, no validation, no sandbox), logs CRITICAL, and emits a
SECURITY_ALERT event tied to a lineage id.
"""

import logging

from self_harness.core.harness import HarnessSpec
from self_harness.mutation.engine import HarnessPatch, MutationEngine
from self_harness.orchestrator import Orchestrator
from self_harness.sandbox.base import Sandbox, SandboxLimits, SandboxResult
from self_harness.tracing.collector import MemorySink
from self_harness.tracing.schema import SpanKind

CONFIG = {
    "loop": {"generations": 2, "runs_per_generation": 1, "max_patch_attempts": 2},
    "agent": {"model": "stub", "max_steps": 3, "trace_payload_max_chars": 1024},
    "training": {"suite": "training"},
    "mutation": {"engine": "noop"},
    "budget": {"max_total_usd": 100.0},
    "sandbox": {"backend": "fake", "cpu": "1", "memory": "1Gi",
                "wall_clock_s": 30, "network": "deny"},
    "tracing": {"sink": "memory"},
    "evaluation": {"suite": "longhorizon", "episodes_per_task": 1, "seed": 1,
                   "promotion": {"min_relative_gain": 0.03,
                                 "significance_alpha": 0.05,
                                 "bootstrap_samples": 200}},
}


class SecurityFlaggingEngine(MutationEngine):
    """Returns a patch carrying an injection alert (and otherwise-valid code
    that would pass the gates) — so the only thing stopping it is fast-fail."""

    def propose(self, *args):
        src = (
            "from self_harness.core.harness import Harness as B, Action\n"
            "class Harness(B):\n"
            "    def __init__(self, spec, model_call):\n"
            "        super().__init__(spec, model_call)\n"
            "    def build_context(self, state):\n"
            "        return super().build_context(state)\n"
            "    def next_action(self, state):\n"
            "        return Action(kind='terminate')\n"
        )
        return HarnessPatch(
            hypothesis="harden webscraper parsing",
            spec_json=HarnessSpec(version="v1").model_dump_json(),
            harness_source=src,
            security_detected=True,
            security_explanation=(
                "CRITICAL ALERT: indirect prompt injection in WebScraper output "
                "('Ignore previous rules...'). High-severity risk mitigated."
            ),
        )


class TrackingSandbox(Sandbox):
    def __init__(self):
        self.calls = 0

    def evaluate(self, harness_version, artifacts_dir, suite, episodes_per_task,
                 limits: SandboxLimits, seed: int = 0) -> SandboxResult:
        self.calls += 1
        return SandboxResult(completed=True, per_task_scores={"t0": 0.9})


def test_security_flag_fast_fails_before_sandbox(tmp_path, caplog):
    sandbox = TrackingSandbox()
    orch = Orchestrator(config=CONFIG, mutation_engine=SecurityFlaggingEngine(),
                        sandbox=sandbox, artifacts_dir=tmp_path)

    with caplog.at_level(logging.CRITICAL, logger="self_harness.orchestrator"):
        orch.run()

    # 1. The candidate never reached the sandbox.
    assert sandbox.calls == 0
    # 2. HEAD never moved — no security-flagged patch was promoted.
    assert orch.registry.current() == "genesis"
    # 3. No candidate artifact was ever registered.
    registered = [p for p in tmp_path.iterdir() if p.is_dir() and p.name != "genesis"]
    assert registered == []
    # 4. CRITICAL fast-fail was logged.
    assert any(r.levelno == logging.CRITICAL for r in caplog.records)
    assert "ACTIVE DEFENSE" in caplog.text


def test_security_incident_written_to_siem_feed(tmp_path):
    orch = Orchestrator(config=CONFIG, mutation_engine=SecurityFlaggingEngine(),
                        sandbox=TrackingSandbox(), artifacts_dir=tmp_path)
    orch.run()

    sink = orch._sink
    assert isinstance(sink, MemorySink)
    alerts = [e for e in sink.events if e.kind == SpanKind.SECURITY_ALERT]
    assert alerts, "expected at least one SECURITY_ALERT event in the SIEM feed"

    alert = alerts[0]
    assert alert.payload["event_type"] == "security_alert"
    assert alert.payload["action"] == "fast_fail_before_sandbox"
    assert alert.payload["lineage_id"].startswith("sec-g")
    assert "prompt injection" in alert.payload["security_explanation"].lower()
    # The hostile mutated code is NOT persisted — only explanation + metadata.
    assert "harness_source" not in alert.payload
    assert "mutatedCode" not in alert.payload
