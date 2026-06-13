"""CRITICAL: Security — static AST gates on LLM-generated harness code.

These tests assert the gate's behavior against the concrete attack patterns
enumerated in THREAT_MODEL.md (T1: hostile or hallucinated patch attempts a
privileged operation). The gate is a COST FILTER, not the security boundary
(that is the sandbox, B2) — but it must still hard-reject the obvious classes
of malicious generation before any evaluation spend is incurred.
"""

import pytest

from self_harness.core.harness import HarnessSpec
from self_harness.mutation.engine import HarnessPatch
from self_harness.mutation.validators import PatchValidator

# A minimal, contract-complete harness subclass — the shape a well-behaved
# mutation engine is expected to emit.
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


def inject(statement: str) -> str:
    """Splice a hostile statement into the body of an otherwise valid harness."""
    return VALID_SOURCE.replace(
        'return Action(kind="terminate")',
        f'{statement}; return Action(kind="terminate")',
    )


# --- Positive path -----------------------------------------------------------

def test_valid_patch_passes():
    assert PatchValidator().check(patch_with(VALID_SOURCE))


# --- Negative path: Threat Model attack patterns -----------------------------

def test_rm_rf_via_eval_os_system_rejected():
    # The canonical worst case: eval("os.system('rm -rf /')").
    assert not PatchValidator().check(
        patch_with(inject("eval(\"os.system('rm -rf /')\")"))
    )


def test_exec_rejected():
    assert not PatchValidator().check(
        patch_with(inject("exec('import os; os.system(\"id\")')"))
    )


def test_dunder_import_call_rejected():
    # __import__('sys') as a direct call.
    assert not PatchValidator().check(patch_with(inject("__import__('sys')")))


def test_compile_rejected():
    assert not PatchValidator().check(
        patch_with(inject("compile('1', '<s>', 'eval')"))
    )


@pytest.mark.parametrize("module", ["os", "sys", "subprocess", "socket", "shutil", "pathlib"])
def test_forbidden_imports_rejected(module):
    assert not PatchValidator().check(patch_with(f"import {module}\n" + VALID_SOURCE))


@pytest.mark.parametrize("module", ["os", "subprocess", "socket"])
def test_forbidden_from_imports_rejected(module):
    assert not PatchValidator().check(
        patch_with(f"from {module} import path\n" + VALID_SOURCE)
    )


def test_local_file_read_via_open_rejected():
    # Reading local files (e.g. exfiltrating credentials) is blocked.
    assert not PatchValidator().check(
        patch_with(inject("open('/etc/passwd').read()"))
    )


def test_getattr_escape_rejected():
    assert not PatchValidator().check(patch_with(inject('getattr(state, "x", None)')))


def test_dunder_globals_introspection_rejected():
    assert not PatchValidator().check(
        patch_with(inject("x = self.next_action.__globals__"))
    )


def test_subclasses_sandbox_escape_rejected():
    # The classic ''.__class__.__mro__[1].__subclasses__() escape chain.
    assert not PatchValidator().check(
        patch_with(inject("c = ().__class__.__bases__[0].__subclasses__()"))
    )


def test_self_harness_import_outside_core_rejected():
    # Mutated code may only touch the contract module (self_harness.core).
    bad = "from self_harness.sandbox.base import Sandbox\n" + VALID_SOURCE
    assert not PatchValidator().check(patch_with(bad))


# --- Negative path: contract / structural violations -------------------------

def test_syntax_error_rejected():
    assert not PatchValidator().check(patch_with("class Harness(:\n    pass\n"))


def test_missing_harness_class_rejected():
    assert not PatchValidator().check(patch_with("x = 1\n"))


def test_missing_contract_methods_rejected():
    bad = (
        "from self_harness.core.harness import Harness as BaseHarness, Action\n"
        "class Harness(BaseHarness):\n"
        "    def next_action(self, state):\n"
        "        return Action(kind='terminate')\n"
    )
    assert not PatchValidator().check(patch_with(bad))


def test_invalid_spec_rejected():
    bad_patch = HarnessPatch(hypothesis="h", spec_json="{not json", harness_source=VALID_SOURCE)
    assert not PatchValidator().check(bad_patch)
