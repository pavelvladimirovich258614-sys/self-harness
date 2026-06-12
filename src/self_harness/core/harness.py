"""Harness artifact: the mutable subject of self-optimization.

Everything traditionally hard-coded around a model lives here as data + code:
system prompt, tool schemas, context-management policy, retry and termination
logic. The mutation engine is only permitted to rewrite this module and its
companion prompt/tool definitions — nothing else in the package.

Contract invariants (enforced by mutation.validators and tests):
  * `Harness` must remain constructible from a `HarnessSpec`.
  * `Harness.next_action(state)` must return an `Action` within the step budget.
  * No network or filesystem side effects outside the provided tool registry.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from pydantic import BaseModel, Field


class ToolDef(BaseModel):
    """JSON-schema tool definition exposed to the model."""

    name: str
    description: str
    parameters: dict[str, Any] = Field(default_factory=dict)
    handler: str = ""  # Dotted path to the callable executing the tool


class HarnessSpec(BaseModel):
    """Declarative portion of the harness. Serialized alongside the code."""

    version: str = "genesis"
    system_prompt: str = (
        "You are a long-horizon autonomous agent. Decompose the task, use the "
        "available tools, verify intermediate results, and stop when the goal "
        "is met or provably unreachable."
    )
    tools: list[ToolDef] = Field(default_factory=list)
    max_retries: int = 3
    context_window_policy: str = "truncate_oldest"  # truncate_oldest | summarize | full
    termination_keywords: list[str] = Field(default_factory=lambda: ["TASK_COMPLETE"])


@dataclass
class AgentState:
    task: str
    messages: list[dict[str, Any]] = field(default_factory=list)
    step: int = 0
    done: bool = False


@dataclass
class Action:
    kind: str  # "model_call" | "tool_call" | "terminate"
    payload: dict[str, Any] = field(default_factory=dict)


class Harness:
    """Procedural portion of the harness: the control loop policy.

    The mutation engine rewrites the bodies of these methods between
    generations — e.g. replacing naive retry-on-any-error with error-class
    aware backoff, or swapping the context policy after trace analysis shows
    truncation-induced failures.
    """

    def __init__(self, spec: HarnessSpec, model_call: Callable[[list[dict]], dict]):
        self.spec = spec
        self._model_call = model_call

    def build_context(self, state: AgentState) -> list[dict[str, Any]]:
        """Assemble the model context according to the active context policy."""
        messages = [{"role": "system", "content": self.spec.system_prompt}]
        history = state.messages
        if self.spec.context_window_policy == "truncate_oldest" and len(history) > 80:
            # Keep the task statement and the most recent exchanges.
            history = history[:1] + history[-79:]
        messages.extend(history)
        return messages

    def next_action(self, state: AgentState) -> Action:
        """Decide the next step. This is the primary mutation surface."""
        if state.done or self._is_terminal(state):
            return Action(kind="terminate")

        response = self._call_with_retries(self.build_context(state))
        if response.get("tool_call"):
            return Action(kind="tool_call", payload=response["tool_call"])
        return Action(kind="model_call", payload=response)

    def _call_with_retries(self, messages: list[dict[str, Any]]) -> dict[str, Any]:
        last_error: Exception | None = None
        for _ in range(self.spec.max_retries):
            try:
                return self._model_call(messages)
            except Exception as exc:  # noqa: BLE001 — retry policy is itself mutable
                last_error = exc
        raise RuntimeError(f"model call failed after {self.spec.max_retries} retries") from last_error

    def _is_terminal(self, state: AgentState) -> bool:
        if not state.messages:
            return False
        last = json.dumps(state.messages[-1])
        return any(kw in last for kw in self.spec.termination_keywords)
