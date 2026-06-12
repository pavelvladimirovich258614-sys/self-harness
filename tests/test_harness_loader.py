import json

from self_harness.core.harness import HarnessSpec
from self_harness.core.harness_loader import HarnessLoader
from self_harness.core.registry import HarnessRegistry, Lineage

MUTATED_SOURCE = '''
from self_harness.core.harness import Harness as BaseHarness, AgentState, Action


class Harness(BaseHarness):
    def next_action(self, state: AgentState) -> Action:
        if state.step >= 1:
            return Action(kind="terminate")
        return super().next_action(state)
'''


def test_loads_genesis_spec_without_source(tmp_path):
    genesis = tmp_path / "genesis"
    genesis.mkdir()
    (genesis / "spec.json").write_text(HarnessSpec().model_dump_json())

    harness = HarnessLoader(tmp_path).load("genesis", model_call=lambda m: {})
    assert harness.spec.version == "genesis"


def test_loads_registered_mutated_harness(tmp_path):
    registry = HarnessRegistry(tmp_path)
    version = registry.register(
        spec_json=HarnessSpec(version="v1").model_dump_json(),
        harness_source=MUTATED_SOURCE,
        lineage=Lineage(parent_version="genesis", generation=0,
                        trace_batch_id="batch-0", hypothesis="terminate earlier"),
    )

    harness = HarnessLoader(tmp_path).load(version, model_call=lambda m: {})
    from self_harness.core.harness import AgentState

    action = harness.next_action(AgentState(task="t", step=1))
    assert action.kind == "terminate"

    lineage = json.loads((tmp_path / version / "lineage.json").read_text())
    assert lineage["parent_version"] == "genesis"


def test_promote_and_rollback(tmp_path):
    registry = HarnessRegistry(tmp_path)
    assert registry.current() == "genesis"
    version = registry.register(
        spec_json=HarnessSpec(version="v1").model_dump_json(),
        harness_source=MUTATED_SOURCE,
        lineage=Lineage("genesis", 0, "batch-0", "h"),
    )
    registry.promote(version)
    assert registry.current() == version
    assert registry.rollback() == "genesis"
