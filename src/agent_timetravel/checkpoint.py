"""``timetravel.checkpoint()`` — capture and restore agent-visible state.

Phase 4's contract is that an agent which mutates the world (filesystem,
database, third-party APIs) can re-run under ``FROZEN`` replay and produce
*exactly* the same observable behaviour. We achieve this with named checkpoints:

.. code-block:: python

    from agent_timetravel import checkpoint

    with checkpoint("after_db_write", payload=state) as state_token:
        if state_token.restored:
            # Frozen replay served the recorded snapshot; skip side effects.
            actual_state = state_token.payload
        else:
            actual_state = do_expensive_db_write(...)
            state_token.capture(actual_state)

The block must work when:

* No replay session is active — pure pass-through (the agent just runs).
* A frozen replay is active and a recorded checkpoint matches — restore.
* A live forward (BRANCH / FULL_RERUN) is active and no record exists — capture.

The restore/capture decision is made by the active
:class:`~timetravel.replay.ReplaySession`, so the user-facing API stays symmetric
whether the agent is being recorded, replayed, or branched.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable

    from agent_timetravel.replay import ReplaySession
    from agent_timetravel.storage import TraceStore


@dataclass(frozen=True, slots=True)
class CheckpointToken:
    """Token handed to the agent inside a ``with checkpoint(...)`` block.

    ``restored`` is ``True`` when frozen replay served a recorded snapshot:
    the agent should skip its side-effecting body and consume ``payload``.

    ``restored`` is ``False`` on live capture or no-session pass-through; the
    agent runs its body. If a capture callback was supplied to the token it
    is invoked at ``__exit__`` to persist the resulting state.
    """

    name: str
    restored: bool
    payload: dict[str, Any] = field(default_factory=dict)

    # -- capture plumbing ---------------------------------------------------
    # When ``restored`` is False and a session is capturing, this is populated
    # by ``_capture_exit`` with a function returning the live state. It's an
    # implementation detail — agents should use ``.capture(state)`` instead.
    _capture_fn: Callable[[dict[str, Any]], None] | None = field(default=None, repr=False)

    def capture(self, live_state: dict[str, Any]) -> None:
        """Record ``live_state`` as this checkpoint's payload.

        No-op on a restored token (the snapshot already exists; we don't
        want to overwrite it). No-op when no replay session is active.
        """
        if self.restored:
            return
        if self._capture_fn is None:
            return
        self._capture_fn(live_state)


def _current_session() -> ReplaySession | None:
    """Return the active ReplaySession if any (lazy import to avoid cycles)."""
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.replay import active_session
    # pylint: enable=import-outside-toplevel

    return active_session()


@contextmanager
def checkpoint(
    name: str,
    *,
    payload: dict[str, Any] | None = None,
    label: str = "",
) -> Iterator[CheckpointToken]:
    """Restore-or-capture a named state checkpoint for the active session.

    Behaviour matrix:

    +-----------------------------+----------------------+-------------------+
    | State                       | ``restored``         | Side effect       |
    +=============================+======================+===================+
    | No active session           | ``False``            | None (pass-through)|
    +-----------------------------+----------------------+-------------------+
    | Frozen replay, recorded hit | ``True``             | None (state served)|
    +-----------------------------+----------------------+-------------------+
    | Frozen replay, no hit       | -- (raises)          | n/a — divergence  |
    +-----------------------------+----------------------+-------------------+
    | Branch / Full past cursor   | ``False``            | Captures on exit  |
    +-----------------------------+----------------------+-------------------+

    For the "frozen replay, recorded hit" case the checkpoint must already
    exist for this ``(branch_id, name)`` — on the first FROZEN run the agent
    has to capture them. The standard flow is:

    1. BRANCH run captures every checkpoint once into a branch.
    2. FROZEN runs of that branch restore the captured snapshots.

    Args:
        name: Stable identifier for the checkpoint within a branch.
        payload: Optional precomputed state. When supplied, captured
            verbatim if the block didn't call ``.capture()``. Restored
            on FROZEN hit.
        label: Free-form label surfaced in the diff UI.

    Yields:
        CheckpointToken: Tells the agent whether to restore or capture.
    """
    session = _current_session()
    if session is None:
        # No replay active — pass straight through. The agent runs its
        # body normally; we yield a non-restoring token.
        yield CheckpointToken(name=name, restored=False, payload=payload or {})
        return

    store: TraceStore = session.store
    branch_id = session.branch_id

    recorded = store.get_checkpoint(branch_id, name)
    if recorded is not None:
        # Frozen replay — serve the recorded snapshot.
        # (Branch/Full also serves recorded checkpoints: they are part of
        # the agent's recorded behaviour up to the cursor. Only spans past
        # the cursor "go live"; checkpoints have the same boundary.)
        yield CheckpointToken(
            name=name,
            restored=True,
            payload=recorded.payload,
        )
        return

    # Live capture path (BRANCH / FULL_RERUN past cursor, or fresh branch).
    captured_payload: dict[str, Any] = dict(payload or {})

    def _persist(live_state: dict[str, Any]) -> None:
        # Late import to avoid a top-level cycle (models imports nothing
        # from this module; checkpoint imports nothing at module top).
        # pylint: disable=import-outside-toplevel
        from agent_timetravel.models import Checkpoint
        # pylint: enable=import-outside-toplevel

        captured_payload.update(live_state)
        store.upsert_checkpoint(
            Checkpoint(
                trace_id=session.trace_id,
                branch_id=branch_id,
                name=name,
                cursor_index=session.cursor,
                label=label,
                payload=captured_payload,
            )
        )

    token = CheckpointToken(
        name=name,
        restored=False,
        payload=captured_payload,
        _capture_fn=_persist,
    )
    yield token


__all__ = ["CheckpointToken", "checkpoint"]
