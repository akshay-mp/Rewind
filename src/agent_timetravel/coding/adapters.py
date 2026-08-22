"""Adapter boundary and deterministic demo implementation."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agent_timetravel.coding.domain import (
    AgentCapability,
    CapabilityTier,
    CodingAttempt,
    CodingEvent,
    CodingRun,
    EventGuarantee,
)


@dataclass(frozen=True, slots=True)
class AdapterResult:
    output: str = ""
    context: dict[str, object] = field(default_factory=dict)
    tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


class CodingAdapter(Protocol):
    """Observable execution contract; implementations must not expose CoT."""

    capabilities: tuple[AgentCapability, ...]

    def execute(
        self,
        run: CodingRun,
        attempt: CodingAttempt,
        emit: Callable[[CodingEvent], None],
    ) -> AdapterResult:
        """Execute one attempt and report durable observable events."""


class DemoCodingAdapter:
    """A deterministic adapter useful for local demos and controller tests."""

    capabilities = (
        AgentCapability("read_workspace", CapabilityTier.OBSERVE, "Read managed files."),
        AgentCapability("write_workspace", CapabilityTier.AUTONOMOUS, "Write managed files."),
        AgentCapability(
            "durable_events",
            CapabilityTier.ASSIST,
            "Emits lifecycle events.",
            (EventGuarantee.DURABLE, EventGuarantee.APPEND_ONLY),
        ),
    )

    def __init__(self, output: str = "demo coding result") -> None:
        self.output = output
        self.calls = 0

    def execute(
        self,
        run: CodingRun,
        attempt: CodingAttempt,
        emit: Callable[[CodingEvent], None],
    ) -> AdapterResult:
        self.calls += 1
        emit(
            CodingEvent(
                run.run_id,
                "attempt_started",
                {"number": attempt.number},
                attempt_id=attempt.attempt_id,
            )
        )
        emit(
            CodingEvent(
                run.run_id,
                "progress",
                {"phase": "deterministic_demo"},
                attempt_id=attempt.attempt_id,
            )
        )
        emit(
            CodingEvent(
                run.run_id,
                "attempt_output",
                {"length": len(self.output)},
                attempt_id=attempt.attempt_id,
            )
        )
        return AdapterResult(
            output=self.output, context={"command_exit_status": 0}, tokens=32, duration_seconds=0.01
        )


__all__ = ["AdapterResult", "CodingAdapter", "DemoCodingAdapter"]
