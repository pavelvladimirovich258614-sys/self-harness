from self_harness.core.harness import HarnessSpec
from self_harness.mutation.engine import HarnessPatch
from self_harness.mutation.validators import PatchValidator

VALID_SOURCE = '''
from self_harness.core.harness import Harness as BaseHarness, AgentState, Action


class Harness(BaseHarness):
    def __init__(self, spec, model_call):
        super().__init__(spec, model_call)

    def build_context(self, state):
        return super().build_context(state)

    def next_action(self, state: AgentState) -> Action:
        if state.step >= 2:
            return Action(kind="terminate")
        return super().next_action(state)
'''

SPEC = HarnessSpec(version="v1").model_dump_json()


def patch_with(source: str) -> HarnessPatch:
    return HarnessPatch(hypothesis="h", spec_json=SPEC, harness_source=source)


def test_valid_patch_passes():
    assert PatchValidator().check(patch_with(VALID_SOURCE))


def test_forbidden_import_rejected():
    assert not PatchValidator().check(patch_with(VALID_SOURCE + "\nimport os\n"))


def test_eval_call_rejected():
    bad = VALID_SOURCE.replace(
        'return Action(kind="terminate")',
        'eval("__import__(\'os\')"); return Action(kind="terminate")',
    )
    assert not PatchValidator().check(patch_with(bad))


def test_getattr_escape_rejected():
    bad = VALID_SOURCE.replace(
        'return Action(kind="terminate")',
        'getattr(state, "x", None); return Action(kind="terminate")',
    )
    assert not PatchValidator().check(patch_with(bad))


def test_dunder_introspection_rejected():
    bad = VALID_SOURCE.replace(
        'return Action(kind="terminate")',
        'x = self.next_action.__globals__; return Action(kind="terminate")',
    )
    assert not PatchValidator().check(patch_with(bad))


def test_self_harness_import_outside_core_rejected():
    bad = "from self_harness.sandbox.base import Sandbox\n" + VALID_SOURCE
    assert not PatchValidator().check(patch_with(bad))


def test_missing_contract_methods_rejected():
    bad = (
        "from self_harness.core.harness import Harness as BaseHarness, Action\n"
        "class Harness(BaseHarness):\n"
        "    def next_action(self, state):\n"
        "        return Action(kind='terminate')\n"
    )
    assert not PatchValidator().check(patch_with(bad))
