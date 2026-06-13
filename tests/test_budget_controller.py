"""CRITICAL: Billing — the autonomous loop must not overspend.

The BudgetGuard is the in-process kill switch. These tests assert that
crossing a USD ceiling raises BudgetExceededError immediately (at the spend
event, not at the next loop iteration) and that the orchestrator turns that
exception into a clean loop abort rather than a crash or continued spend.
"""

import pytest

from self_harness.budget import (
    BudgetConfig,
    BudgetExceededError,
    BudgetGuard,
)


def test_per_generation_cap_raises_immediately():
    guard = BudgetGuard(BudgetConfig(
        max_usd_per_generation=0.10,
        sandbox_usd_per_run=0.06,
        max_total_usd=1_000.0,
    ))
    guard.start_generation()

    guard.record_sandbox_run()                 # 0.06 USD — under the cap
    assert guard.generation_usd == pytest.approx(0.06)

    with pytest.raises(BudgetExceededError) as exc:
        guard.record_sandbox_run()             # 0.12 USD — crosses the cap

    assert exc.value.scope == "generation"
    assert exc.value.spent_usd >= 0.10


def test_cumulative_cap_raises_even_without_per_generation_cap():
    guard = BudgetGuard(BudgetConfig(
        max_total_usd=0.10,
        sandbox_usd_per_run=0.06,
        max_usd_per_generation=None,           # per-generation cap disabled
    ))
    guard.start_generation()
    guard.record_sandbox_run()                 # 0.06 cumulative
    with pytest.raises(BudgetExceededError) as exc:
        guard.record_sandbox_run()             # 0.12 cumulative — crosses total cap
    assert exc.value.scope == "total"


def test_mutation_token_spend_counts_against_budget():
    # 1,000,000 chars ≈ 250,000 tokens at 2.50 USD/Mtoken ≈ 0.625 USD.
    guard = BudgetGuard(BudgetConfig(
        max_usd_per_generation=0.50,
        mutation_usd_per_mtoken=2.50,
        max_total_usd=1_000.0,
    ))
    guard.start_generation()
    with pytest.raises(BudgetExceededError):
        guard.record_mutation_call(request_chars=1_000_000, response_chars=0)


def test_start_generation_resets_per_generation_counter():
    guard = BudgetGuard(BudgetConfig(
        max_usd_per_generation=0.10,
        sandbox_usd_per_run=0.06,
        max_total_usd=1_000.0,
    ))
    guard.start_generation()
    guard.record_sandbox_run()                 # gen spend 0.06
    guard.start_generation()                   # new generation resets the gauge
    assert guard.generation_usd == 0.0
    guard.record_sandbox_run()                 # 0.06 again — must NOT raise
    assert guard.generation_usd == pytest.approx(0.06)


def test_allow_gates_block_before_spend_when_caps_hit():
    guard = BudgetGuard(BudgetConfig(max_sandbox_runs=1))
    guard.start_generation()
    assert guard.allow_sandbox_run() is True
    guard.record_sandbox_run()
    assert guard.allow_sandbox_run() is False  # run cap reached, no further spend


def test_loop_aborts_cleanly_on_budget_exceeded(tmp_path):
    """The orchestrator converts BudgetExceededError into a loop abort: it
    stops, does not crash, and does not keep spending."""
    from self_harness.mutation.engine import HarnessPatch, MutationEngine
    from self_harness.orchestrator import Orchestrator
    from self_harness.sandbox.base import Sandbox, SandboxLimits, SandboxResult

    class EagerEngine(MutationEngine):
        def propose(self, *args):
            from self_harness.core.harness import HarnessSpec
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
            return HarnessPatch(hypothesis="h",
                                spec_json=HarnessSpec(version="v1").model_dump_json(),
                                harness_source=src)

    class CountingSandbox(Sandbox):
        def __init__(self):
            self.calls = 0

        def evaluate(self, harness_version, artifacts_dir, suite,
                     episodes_per_task, limits: SandboxLimits, seed: int = 0):
            self.calls += 1
            return SandboxResult(completed=True, per_task_scores={"t0": 0.5})

    config = {
        "loop": {"generations": 5, "runs_per_generation": 1, "max_patch_attempts": 1},
        "agent": {"model": "stub", "max_steps": 3, "trace_payload_max_chars": 1024},
        "training": {"suite": "training"},
        "mutation": {"engine": "noop"},
        # sandbox_usd_per_run is large; the cumulative cap is crossed within
        # the first generation's paired evaluation.
        "budget": {"max_total_usd": 0.05, "sandbox_usd_per_run": 0.10},
        "sandbox": {"backend": "fake", "cpu": "1", "memory": "1Gi",
                    "wall_clock_s": 30, "network": "deny"},
        "tracing": {"sink": "memory"},
        "evaluation": {"suite": "longhorizon", "episodes_per_task": 1, "seed": 1,
                       "promotion": {"min_relative_gain": 0.03,
                                     "significance_alpha": 0.05,
                                     "bootstrap_samples": 200}},
    }

    sandbox = CountingSandbox()
    orch = Orchestrator(config=config, mutation_engine=EagerEngine(),
                        sandbox=sandbox, artifacts_dir=tmp_path)
    orch.run()  # must return, not raise

    # The loop stopped at the first cap crossing rather than running all 5
    # generations (which would have been 10 sandbox evaluations).
    assert sandbox.calls < 10
    assert orch.budget.total_usd >= config["budget"]["max_total_usd"]
