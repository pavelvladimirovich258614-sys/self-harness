"""End-to-end loop validation with the stub model: the full
act → telemetry → analyze → mutate → validate → sandbox → gate cycle runs
in-process, with a fake sandbox and scripted engines. This is the basis for
the claim that the loop is validated end-to-end on local backends."""

from self_harness.core.harness import HarnessSpec
from self_harness.mutation.engine import HarnessPatch, MutationEngine, NoopMutationEngine
from self_harness.orchestrator import Orchestrator
from self_harness.sandbox.base import Sandbox, SandboxLimits, SandboxResult

CONFIG = {
    "loop": {"generations": 3, "runs_per_generation": 3, "max_patch_attempts": 2},
    "agent": {"model": "stub", "max_steps": 10, "trace_payload_max_chars": 4096},
    "training": {"suite": "training"},
    "mutation": {"engine": "noop"},
    "budget": {"max_total_usd": 100.0},
    "sandbox": {"backend": "fake", "cpu": "1", "memory": "1Gi",
                "wall_clock_s": 60, "network": "deny"},
    "tracing": {"sink": "memory"},
    "evaluation": {
        "suite": "longhorizon", "episodes_per_task": 1, "seed": 7,
        "promotion": {"min_relative_gain": 0.03, "significance_alpha": 0.05,
                      "bootstrap_samples": 1000},
    },
}

IMPROVED_SOURCE = '''
from self_harness.core.harness import Harness as BaseHarness, AgentState, Action


class Harness(BaseHarness):
    def __init__(self, spec, model_call):
        super().__init__(spec, model_call)

    def build_context(self, state):
        return super().build_context(state)

    def next_action(self, state: AgentState) -> Action:
        if state.step >= 3:
            return Action(kind="terminate")
        return super().next_action(state)
'''


class FakeSandbox(Sandbox):
    """Scores genesis low and any mutated version high — exercises promotion."""

    def evaluate(self, harness_version, artifacts_dir, suite, episodes_per_task,
                 limits: SandboxLimits, seed: int = 0) -> SandboxResult:
        base = 0.4 if harness_version == "genesis" else 0.9
        return SandboxResult(
            completed=True,
            per_task_scores={f"t{i}": base for i in range(8)},
        )


class OneShotEngine(MutationEngine):
    """Emits one valid patch, then nothing — tests promote-then-stagnate."""

    def __init__(self):
        self.fired = False

    def propose(self, current_spec_json, current_source, traces, report):
        if self.fired:
            return None
        self.fired = True
        return HarnessPatch(
            hypothesis="terminate earlier on stub tasks",
            spec_json=HarnessSpec(version="v1").model_dump_json(),
            harness_source=IMPROVED_SOURCE,
        )


def test_loop_runs_with_noop_engine(tmp_path):
    orch = Orchestrator(
        config=CONFIG,
        mutation_engine=NoopMutationEngine(),
        sandbox=FakeSandbox(),
        artifacts_dir=tmp_path,
    )
    orch.run()
    assert orch.registry.current() == "genesis"  # Control arm never promotes
    assert orch.budget.total_usd < CONFIG["budget"]["max_total_usd"]


def test_loop_promotes_winning_patch(tmp_path):
    orch = Orchestrator(
        config=CONFIG,
        mutation_engine=OneShotEngine(),
        sandbox=FakeSandbox(),
        artifacts_dir=tmp_path,
    )
    orch.run()
    head = orch.registry.current()
    assert head != "genesis"
    assert (tmp_path / head / "lineage.json").exists()
    assert orch.budget.total_usd > 0


def test_oscillation_tabu_blocks_duplicate_patch(tmp_path):
    class RepeatingEngine(MutationEngine):
        def propose(self, *args):
            return HarnessPatch(
                hypothesis="same patch every time",
                spec_json=HarnessSpec(version="v1").model_dump_json(),
                harness_source=IMPROVED_SOURCE,
            )

    orch = Orchestrator(
        config=CONFIG,
        mutation_engine=RepeatingEngine(),
        sandbox=FakeSandbox(),
        artifacts_dir=tmp_path,
    )
    orch.run()
    # The identical patch is registered (and promoted) once; the tabu check
    # blocks every subsequent re-registration of the same content.
    versions = [p for p in tmp_path.iterdir() if p.is_dir() and p.name != "genesis"]
    assert len(versions) == 1
