"""Versioned harness artifact registry with full lineage.

Every harness version is immutable once registered. Lineage links each
version to the generation, trace batch, and patch hypothesis that produced
it, enabling rollback and post-hoc analysis of the evolutionary trajectory.

Local backend stores artifacts on disk; swap `artifacts_dir` for a GCS bucket
mount (or extend with a Cloud Storage backend) for cluster deployments.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Lineage:
    parent_version: str
    generation: int
    trace_batch_id: str
    hypothesis: str  # The mutation engine's falsifiable claim for this patch


class HarnessRegistry:
    def __init__(self, artifacts_dir: Path):
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)
        self._head_file = self.artifacts_dir / "HEAD"

    def current(self) -> str:
        if self._head_file.exists():
            return self._head_file.read_text().strip()
        return "genesis"

    @staticmethod
    def content_digest(spec_json: str, harness_source: str) -> str:
        return hashlib.sha256((spec_json + harness_source).encode()).hexdigest()[:12]

    def has_content(self, digest: str) -> bool:
        """Tabu check: a content digest already registered in any generation.

        Blocks mutation oscillation (A -> B -> A): a patch whose content
        matches any previously registered version is never re-evaluated.
        """
        return any(
            p.is_dir() and p.name.endswith(f"-{digest}")
            for p in self.artifacts_dir.iterdir()
        )

    def register(self, spec_json: str, harness_source: str, lineage: Lineage) -> str:
        """Store a candidate version. Content-addressed; does not move HEAD."""
        digest = self.content_digest(spec_json, harness_source)
        version = f"g{lineage.generation:04d}-{digest}"
        version_dir = self.artifacts_dir / version
        version_dir.mkdir(parents=True, exist_ok=True)
        (version_dir / "spec.json").write_text(spec_json)
        (version_dir / "harness.py").write_text(harness_source)
        (version_dir / "lineage.json").write_text(
            json.dumps({**asdict(lineage), "registered_at": time.time()}, indent=2)
        )
        return version

    def promote(self, version: str) -> None:
        """Move HEAD to a version that passed evaluation gating."""
        if not (self.artifacts_dir / version).exists():
            raise ValueError(f"unknown harness version: {version}")
        self._head_file.write_text(version)

    def rollback(self) -> str:
        """Demote HEAD to its parent. Triggered on production regression."""
        lineage_file = self.artifacts_dir / self.current() / "lineage.json"
        parent = json.loads(lineage_file.read_text())["parent_version"]
        self._head_file.write_text(parent)
        return parent
