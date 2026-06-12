"""Static safety gates for generated harness patches.

First line of defense, applied before any sandbox execution:

  gate 1 — syntax:    the source must parse.
  gate 2 — imports:   allowlist only; within self_harness only `core` is importable.
  gate 3 — calls:     no eval/exec/compile/__import__/open/getattr-style escapes.
  gate 4 — dunders:   no introspection escape hatches (__globals__, __subclasses__, ...).
  gate 5 — contract:  a Harness class with the required methods must exist.
  gate 6 — spec:      the new spec must deserialize into HarnessSpec.

These gates are a COST FILTER, not the security boundary. AST screening is
bypassable in principle; the actual boundary is the sandbox (gVisor, zero
credentials, deny-by-default egress). Gates exist to discard the bulk of
broken or hostile patches before paying for an evaluation run. See
THREAT_MODEL.md for the full trust analysis.
"""

from __future__ import annotations

import ast
import logging

from ..core.harness import HarnessSpec
from .engine import HarnessPatch

logger = logging.getLogger(__name__)

ALLOWED_IMPORTS = {
    "json", "time", "math", "re", "enum", "uuid", "typing", "dataclasses",
    "collections", "itertools", "functools", "pydantic", "self_harness",
    "__future__",
}

# Within the framework package, mutated code may only touch the contract module.
ALLOWED_SELF_HARNESS_PREFIX = "self_harness.core"

FORBIDDEN_CALLS = {
    "eval", "exec", "compile", "__import__", "open", "input", "breakpoint",
    "globals", "locals", "vars", "getattr", "setattr", "delattr", "memoryview",
}

FORBIDDEN_ATTRIBUTES = {
    "__globals__", "__builtins__", "__subclasses__", "__bases__", "__mro__",
    "__code__", "__closure__", "__import__", "__loader__", "__spec__",
}

FORBIDDEN_NAMES = {"__builtins__", "__loader__", "__spec__"}

REQUIRED_METHODS = {"__init__", "build_context", "next_action"}


class PatchValidator:
    def check(self, patch: HarnessPatch) -> bool:
        return (
            self._check_spec(patch.spec_json)
            and self._check_source(patch.harness_source)
        )

    @staticmethod
    def _check_spec(spec_json: str) -> bool:
        try:
            HarnessSpec.model_validate_json(spec_json)
            return True
        except Exception as exc:  # noqa: BLE001
            logger.info("patch rejected: invalid spec (%s)", exc)
            return False

    def _check_source(self, source: str) -> bool:
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            logger.info("patch rejected: syntax error (%s)", exc)
            return False

        for node in ast.walk(tree):
            if not self._check_node(node):
                return False

        harness_cls = next(
            (n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "Harness"),
            None,
        )
        if harness_cls is None:
            logger.info("patch rejected: no Harness class")
            return False

        methods = {n.name for n in harness_cls.body if isinstance(n, ast.FunctionDef)}
        missing = REQUIRED_METHODS - methods
        if missing:
            logger.info("patch rejected: missing methods %s", missing)
            return False
        return True

    def _check_node(self, node: ast.AST) -> bool:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            return self._check_import(node)

        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if name in FORBIDDEN_CALLS:
                logger.info("patch rejected: forbidden call %r", name)
                return False

        if isinstance(node, ast.Attribute) and node.attr in FORBIDDEN_ATTRIBUTES:
            logger.info("patch rejected: forbidden attribute access %r", node.attr)
            return False

        if isinstance(node, ast.Name) and node.id in FORBIDDEN_NAMES:
            logger.info("patch rejected: forbidden name %r", node.id)
            return False

        return True

    @staticmethod
    def _check_import(node: ast.Import | ast.ImportFrom) -> bool:
        modules = (
            [node.module or ""] if isinstance(node, ast.ImportFrom)
            else [alias.name for alias in node.names]
        )
        for module in modules:
            root = module.split(".")[0]
            if root and root not in ALLOWED_IMPORTS:
                logger.info("patch rejected: forbidden import %r", root)
                return False
            if root == "self_harness" and not module.startswith(ALLOWED_SELF_HARNESS_PREFIX):
                logger.info("patch rejected: self_harness import outside core: %r", module)
                return False
        return True
