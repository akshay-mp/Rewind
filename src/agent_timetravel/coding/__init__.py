"""Backend primitives for TimeTravel's coding-agent control plane."""

from agent_timetravel.coding.adapters import CodingAdapter, DemoCodingAdapter
from agent_timetravel.coding.controller import CodingController, ControlConflict, LeaseRequired
from agent_timetravel.coding.domain import (
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
from agent_timetravel.coding.evaluator import evaluate_goal
from agent_timetravel.coding.runtime import DockerContainerRuntime, RuntimeConfig
from agent_timetravel.coding.workspace import GitWorktreeWorkspaceProvider

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
