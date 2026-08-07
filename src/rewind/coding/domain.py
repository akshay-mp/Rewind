"""Typed, persistence-friendly coding-agent domain objects.

The domain deliberately contains observable execution facts only. It has no
field for hidden chain-of-thought or private model reasoning.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any
from uuid import uuid4


def now_iso() -> str:
    """Return a sortable UTC timestamp."""
    return datetime.now(UTC).isoformat()


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"
    STOPPED = "stopped"
    INTERRUPTED_RECOVERABLE = "interrupted_recoverable"


class Verdict(StrEnum):
    PASS = "pass"  # noqa: S105 - verdict label, not a credential
    FAIL = "fail"
    UNKNOWN = "unknown"
    ERROR = "error"


class CapabilityTier(StrEnum):
    OBSERVE = "observe"
    ASSIST = "assist"
    AUTONOMOUS = "autonomous"


class EventGuarantee(StrEnum):
    BEST_EFFORT = "best_effort"
    DURABLE = "durable"
    APPEND_ONLY = "append_only"


@dataclass(frozen=True, slots=True)
class AgentCapability:
    """A capability and its safety tier, exposed to the control plane."""

    name: str
    tier: CapabilityTier
    description: str = ""
    guarantees: tuple[EventGuarantee, ...] = (EventGuarantee.DURABLE,)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "tier": self.tier.value,
            "description": self.description,
            "guarantees": [item.value for item in self.guarantees],
        }


@dataclass(frozen=True, slots=True)
class CheckSpec:
    """One trusted, deterministic assertion in a goal profile."""

    kind: str
    value: Any = None
    path: str | None = None
    maximum: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "value": self.value, "path": self.path, "maximum": self.maximum}


@dataclass(frozen=True, slots=True)
class GoalProfile:
    """Immutable/versioned evaluation policy."""

    profile_id: str = field(default_factory=lambda: str(uuid4()))
    name: str = "Coding goal"
    version: int = 1
    checks: tuple[CheckSpec, ...] = ()
    created_at: str = field(default_factory=now_iso)
    immutable: bool = True
    trusted: bool = True
    protected: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "name": self.name,
            "version": self.version,
            "checks": [check.to_dict() for check in self.checks],
            "created_at": self.created_at,
            "immutable": self.immutable,
            "trusted": self.trusted,
            "protected": self.protected,
            "metadata": self.metadata,
        }


@dataclass(slots=True)
class CodingRun:
    run_id: str = field(default_factory=lambda: str(uuid4()))
    workspace_path: str = ""
    task: str = ""
    adapter_name: str = "demo"
    goal_profile_id: str = ""
    status: RunStatus = RunStatus.CREATED
    revision: int = 0
    attempt_limit: int = 3
    time_budget_seconds: int = 1800
    token_budget: int = 200_000
    cost_budget_usd: float = 0.0
    terminal_reason: str | None = None
    created_at: str = field(default_factory=now_iso)
    updated_at: str = field(default_factory=now_iso)
    active_attempt_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CodingAttempt:
    attempt_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: str = ""
    number: int = 1
    status: str = "running"
    started_at: str = field(default_factory=now_iso)
    finished_at: str | None = None
    before_snapshot_id: str | None = None
    after_snapshot_id: str | None = None
    repair_of_attempt_id: str | None = None
    output: str = ""
    tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CodingEvent:
    run_id: str
    kind: str
    payload: dict[str, Any] = field(default_factory=dict)
    sequence: int = 0
    event_id: str = field(default_factory=lambda: str(uuid4()))
    attempt_id: str | None = None
    parent_event_id: str | None = None
    request_id: str | None = None
    effect_class: str = "observable"
    source: str = "live"
    created_at: str = field(default_factory=now_iso)


@dataclass(slots=True)
class ControlLease:
    run_id: str
    owner: str
    lease_id: str = field(default_factory=lambda: str(uuid4()))
    mode: str = "automation"
    expires_at: str = ""
    heartbeat_at: str = field(default_factory=now_iso)
    active: bool = True


@dataclass(slots=True)
class WorkspaceSnapshot:
    snapshot_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: str = ""
    attempt_id: str | None = None
    tree_hash: str = ""
    path: str = ""
    created_at: str = field(default_factory=now_iso)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class WorkspaceChange:
    change_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: str = ""
    attempt_id: str | None = None
    path: str = ""
    change_type: str = "modified"
    before: str = ""
    after: str = ""
    created_at: str = field(default_factory=now_iso)


@dataclass(slots=True)
class EvaluationResult:
    evaluation_id: str = field(default_factory=lambda: str(uuid4()))
    run_id: str = ""
    attempt_id: str | None = None
    profile_id: str = ""
    verdict: Verdict = Verdict.UNKNOWN
    evidence: dict[str, Any] = field(default_factory=dict)
    metrics: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=now_iso)
    error: str | None = None


__all__ = [
    "AgentCapability",
    "CapabilityTier",
    "CheckSpec",
    "CodingAttempt",
    "CodingEvent",
    "CodingRun",
    "ControlLease",
    "EvaluationResult",
    "EventGuarantee",
    "GoalProfile",
    "RunStatus",
    "Verdict",
    "WorkspaceChange",
    "WorkspaceSnapshot",
    "now_iso",
]
