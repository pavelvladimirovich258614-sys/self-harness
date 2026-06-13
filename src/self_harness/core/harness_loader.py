"""Dynamic harness loading.

Boots the agent of generation N+1 with the harness artifact produced by
generation N. A harness version is a directory containing:

    spec.json      — serialized HarnessSpec (prompts, tools, policies)
    harness.py     — the procedural control policy (mutated source)

Loading executes the candidate `harness.py` in a fresh module namespace and
binds its `Harness` class to the deserialized spec. Untrusted candidates are
only ever loaded inside the sandbox; the orchestrator loads exclusively
versions that passed validation, sandbox evaluation, and statistical gating.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Callable

from .harness import Harness, HarnessSpec


class HarnessLoader:
    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)

    def load(self, version: str, model_call: Callable) -> Harness:
        """Instantiate a registered harness version."""
        version_dir = self.artifacts_dir / version
        spec = HarnessSpec.model_validate_json((version_dir / "spec.json").read_text())

        code_path = version_dir / "harness.py"
        if code_path.exists():
            harness_cls = self._load_class(code_path, version)
            return harness_cls(spec=spec, model_call=model_call)
        # Genesis fallback: the built-in reference implementation.
        return Harness(spec=spec, model_call=model_call)

    @staticmethod
    def _load_class(code_path: Path, version: str) -> type[Harness]:
        module_name = f"self_harness_artifact_{version}"
        spec = importlib.util.spec_from_file_location(module_name, code_path)
        if spec is None or spec.loader is None:
            raise ImportError(f"cannot load harness artifact at {code_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        harness_cls = getattr(module, "Harness", None)
        if harness_cls is None:
            raise ImportError(f"artifact {version} does not define a Harness class")
        return harness_cls
