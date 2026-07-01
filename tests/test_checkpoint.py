"""Unit tests for :mod:`rewind.checkpoint` (Phase 4).

Covers the three paths in the behaviour matrix:

* No active session — pass-through.
* Frozen replay with a recorded checkpoint — restore.
* Live forward (BRANCH / FULL_RERUN) — capture on exit, restore next time.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from uuid import UUID

import pytest

from rewind.checkpoint import CheckpointToken, checkpoint
from rewind.enums import SpanKind
from rewind.models import Checkpoint, Span
from rewind.replay import ReplaySession
from rewind.storage import TraceStore


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    """A TraceStore rooted at a tmp file — isolated per-test."""
    return TraceStore(str(tmp_path / "checkpoint-test.db"))


@pytest.fixture
def trace_id(store: TraceStore) -> str:
    """Insert a minimal trace + one LLM span so replay can load it."""
    from rewind.models import Trace

    tid = "f" * 32
    s = Span(
        trace_id=tid,
        span_id="0" * 16,
        name="prompt",
        kind=SpanKind.LLM,
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-01T00:00:01Z",
        raw_attributes={},
    )
    store.upsert_trace(Trace(trace_id=tid, spans=[s]))
    return tid


# ----------------------------------------------------------------------
# Path 1: no session — pass-through
# ----------------------------------------------------------------------
def test_checkpoint_no_session_is_passthrough() -> None:
    """With no active ReplaySession, ``checkpoint`` yields restored=False."""
    with checkpoint("noop") as token:
        assert isinstance(token, CheckpointToken)
        assert token.restored is False
        assert token.payload == {}


def test_checkpoint_no_session_capture_is_noop() -> None:
    """``.capture()`` outside a session doesn't raise and doesn't persist."""
    # capture() is a no-op when there is no capture_fn (no session).
    with checkpoint("noop", payload={"pre": 1}) as token:
        token.capture({"post": 2})
        assert token.payload == {"pre": 1}


# ----------------------------------------------------------------------
# Path 2: frozen replay serves a recorded checkpoint
# ----------------------------------------------------------------------
def test_checkpoint_restores_recorded_payload(
    store: TraceStore, trace_id: str
) -> None:
    """A pre-recorded checkpoint is surfaced to the agent on FROZEN replay."""
    # Seed a recorded checkpoint for the root branch.
    session = ReplaySession.for_root(store, trace_id)
    store.upsert_checkpoint(
        Checkpoint(
            trace_id=trace_id,
            branch_id=session.branch_id,
            name="after_db_write",
            cursor_index=session.cursor,
            payload={"rows": 42, "tables": ["users"]},
        )
    )

    with _bind(session), checkpoint("after_db_write") as token:
        assert token.restored is True
        assert token.payload == {"rows": 42, "tables": ["users"]}


def test_checkpoint_restore_is_readonly_on_payload(
    store: TraceStore, trace_id: str
) -> None:
    """Mutating the restored payload does NOT write back to the DB.

    Restored tokens are immutable snapshots; calling ``.capture()`` is
    a documented no-op on a restored token.
    """
    session = ReplaySession.for_root(store, trace_id)
    store.upsert_checkpoint(
        Checkpoint(
            trace_id=trace_id,
            branch_id=session.branch_id,
            name="seed",
            cursor_index=0,
            payload={"x": 1},
        )
    )
    with _bind(session), checkpoint("seed") as token:
        assert token.restored
        token.capture({"x": 999})  # must not persist

    # Re-read directly from store to prove nothing changed.
    again = store.get_checkpoint(session.branch_id, "seed")
    assert again is not None
    assert again.payload == {"x": 1}


# ----------------------------------------------------------------------
# Path 3: live forward captures on exit
# ----------------------------------------------------------------------
def test_checkpoint_captures_on_live_forward(
    store: TraceStore, trace_id: str
) -> None:
    """A BRANCH session with no prior record captures the live state on exit."""
    from rewind.enums import ReplayMode

    root = ReplaySession.for_root(store, trace_id)
    branched = root.fork(branch_at=0, mode=ReplayMode.BRANCH, label="test")

    with _bind(branched), checkpoint("first_write", label="my label") as token:
        assert token.restored is False
        # Simulate the agent finishing its side-effecting block:
        token.capture({"files": ["a.txt", "b.txt"]})

    persisted = store.get_checkpoint(branched.branch_id, "first_write")
    assert persisted is not None
    assert persisted.payload == {"files": ["a.txt", "b.txt"]}
    assert persisted.label == "my label"
    assert persisted.cursor_index == branched.cursor


def test_checkpoint_captured_payload_restores_on_subsequent_frozen(
    store: TraceStore, trace_id: str
) -> None:
    """A checkpoint captured in a BRANCH run is served back in FROZEN.

    This is the headline Phase 4 contract: an agent using
    ``rewind.checkpoint`` restores full state after a rewind.
    """
    from rewind.enums import ReplayMode

    root = ReplaySession.for_root(store, trace_id)
    branched = root.fork(branch_at=0, mode=ReplayMode.BRANCH, label="cap")

    # First run: capture.
    with _bind(branched), checkpoint("write", payload={"hint": "abc"}) as token:
        assert not token.restored
        token.capture({"rows": 7})

    # Second run on the SAME branch but FROZEN — serve back.
    frozen = ReplaySession(
        store=store,
        trace_id=trace_id,
        branch_id=branched.branch_id,
        mode=ReplayMode.FROZEN,
        label="frozen-replay",
    )
    with _bind(frozen), checkpoint("write") as token:
        assert token.restored
        # Captured payload merged with the supplied payload.
        assert token.payload == {"hint": "abc", "rows": 7}


def test_checkpoint_no_capture_call_is_safe(
    store: TraceStore, trace_id: str
) -> None:
    """A live-forward block that never calls ``.capture()`` doesn't crash.

    Some agents might use ``checkpoint(name, payload=...)`` purely to
    register intent without live capture — the supplied ``payload`` is
    what gets persisted (if anything).
    """
    from rewind.enums import ReplayMode

    root = ReplaySession.for_root(store, trace_id)
    branched = root.fork(branch_at=0, mode=ReplayMode.BRANCH)

    with _bind(branched), checkpoint("intent", payload={"preset": True}) as token:
        # No call to token.capture(...) — that's the point of the test.
        _ = token

    # Because capture() was never called, _persist is never invoked, so
    # nothing should be on disk under this name.
    assert store.get_checkpoint(branched.branch_id, "intent") is None


# ----------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------
@contextmanager
def _bind(session: ReplaySession) -> Iterator[None]:
    """Bind ``session`` to the active-session ContextVar for the duration.

    Mirrors what :func:`rewind.replay.replay` does, so we can test
    :func:`checkpoint` without going through the public context manager
    (which would advance the cursor itself).
    """
    # pylint: disable=import-outside-toplevel,protected-access
    from rewind.replay import _active_session
    # pylint: enable=import-outside-toplevel,protected-access

    token = _active_session.set(session)
    try:
        yield
    finally:
        _active_session.reset(token)


_ = UUID  # silence unused-import when TYPE_CHECKING-only in some checkers
