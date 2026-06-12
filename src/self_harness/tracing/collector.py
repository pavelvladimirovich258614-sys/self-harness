"""In-process trace recorder.

Buffers structured spans during agent execution and flushes them to a sink
(in-memory for local iteration, BigQuery for cluster runs). The collector is
the only component the agent talks to — sink selection is configuration.
"""

from __future__ import annotations

import time
from typing import Any, Protocol

from .schema import SpanKind, TraceEvent


class TraceSink(Protocol):
    def write(self, events: list[TraceEvent]) -> None: ...


class MemorySink:
    """Default sink for laptop-scale iteration and tests."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def write(self, events: list[TraceEvent]) -> None:
        self.events.extend(events)


class TraceCollector:
    def __init__(self, sink: TraceSink, flush_every: int = 50):
        self.sink = sink
        self.flush_every = flush_every
        self._buffer: list[TraceEvent] = []
        self._steps: dict[str, int] = {}
        self._versions: dict[str, str] = {}

    def start_run(self, run_id: str, harness_version: str, task: str) -> None:
        self._steps[run_id] = 0
        self._versions[run_id] = harness_version
        self._append(TraceEvent(
            run_id=run_id, harness_version=harness_version,
            kind=SpanKind.RUN_START, payload={"task": task},
        ))

    def record(self, run_id: str, kind: SpanKind, payload: dict[str, Any], started_at: float) -> None:
        self._steps[run_id] = self._steps.get(run_id, 0) + 1
        self._append(TraceEvent(
            run_id=run_id,
            harness_version=self._versions.get(run_id, "unknown"),
            kind=kind,
            duration_s=time.monotonic() - started_at,
            step=self._steps[run_id],
            payload=payload,
        ))

    def end_run(self, run_id: str, success: bool, steps: int) -> None:
        self._append(TraceEvent(
            run_id=run_id, harness_version=self._versions.get(run_id, "unknown"),
            kind=SpanKind.RUN_END, payload={"success": success, "steps": steps},
        ))
        self.flush()

    def flush(self) -> None:
        if self._buffer:
            self.sink.write(self._buffer)
            self._buffer = []

    def _append(self, event: TraceEvent) -> None:
        self._buffer.append(event)
        if len(self._buffer) >= self.flush_every:
            self.flush()
