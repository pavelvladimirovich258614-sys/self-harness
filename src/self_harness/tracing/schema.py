"""Canonical execution-trace event model.

One row per span. This schema is mirrored in
`infra/bigquery/traces_schema.json` — keep the two in sync. Field choices are
driven by what the mutation engine and the analyzer need:

  * full payloads (not summaries) — the mutation model consumes raw traces;
  * harness_version on every event — enables cross-generation SQL comparisons;
  * parent/run linkage — supports tool-calling-graph reconstruction.
"""

from __future__ import annotations

import enum
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field


class SpanKind(str, enum.Enum):
    RUN_START = "run_start"
    MODEL_CALL = "model_call"
    TOOL_CALL = "tool_call"
    ERROR = "error"
    TERMINATE = "terminate"
    RUN_END = "run_end"


class TraceEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: uuid.uuid4().hex)
    run_id: str
    harness_version: str
    kind: SpanKind
    timestamp: float = Field(default_factory=time.time)
    duration_s: float = 0.0
    step: int = 0
    payload: dict[str, Any] = Field(default_factory=dict)

    def to_bq_row(self) -> dict[str, Any]:
        """Flatten for BigQuery streaming insert (payload stored as JSON)."""
        row = self.model_dump()
        row["kind"] = self.kind.value
        import json

        row["payload"] = json.dumps(row["payload"], default=str)
        return row
