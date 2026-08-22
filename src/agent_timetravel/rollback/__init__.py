"""Pluggable rollback handlers for filesystem-anchored agent state.

Phase 4 guarantees "an agent using :func:`timetravel.checkpoint` restores full
state after a timetravel". Checkpoints cover *arbitrary state captured by the
agent itself*; this module covers *state the agent writes to the filesystem*.

The pattern: when a session forks a branch, the on-disk state should be
preserved as a snapshot. When the branch is rewound, the snapshot is restored.
The reference implementation uses ``git stash`` keyed by a branch-id tag.

Handlers are intentionally pluggable — agents whose side effects land in
MySQL, S3, etc. can implement the :class:`RollbackHandler` protocol without
touching TimeTravel's core.

A handler is **always optional**. If none is registered, branching and
replay proceed without filesystem state management (correct only for
agents that checkpoint their own state — see the Phase 4 security doc).
"""

from __future__ import annotations

from agent_timetravel.rollback.base import RollbackError, RollbackHandler

__all__ = ["RollbackError", "RollbackHandler"]
