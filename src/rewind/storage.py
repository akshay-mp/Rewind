"""SQLite persistence for Rewind traces.

Design notes
------------
- **WAL mode** for concurrent read (timeline UI) / write (OTLP receiver)
  workloads without blocking. Set on every connection.
- **Verbatim fidelity**: the ``raw_attributes`` column is JSON text that must
  round-trip byte-for-byte with the ingested OpenInference payload. This is the
  Phase 1 exit criterion (``hash(span.attributes["gen_ai.prompt"]) matches``).
- **One file per workspace** (default ``~/.rewind/rewind.db``). Zero-config.
- We use stdlib ``sqlite3`` rather than an ORM to keep the dependency surface
  small and the queries auditable; ``Span`` is the only thing crossing the
  boundary, and it serializes via Pydantic.
"""

from __future__ import annotations

import contextlib
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import TypeVar
from uuid import UUID

from rewind.enums import SpanKind, SpanStatus
from rewind.models import Branch, Span, Trace

#: Default schema version of the on-disk DB. Bump + migrate on breaking changes.
SCHEMA_VERSION = 1

#: Generic return type for ``TraceStore._execute``.
_T = TypeVar("_T")

#: The canonical DDL. Persisted in ``PRAGMA user_version`` on init.
_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS traces (
    trace_id          TEXT PRIMARY KEY,
    root_branch_id    TEXT NOT NULL,
    created_at        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS branches (
    branch_id         TEXT PRIMARY KEY,
    trace_id          TEXT NOT NULL REFERENCES traces(trace_id),
    parent_branch_id  TEXT,
    branch_at_index   INTEGER,
    mode              TEXT NOT NULL DEFAULT 'frozen',
    label             TEXT NOT NULL DEFAULT '',
    created_at        TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_branches_trace ON branches(trace_id);

CREATE TABLE IF NOT EXISTS spans (
    rewind_id         TEXT PRIMARY KEY,
    trace_id          TEXT NOT NULL REFERENCES traces(trace_id),
    span_id           TEXT NOT NULL,
    parent_span_id    TEXT,
    branch_id         TEXT NOT NULL DEFAULT '',
    name              TEXT NOT NULL,
    kind              TEXT NOT NULL,
    start_time        TEXT NOT NULL,
    end_time          TEXT NOT NULL,
    status            TEXT NOT NULL,
    status_message    TEXT,
    model_name        TEXT,
    prompt_tokens     INTEGER,
    completion_tokens INTEGER,
    total_tokens      INTEGER,
    messages_hash     TEXT,
    tools_hash        TEXT,
    raw_attributes    TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_spans_trace_branch ON spans(trace_id, branch_id);
CREATE INDEX IF NOT EXISTS idx_spans_kind ON spans(kind);
CREATE INDEX IF NOT EXISTS idx_spans_model ON spans(model_name);
"""


class TraceStore:
    """A SQLite-backed store for traces, spans, and branches.

    The store is opened read-write by default and initialises the schema on
    first use. Connections enable WAL and a short ``busy_timeout`` so the
    Phase 1 receiver can write while the UI reads.
    """

    def __init__(self, db_path: str | Path) -> None:
        """Open or create a store at ``db_path``."""
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # -- connection management ------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        """Open a configured connection. Caller is responsible for closing.

        We use ``isolation_level=None`` (autocommit) and manage transactions
        manually in ``_execute`` so PRAGMAs (which implicitly commit in legacy
        mode) don't fight our BEGIN/COMMIT wrapper.
        """
        # ``check_same_thread=False`` because FastAPI runs handlers in a
        # threadpool; we rely on WAL + transactions for safety.
        conn = sqlite3.connect(self.db_path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        return conn

    def _execute(self, fn: Callable[[sqlite3.Connection], _T]) -> _T:
        """Open a connection, run ``fn(conn)`` in a transaction, always close.

        Writes always COMMIT before the connection closes; reads run fine inside
        the same wrapper. Closing explicitly (the stdlib context manager only
        commits, it does not close) prevents file-descriptor leaks so WAL
        checkpoints can run.
        """
        conn = self._connect()
        in_txn = False
        try:
            conn.execute("BEGIN")
            in_txn = True
            result = fn(conn)
            conn.execute("COMMIT")
            in_txn = False
            return result
        finally:
            if in_txn:
                # Connection may already be torn down; nothing to roll back.
                with contextlib.suppress(sqlite3.Error):
                    conn.execute("ROLLBACK")
            conn.close()

    def _init_schema(self) -> None:
        """Create tables/views if missing and stamp the schema version.

        ``executescript`` issues its own COMMIT, so this bypasses ``_execute``
        (which wraps in BEGIN/COMMIT). Schema init is idempotent and single-shot.
        """
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        finally:
            conn.close()

    # -- traces ---------------------------------------------------------------
    def upsert_trace(self, trace: Trace) -> None:
        """Insert or update the trace root row (does not touch spans)."""

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO traces (trace_id, root_branch_id, created_at)
                VALUES (?, ?, ?)
                ON CONFLICT(trace_id) DO UPDATE SET
                    root_branch_id = excluded.root_branch_id
                """,
                (trace.trace_id, str(trace.root_branch_id), trace.created_at),
            )

        self._execute(_upsert)

    def get_trace(self, trace_id: str) -> Trace | None:
        """Load a trace with all spans on its *root* branch."""

        def _load(conn: sqlite3.Connection) -> Trace | None:
            row = conn.execute(
                "SELECT trace_id, root_branch_id, created_at FROM traces WHERE trace_id = ?",
                (trace_id,),
            ).fetchone()
            if row is None:
                return None
            spans = [
                self._span_from_row(r)
                for r in conn.execute(
                    """
                    SELECT * FROM spans
                    WHERE trace_id = ? AND (branch_id = '' OR branch_id = ?)
                    ORDER BY start_time, rowid
                    """,
                    (trace_id, str(row["root_branch_id"])),
                )
            ]
            return Trace(
                trace_id=row["trace_id"],
                root_branch_id=UUID(row["root_branch_id"]),
                created_at=row["created_at"],
                spans=spans,
            )

        return self._execute(_load)

    # -- spans ----------------------------------------------------------------
    def insert_span(self, span: Span, branch_id: UUID | None = None) -> None:
        """Persist a single span under ``branch_id`` (defaults to root branch).

        ``branch_id`` is empty string for the ingested original timeline, or a
        branch UUID for replay branches (Phase 3).
        """
        bid = "" if branch_id is None else str(branch_id)

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO spans (
                    rewind_id, trace_id, span_id, parent_span_id, branch_id,
                    name, kind, start_time, end_time, status, status_message,
                    model_name, prompt_tokens, completion_tokens, total_tokens,
                    messages_hash, tools_hash, raw_attributes
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
                )
                ON CONFLICT(rewind_id) DO UPDATE SET
                    end_time = excluded.end_time,
                    status = excluded.status,
                    status_message = excluded.status_message,
                    total_tokens = excluded.total_tokens,
                    raw_attributes = excluded.raw_attributes
                """,
                (
                    str(span.rewind_id),
                    span.trace_id,
                    span.span_id,
                    span.parent_span_id,
                    bid,
                    span.name,
                    span.kind.value,
                    span.start_time,
                    span.end_time,
                    span.status.value,
                    span.status_message,
                    span.model_name,
                    span.prompt_tokens,
                    span.completion_tokens,
                    span.total_tokens,
                    span.messages_hash,
                    span.tools_hash,
                    json.dumps(span.raw_attributes, sort_keys=True),
                ),
            )

        self._execute(_insert)

    def get_spans(self, trace_id: str, branch_id: UUID | None = None) -> list[Span]:
        """Return all spans for a trace/branch ordered by start time."""
        bid = "" if branch_id is None else str(branch_id)

        def _select(conn: sqlite3.Connection) -> list[Span]:
            rows = conn.execute(
                """
                SELECT * FROM spans
                WHERE trace_id = ? AND (branch_id = '' OR branch_id = ?)
                ORDER BY start_time, rowid
                """,
                (trace_id, bid),
            ).fetchall()
            return [self._span_from_row(r) for r in rows]

        return self._execute(_select)

    # -- listing (Phase 2 timeline API) --------------------------------------
    def list_traces(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[Trace], int]:
        """Return a page of traces (root branch only) and the total row count.

        Ordering is by ``rowid`` descending so the most recently ingested
        trace appears first. ``total`` is the count of all trace rows and
        ignores ``limit``/``offset`` — the UI uses it to render pagination
        affordances.
        """

        def _select(conn: sqlite3.Connection) -> tuple[list[Trace], int]:
            total = conn.execute("SELECT COUNT(*) AS n FROM traces").fetchone()["n"]
            rows = conn.execute(
                """
                SELECT trace_id, root_branch_id, created_at FROM traces
                ORDER BY rowid DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            traces: list[Trace] = []
            for row in rows:
                spans = [
                    self._span_from_row(r)
                    for r in conn.execute(
                        """
                        SELECT * FROM spans
                        WHERE trace_id = ?
                          AND (branch_id = '' OR branch_id = ?)
                        ORDER BY start_time, rowid
                        """,
                        (row["trace_id"], row["root_branch_id"]),
                    )
                ]
                traces.append(
                    Trace(
                        trace_id=row["trace_id"],
                        root_branch_id=UUID(row["root_branch_id"]),
                        created_at=row["created_at"],
                        spans=spans,
                    )
                )
            return traces, total

        return self._execute(_select)

    def get_span(self, rewind_id: UUID) -> Span | None:
        """Return a single span by ``rewind_id`` (across all branches/traces).

        Used by the timeline API's ``GET /api/v1/spans/{rewind_id}``. Returns
        ``None`` if no such span exists.
        """

        def _select(conn: sqlite3.Connection) -> Span | None:
            row = conn.execute(
                "SELECT * FROM spans WHERE rewind_id = ?",
                (str(rewind_id),),
            ).fetchone()
            return self._span_from_row(row) if row else None

        return self._execute(_select)

    # -- branches -------------------------------------------------------------
    def insert_branch(self, branch: Branch) -> None:
        """Persist a branch pointer. Spans for the branch are inserted separately."""

        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO branches (
                    branch_id, trace_id, parent_branch_id, branch_at_index,
                    mode, label, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    str(branch.branch_id),
                    branch.trace_id,
                    str(branch.parent_branch_id) if branch.parent_branch_id else None,
                    branch.branch_at_index,
                    branch.mode,
                    branch.label,
                    branch.created_at,
                ),
            )

        self._execute(_insert)

    def list_branches(self, trace_id: str) -> list[Branch]:
        """List all branches for a trace, root branch first."""

        def _select(conn: sqlite3.Connection) -> list[Branch]:
            rows = conn.execute(
                """
                SELECT * FROM branches WHERE trace_id = ?
                ORDER BY (parent_branch_id IS NULL) DESC, created_at
                """,
                (trace_id,),
            ).fetchall()
            return [self._branch_from_row(r) for r in rows]

        return self._execute(_select)

    # -- row mappers ----------------------------------------------------------
    @staticmethod
    def _span_from_row(row: sqlite3.Row) -> Span:
        """Reconstruct a ``Span`` from a DB row, preserving ``raw_attributes``."""
        return Span(
            rewind_id=UUID(row["rewind_id"]),
            trace_id=row["trace_id"],
            span_id=row["span_id"],
            parent_span_id=row["parent_span_id"],
            name=row["name"],
            kind=SpanKind(row["kind"]),
            start_time=row["start_time"],
            end_time=row["end_time"],
            status=SpanStatus(row["status"]),
            status_message=row["status_message"],
            model_name=row["model_name"],
            prompt_tokens=row["prompt_tokens"],
            completion_tokens=row["completion_tokens"],
            total_tokens=row["total_tokens"],
            messages_hash=row["messages_hash"],
            tools_hash=row["tools_hash"],
            raw_attributes=json.loads(row["raw_attributes"]),
        )

    @staticmethod
    def _branch_from_row(row: sqlite3.Row) -> Branch:
        return Branch(
            branch_id=UUID(row["branch_id"]),
            trace_id=row["trace_id"],
            parent_branch_id=UUID(row["parent_branch_id"]) if row["parent_branch_id"] else None,
            branch_at_index=row["branch_at_index"],
            mode=row["mode"],
            label=row["label"],
            created_at=row["created_at"],
        )

    def raw_attributes_bytes(self, rewind_id: UUID) -> bytes | None:
        """Return the verbatim ``raw_attributes`` JSON bytes for ``rewind_id``.

        Used by the fidelity tests: byte-for-byte comparison against the
        source OpenInference payload.
        """

        def _select(conn: sqlite3.Connection) -> bytes | None:
            row = conn.execute(
                "SELECT raw_attributes FROM spans WHERE rewind_id = ?",
                (str(rewind_id),),
            ).fetchone()
            return row["raw_attributes"].encode("utf-8") if row else None

        return self._execute(_select)


__all__ = ["SCHEMA_VERSION", "TraceStore"]
