"""Rollback handler protocol + shared error type.

The protocol is intentionally minimal: two methods, symmetric by design.
Implementations are free to manage state however they like — git stash,
docker snapshot, S3 version pin — provided the round-trip is idempotent.

Conventions:

* ``on_branch`` is called *before* the agent starts writing — it should
  snapshot the current state and tag it with ``branch_id``.
* ``on_rewind`` is called when a branch is being abandoned — it should
  restore the snapshot taken at ``on_branch`` time and clean up the tag.
* Branch ids are UUIDs (the :class:`~rewind.replay.ReplaySession.branch_id`).
  Handlers must namespace their own state under that id so concurrent
  branches don't collide.
* Handlers **must not raise out of the protocol methods** except via
  :class:`RollbackError`. A failure to stash is a rollback failure, not
  an application bug — but it should abort the branching, not silently
  continue with corrupted state.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from uuid import UUID


class RollbackError(RuntimeError):
    """Raised when a rollback handler cannot snapshot or restore state.

    Catch this around ``session.fork(...)`` / ``with replay(...):`` if
    your integration can degrade gracefully (e.g. fall back to FROZEN
    replay with no forward captures). The default behaviour is to abort:
    a corrupted snapshot is worse than no snapshot.
    """


@runtime_checkable
class RollbackHandler(Protocol):
    """Symmetric snapshot/restore interface for non-checkpoint state.

    See module docstring for the protocol contract. The two methods must
    be idempotent (calling ``on_branch`` twice with the same branch_id is
    a refresh, not an error; calling ``on_rewind`` on an unknown id is a
    no-op, not an error).
    """

    def on_branch(self, branch_id: UUID) -> None:
        """Snapshot the current state and tag it with ``branch_id``.

        Called by the active :class:`~rewind.replay.ReplaySession` before
        yielding to a BRANCH / FULL_RERUN re-run. Must succeed before the
        agent starts writing; raise :class:`RollbackError` if not.
        """
        raise NotImplementedError

    def on_rewind(self, branch_id: UUID) -> None:
        """Restore the state saved at ``on_branch`` time and drop the tag.

        Called when a branch is being abandoned (the user navigates back
        to the parent). Must restore the exact pre-branch state; idempotent
        for an unknown id. Raise :class:`RollbackError` on restoration
        failure — *never* silently leave the working tree in a partial state.
        """
        raise NotImplementedError


__all__ = ["RollbackError", "RollbackHandler"]
