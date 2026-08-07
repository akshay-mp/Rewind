"""Backend primitives for Rewind's coding-agent control plane."""

from rewind.coding.adapters import CodingAdapter, DemoCodingAdapter
from rewind.coding.controller import CodingController, ControlConflict, LeaseRequired
from rewind.coding.domain import (
    AgentCapability,
    CapabilityTier,
    CheckSpec,
    CodingAttempt,
    CodingEvent,
    CodingRun,
    ControlLease,
    EvaluationResult,
    EventGuarantee,
    GoalProfile,
    RunStatus,
    Verdict,
    WorkspaceChange,
    WorkspaceSnapshot,
)
from rewind.coding.evaluator import evaluate_goal
from rewind.coding.runtime import DockerContainerRuntime, RuntimeConfig
from rewind.coding.workspace import GitWorktreeWorkspaceProvider

__all__ = [
    "AgentCapability",
    "CapabilityTier",
    "CheckSpec",
    "CodingAdapter",
    "CodingAttempt",
    "CodingController",
    "CodingEvent",
    "CodingRun",
    "ControlConflict",
    "ControlLease",
    "DemoCodingAdapter",
    "DockerContainerRuntime",
    "EvaluationResult",
    "EventGuarantee",
    "GitWorktreeWorkspaceProvider",
    "GoalProfile",
    "LeaseRequired",
    "RunStatus",
    "RuntimeConfig",
    "Verdict",
    "WorkspaceChange",
    "WorkspaceSnapshot",
    "evaluate_goal",
]
