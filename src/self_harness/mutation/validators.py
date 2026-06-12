"""Static safety gates for generated harness patches.

First line of defense, applied before any sandbox execution:

  gate 1 — syntax:     the source must parse.
  gate 2 — imports:    only an allowlist of modules; no os/subprocess/socket.
  gate 3 — contract:   a Harness class with the required methods must exist.
  gate 4 — spec:       the new spec must deserialize into HarnessSpec.

Patches failing any gate are discarded without execution. Sandbox isolation
remains the real boundary; these gates only cut cost and noise.
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
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                module = (node.module if isinstance(node, ast.ImportFrom)
                          else node.names[0].name) or ""
                root = module.split(".")[0]
                if root and root not in ALLOWED_IMPORTS:
                    logger.info("patch rejected: forbidden import %r", root)
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
