"""Model-facing agent. Deliberately thin and harness-agnostic.

The agent owns nothing that should be learned: it executes whatever control
policy the currently loaded `Harness` dictates and emits a structured trace
span for every step. All orchestration intelligence lives in the harness so
that the mutation engine has a single, well-bounded rewrite target.
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Callable

from ..tracing.collector import TraceCollector
from ..tracing.schema import SpanKind
from .harness import AgentState, Harness


class Agent:
    def __init__(
        self,
        harness: Harness,
        tools: dict[str, Callable[..., Any]],
        collector: TraceCollector,
        max_steps: int = 60,
        payload_max_chars: int = 65_536,
    ):
        self.harness = harness
        self.tools = tools
        self.collector = collector
        self.max_steps = max_steps
        # Raw-trace fidelity matters: the mutation engine consumes these
        # payloads verbatim. The cap exists only to bound pathological tool
        # outputs; it is set high and configurable, never a summary.
        self.payload_max_chars = payload_max_chars

    def run(self, task: str) -> AgentState:
        """Execute one long-horizon episode under the current harness."""
        run_id = uuid.uuid4().hex
        state = AgentState(task=task, messages=[{"role": "user", "content": task}])
        self.collector.start_run(run_id, harness_version=self.harness.spec.version, task=task)

        while state.step < self.max_steps and not state.done:
            t0 = time.monotonic()
            try:
                action = self.harness.next_action(state)
            except Exception as exc:  # Harness failures are signal, not noise.
                self.collector.record(run_id, SpanKind.ERROR, {"error": repr(exc)}, t0)
                break

            if action.kind == "terminate":
                state.done = True
                self.collector.record(run_id, SpanKind.TERMINATE, {}, t0)
            elif action.kind == "tool_call":
                result = self._execute_tool(action.payload)
                state.messages.append({"role": "tool", "content": result})
                self.collector.record(
                    run_id, SpanKind.TOOL_CALL,
                    {"tool": action.payload.get("name"), "args": action.payload.get("args"),
                     "result_preview": str(result)[: self.payload_max_chars]},
                    t0,
                )
            else:
                state.messages.append({"role": "assistant", "content": action.payload})
                self.collector.record(run_id, SpanKind.MODEL_CALL, action.payload, t0)

            state.step += 1

        self.collector.end_run(run_id, success=state.done, steps=state.step)
        return state

    def _execute_tool(self, payload: dict[str, Any]) -> Any:
        name = payload.get("name", "")
        handler = self.tools.get(name)
        if handler is None:
            return {"error": f"unknown tool: {name}"}
        try:
            return handler(**payload.get("args", {}))
        except Exception as exc:  # noqa: BLE001 — tool errors feed the mutation signal
            return {"error": repr(exc)}
