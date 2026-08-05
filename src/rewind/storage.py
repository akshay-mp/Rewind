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
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import TYPE_CHECKING, Any, TypeVar
from uuid import UUID

from rewind.enums import SpanKind, SpanStatus
from rewind.models import Branch, Checkpoint, Span, Trace

# Phase 5.5 eval-runtime types are imported lazily inside the eval-storage
# helpers (``upsert_eval_run`` etc.) to avoid a cyclic import:
# ``rewind.evaluate`` lazily imports ``rewind.storage.TraceStore`` from inside
# its default-session factory, so a top-level import here would close the
# loop. Pylint's cyclic-import check is satisfied by the lazy form.
if TYPE_CHECKING:
    from rewind.evaluate import (
        EvalSuiteResult,
        EvalSuiteResultSummary,
        ScenarioResult,
    )
    from rewind.stepping import InteractiveSession

#: Default schema version of the on-disk DB. Bump + migrate on breaking changes.
#:
#: History:
#: * v1 - traces / branches / spans (Phases 0-3).
#: * v2 - adds ``checkpoints`` table for Phase 4 state rollback.
#: * v3 - adds ``eval_runs`` + ``eval_scenarios`` tables for Phase 5.5 batch eval.
#: * v4 - adds ``interactive_sessions`` table for Phase 9 step-through debug.
#: * v5 - adds ``run_control`` column to ``interactive_sessions`` (Phase 1.2).
#: * v6 - adds ``prompt_versions``, ``prompt_version_results``,
#:   ``assertion_profiles``, ``step_reviews`` tables (Phase 2.1 durable records).
#: * v7 - adds ``regression_cases`` + ``regression_runs`` tables (Phase 4).
#: * v8 - adds ``run_environment`` table for reproducibility manifests (Phase 5.3).
#: * v9 - expands prompt experiment records with reproducibility and review
#:   snapshots (Phase 2 completion).
SCHEMA_VERSION = 9

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

#: Idempotent additive migration: Phase 4 ``checkpoints`` table.
#:
#: Kept separate from ``_SCHEMA_SQL`` because fresh DBs hit ``executescript``
#: which does not run migration blocks. The init code runs this for every DB
#: (existing or fresh); it is ``IF NOT EXISTS`` so it's safe to re-run.
_CHECKPOINT_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS checkpoints (
    checkpoint_id  TEXT PRIMARY KEY,
    trace_id       TEXT NOT NULL REFERENCES traces(trace_id),
    branch_id      TEXT NOT NULL,
    name           TEXT NOT NULL,
    cursor_index   INTEGER NOT NULL,
    label          TEXT NOT NULL DEFAULT '',
    payload        TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL,
    UNIQUE(branch_id, name)
);
CREATE INDEX IF NOT EXISTS idx_checkpoints_branch_name
    ON checkpoints(branch_id, name);
CREATE INDEX IF NOT EXISTS idx_checkpoints_trace ON checkpoints(trace_id);
"""

#: Idempotent additive migration: Phase 5.5 ``eval_runs`` + ``eval_scenarios``.
#:
#: An eval run is the persisted result of executing one :class:`EvalSuite`.
#: It is keyed by ``run_id`` (a UUID minted at submit time) and stores the
#: input YAML verbatim for reproducibility (audit/QA requirement). Each row
#: in ``eval_scenarios`` is one scenario's outcome set + rollup, ordered to
#: preserve the suite's original scenario order (``seq`` column).
_EVAL_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS eval_runs (
    run_id          TEXT PRIMARY KEY,
    suite_name      TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL,
    overall_verdict TEXT NOT NULL,
    suite_yaml      TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_eval_runs_started ON eval_runs(started_at);

CREATE TABLE IF NOT EXISTS eval_scenarios (
    run_id          TEXT NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    scenario_name   TEXT NOT NULL,
    seed_trace_id   TEXT NOT NULL,
    branch_id       TEXT NOT NULL,
    verdict         TEXT NOT NULL,
    outcomes        TEXT NOT NULL DEFAULT '[]',
    rollup          TEXT NOT NULL DEFAULT '{}',
    latency         TEXT NOT NULL DEFAULT '{}',
    error_message   TEXT,
    PRIMARY KEY (run_id, seq)
);
CREATE INDEX IF NOT EXISTS idx_eval_scenarios_run ON eval_scenarios(run_id);
"""

#: Idempotent additive migration: Phase 9 ``interactive_sessions`` table.
#:
#: An interactive session is the server-side bookkeeping for a step-through
#: debug run: which trace/branch is being stepped, which runner produced it,
#: and its lifecycle status. The actual paused-step traffic flows over the
#: SSE stream + POST /decide (not via this table) — the row exists so the
#: UI can list/resume sessions and survive an SSE reconnect. Rows are
#: intentionally small (no span payloads); the spans themselves live in the
#: existing ``spans`` table under ``session.branch_id``.
_INTERACTIVE_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS interactive_sessions (
    session_id      TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL REFERENCES traces(trace_id),
    branch_id       TEXT NOT NULL,
    runner_ref      TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running',
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL,
    run_control     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_interactive_sessions_trace
    ON interactive_sessions(trace_id);
CREATE INDEX IF NOT EXISTS idx_interactive_sessions_status
    ON interactive_sessions(status);
"""

#: Phase 1.2 migration — adds the ``run_control`` column to existing
#: ``interactive_sessions`` rows. SQLite lacks ``ADD COLUMN IF NOT EXISTS``,
#: so this is guarded by a ``PRAGMA table_info`` check in
#: :meth:`TraceStore._init_schema` rather than executed blindly (which would
#: raise on already-migrated DBs). Fresh DBs get the column via the DDL
#: above (the ``CREATE TABLE`` already includes it — see the migration note).
_RUN_CONTROL_MIGRATION_SQL = """
ALTER TABLE interactive_sessions
    ADD COLUMN run_control TEXT NOT NULL DEFAULT '{}';
"""

#: Phase 2.1 — durable experiment records. Prompt variants, their results,
#: reusable assertion profiles, and per-step developer reviews are persisted
#: so a page refresh or a colleague's machine can hydrate the full experiment
#: history. Previously these lived only in browser localStorage.
_PROMPT_VERSION_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS prompt_versions (
    version_id      TEXT PRIMARY KEY,
    trace_id        TEXT NOT NULL REFERENCES traces(trace_id),
    cursor_index    INTEGER NOT NULL,
    base_messages   TEXT NOT NULL DEFAULT '[]',
    messages        TEXT NOT NULL DEFAULT '[]',
    base_model      TEXT NOT NULL DEFAULT '',
    model           TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'running',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prompt_versions_trace
    ON prompt_versions(trace_id, cursor_index);

CREATE TABLE IF NOT EXISTS prompt_version_results (
    version_id      TEXT PRIMARY KEY REFERENCES prompt_versions(version_id) ON DELETE CASCADE,
    result          TEXT,
    usage           TEXT NOT NULL DEFAULT '{}',
    latency_ms      INTEGER,
    completed_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS assertion_profiles (
    profile_id      TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    required_text   TEXT NOT NULL DEFAULT '[]',
    forbidden_text  TEXT NOT NULL DEFAULT '[]',
    require_json    INTEGER NOT NULL DEFAULT 0,
    require_citations INTEGER NOT NULL DEFAULT 0,
    max_tokens      INTEGER,
    max_cost_usd    REAL,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_assertion_profiles_name
    ON assertion_profiles(name);

CREATE TABLE IF NOT EXISTS step_reviews (
    trace_id        TEXT NOT NULL REFERENCES traces(trace_id),
    cursor_index    INTEGER NOT NULL,
    review_note     TEXT,
    review_verdict  TEXT,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (trace_id, cursor_index)
);
"""

#: Phase 2 completion migration. SQLite has no portable ``ADD COLUMN IF NOT
#: EXISTS``, so ``_init_schema`` applies these additive columns only when they
#: are absent. This keeps Phase 2.1 databases upgradeable in place.
_PROMPT_EXPERIMENT_MIGRATION_COLUMNS = {
    "prompt_versions": {
        "branch_id": "TEXT NOT NULL DEFAULT ''",
        "parent_version_id": "TEXT",
        "parameters": "TEXT NOT NULL DEFAULT '{}'",
        "author_note": "TEXT NOT NULL DEFAULT ''",
        "updated_at": "TEXT NOT NULL DEFAULT ''",
        "assertions": "TEXT NOT NULL DEFAULT '{}'",
        "evaluator_names": "TEXT NOT NULL DEFAULT '[]'",
    },
    "prompt_version_results": {
        "reasoning": "TEXT",
        "pricing": "TEXT NOT NULL DEFAULT '{}'",
        "assertion_result": "TEXT NOT NULL DEFAULT '{}'",
        "review_verdict": "TEXT",
        "review_note": "TEXT",
        "evaluator_results": "TEXT NOT NULL DEFAULT '{}'",
    },
    "step_reviews": {
        "assertions": "TEXT NOT NULL DEFAULT '{}'",
        "assertion_result": "TEXT NOT NULL DEFAULT '{}'",
    },
}

#: Phase 4 — executable regression cases + runs. A regression case freezes a
#: golden trace + expected assertions so a later ``run_frozen_verification``
#: can re-execute the agent deterministically and flag drift.
_REGRESSION_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS regression_cases (
    case_id         TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    seed_trace_id   TEXT NOT NULL REFERENCES traces(trace_id),
    expected        TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regression_cases_name
    ON regression_cases(name);

CREATE TABLE IF NOT EXISTS regression_runs (
    run_id          TEXT PRIMARY KEY,
    case_id         TEXT NOT NULL REFERENCES regression_cases(case_id) ON DELETE CASCADE,
    passed          INTEGER NOT NULL,
    detail          TEXT,
    branch_id       TEXT,
    started_at      TEXT NOT NULL,
    finished_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_regression_runs_case
    ON regression_runs(case_id);
"""

#: Phase 5.3 — reproducibility manifests. One row per captured environment,
#: keyed by content hash so identical environments dedupe.
_REPRODUCIBILITY_MIGRATION_SQL = """
CREATE TABLE IF NOT EXISTS run_environment (
    env_hash        TEXT PRIMARY KEY,
    manifest        TEXT NOT NULL,
    captured_at     TEXT NOT NULL
);
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
        Additive migrations (Phase 4 ``checkpoints``) are also idempotent and
        run for both fresh and existing DBs.
        """
        conn = self._connect()
        try:
            conn.executescript(_SCHEMA_SQL)
            conn.executescript(_CHECKPOINT_MIGRATION_SQL)
            conn.executescript(_EVAL_MIGRATION_SQL)
            conn.executescript(_INTERACTIVE_MIGRATION_SQL)
            conn.executescript(_PROMPT_VERSION_MIGRATION_SQL)
            conn.executescript(_REGRESSION_MIGRATION_SQL)
            conn.executescript(_REPRODUCIBILITY_MIGRATION_SQL)
            # Phase 1.2: ``ALTER TABLE ... ADD COLUMN`` has no IF NOT EXISTS in
            # SQLite, so guard on ``PRAGMA table_info``. Fresh DBs already get
            # the column from the DDL above; this branch only patches DBs
            # created under schema v4.
            cols = {
                str(row[1])
                for row in conn.execute("PRAGMA table_info(interactive_sessions)")
            }
            if "run_control" not in cols:
                conn.executescript(_RUN_CONTROL_MIGRATION_SQL)
            for table, columns in _PROMPT_EXPERIMENT_MIGRATION_COLUMNS.items():
                existing = {
                    str(row[1])
                    for row in conn.execute(f"PRAGMA table_info({table})")
                }
                for column, definition in columns.items():
                    if column not in existing:
                        conn.execute(
                            f"ALTER TABLE {table} ADD COLUMN {column} {definition}"
                        )
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

    # -- checkpoints (Phase 4) -----------------------------------------------
    def upsert_checkpoint(self, checkpoint: Checkpoint) -> None:
        """Insert or replace a checkpoint keyed by ``(branch_id, name)``.

        Re-capturing the same name on the same branch overwrites — the
        assumption is the agent will only reach a named checkpoint once per
        branch, and a re-capture signals an updated snapshot.
        """

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO checkpoints (
                    checkpoint_id, trace_id, branch_id, name, cursor_index,
                    label, payload, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(branch_id, name) DO UPDATE SET
                    cursor_index = excluded.cursor_index,
                    label        = excluded.label,
                    payload      = excluded.payload,
                    created_at   = excluded.created_at
                """,
                (
                    str(checkpoint.checkpoint_id),
                    checkpoint.trace_id,
                    str(checkpoint.branch_id),
                    checkpoint.name,
                    checkpoint.cursor_index,
                    checkpoint.label,
                    json.dumps(checkpoint.payload, sort_keys=True),
                    checkpoint.created_at,
                ),
            )

        self._execute(_upsert)

    def get_checkpoint(self, branch_id: UUID, name: str) -> Checkpoint | None:
        """Return a checkpoint by ``(branch_id, name)`` or ``None``."""

        def _select(conn: sqlite3.Connection) -> Checkpoint | None:
            row = conn.execute(
                """
                SELECT * FROM checkpoints
                WHERE branch_id = ? AND name = ?
                """,
                (str(branch_id), name),
            ).fetchone()
            return self._checkpoint_from_row(row) if row else None

        return self._execute(_select)

    def list_checkpoints(self, branch_id: UUID) -> list[Checkpoint]:
        """List all checkpoints for a branch in cursor order."""

        def _select(conn: sqlite3.Connection) -> list[Checkpoint]:
            rows = conn.execute(
                """
                SELECT * FROM checkpoints
                WHERE branch_id = ?
                ORDER BY cursor_index, created_at
                """,
                (str(branch_id),),
            ).fetchall()
            return [self._checkpoint_from_row(r) for r in rows]

        return self._execute(_select)

    # -- row mappers (checkpoint) --------------------------------------------
    @staticmethod
    def _checkpoint_from_row(row: sqlite3.Row) -> Checkpoint:
        """Reconstruct a :class:`Checkpoint` from a DB row."""
        return Checkpoint(
            checkpoint_id=UUID(row["checkpoint_id"]),
            trace_id=row["trace_id"],
            branch_id=UUID(row["branch_id"]),
            name=row["name"],
            cursor_index=row["cursor_index"],
            label=row["label"],
            payload=json.loads(row["payload"]),
            created_at=row["created_at"],
        )

    # -- large-span paging (Phase 4) -----------------------------------------
    def get_spans_paginated(
        self,
        trace_id: str,
        branch_id: UUID | None = None,
        *,
        limit: int = 1000,
        offset: int = 0,
    ) -> tuple[list[Span], int]:
        """Return one page of spans + the total row count for this branch.

        Phase 4 guarantee: a trace with 100k+ spans must load its timeline
        without OOM. The read API uses this method so the receiver path (full
        eager load) is untouched while the UI streams pages.

        ``limit`` is clamped to [1, 10_000] to bound query memory; callers
        needing the full timeline should iterate pages. Total count ignores
        ``limit``/``offset`` and respects the same branch union filter as
        :meth:`get_spans`.
        """
        bid = "" if branch_id is None else str(branch_id)
        clamped = max(1, min(limit, 10_000))
        if offset < 0:
            raise ValueError(f"offset must be >= 0, got {offset}")

        def _select(conn: sqlite3.Connection) -> tuple[list[Span], int]:
            total = conn.execute(
                """
                SELECT COUNT(*) AS n FROM spans
                WHERE trace_id = ? AND (branch_id = '' OR branch_id = ?)
                """,
                (trace_id, bid),
            ).fetchone()["n"]
            rows = conn.execute(
                """
                SELECT * FROM spans
                WHERE trace_id = ? AND (branch_id = '' OR branch_id = ?)
                ORDER BY start_time, rowid
                LIMIT ? OFFSET ?
                """,
                (trace_id, bid, clamped, offset),
            ).fetchall()
            return [self._span_from_row(r) for r in rows], total

        return self._execute(_select)

    def iter_spans(
        self,
        trace_id: str,
        branch_id: UUID | None = None,
        *,
        chunk_size: int = 1000,
    ) -> Iterator[Span]:
        """Stream spans one at a time without materializing the whole trace.

        Phase 4 guarantee: 100k-span traces must load without OOM. This
        generator is the streaming counterpart of :meth:`get_spans` — it
        emits spans lazily, paging under the hood. Each page issues a fresh
        :meth:`_connect` (so callers can pause/drop the generator without
        leaking a connection).

        Args:
            trace_id: Trace id to stream.
            branch_id: Optional branch id. ``None`` (the default) returns
                the union of root spans (``branch_id == ''``) plus the
                given branch — same union as :meth:`get_spans`.
            chunk_size: Page size. Clamped to [1, 10_000] internally.

        Yields:
            Span: One span at a time, in ``(start_time, rowid)`` order.
        """
        if chunk_size < 1:
            raise ValueError(f"chunk_size must be >= 1, got {chunk_size}")

        bid = "" if branch_id is None else str(branch_id)
        clamped = max(1, min(chunk_size, 10_000))
        offset = 0
        while True:
            page, total = self.get_spans_paginated(
                trace_id,
                branch_id=UUID(bid) if bid else None,
                limit=clamped,
                offset=offset,
            )
            if not page:
                return
            yield from page
            offset += len(page)
            if offset >= total:
                return

    # -- eval runs (Phase 5.5) -----------------------------------------------
    def upsert_eval_run(self, result: EvalSuiteResult, suite_yaml: str = "") -> None:
        """Persist an eval run, replacing any prior run with the same id.

        Uses a single transaction: deletes existing rows for ``run_id`` first
        so re-running the same suite (re-using the run_id for idempotent entry)
        is safe. All scenario columns are serialized via the public
        :func:`scenario_result_to_dict` helpers so the on-disk format matches
        the wire format exactly.
        """
        # pylint: disable=import-outside-toplevel
        from rewind.evaluate import scenario_result_to_dict

        # pylint: enable=import-outside-toplevel

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO eval_runs (
                    run_id, suite_name, started_at, finished_at,
                    overall_verdict, suite_yaml
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    suite_name      = excluded.suite_name,
                    started_at      = excluded.started_at,
                    finished_at     = excluded.finished_at,
                    overall_verdict = excluded.overall_verdict,
                    suite_yaml      = excluded.suite_yaml
                """,
                (
                    str(result.run_id),
                    result.suite_name,
                    result.started_at,
                    result.finished_at,
                    result.overall_verdict.value,
                    suite_yaml,
                ),
            )
            # Replace scenarios atomically.
            conn.execute(
                "DELETE FROM eval_scenarios WHERE run_id = ?",
                (str(result.run_id),),
            )
            for seq, scenario in enumerate(result.scenarios):
                scen_dict = scenario_result_to_dict(scenario)
                conn.execute(
                    """
                    INSERT INTO eval_scenarios (
                        run_id, seq, scenario_name, seed_trace_id, branch_id,
                        verdict, outcomes, rollup, latency, error_message
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(result.run_id),
                        seq,
                        scenario.name,
                        scenario.seed_trace_id,
                        scen_dict["branch_id"] or "",
                        scenario.verdict.value,
                        json.dumps(scen_dict["outcomes"], sort_keys=True),
                        json.dumps(scen_dict["rollup"], sort_keys=True),
                        json.dumps(scen_dict["latency"], sort_keys=True),
                        scenario.error_message,
                    ),
                )

        self._execute(_upsert)

    def get_eval_run(self, run_id: str) -> EvalSuiteResult | None:
        """Load an eval run with all its scenario rows, or ``None`` if absent.

        Reconstruction reverses :meth:`upsert_eval_run`: scenarios are ordered
        by ``seq`` so the caller gets the same list (and order) that was stored.
        """
        run_id_str = str(run_id)

        def _select(conn: sqlite3.Connection) -> EvalSuiteResult | None:
            # pylint: disable=import-outside-toplevel
            from rewind.enums import EvalVerdict
            from rewind.evaluate import EvalSuiteResult as _EvalSuiteResult

            # pylint: enable=import-outside-toplevel
            run_row = conn.execute(
                "SELECT * FROM eval_runs WHERE run_id = ?",
                (run_id_str,),
            ).fetchone()
            if run_row is None:
                return None
            scenario_rows = conn.execute(
                """
                SELECT * FROM eval_scenarios
                WHERE run_id = ?
                ORDER BY seq
                """,
                (run_id_str,),
            ).fetchall()
            scenarios: list[ScenarioResult] = [
                self._eval_scenario_from_row(r) for r in scenario_rows
            ]
            return _EvalSuiteResult(
                run_id=UUID(run_row["run_id"]),
                suite_name=run_row["suite_name"],
                started_at=run_row["started_at"],
                finished_at=run_row["finished_at"],
                overall_verdict=EvalVerdict(run_row["overall_verdict"]),
                scenarios=scenarios,
            )

        return self._execute(_select)

    def list_eval_runs(
        self, limit: int = 50, offset: int = 0
    ) -> tuple[list[EvalSuiteResultSummary], int]:
        """Return a page of eval runs + total count, newest first.

        Each summary excludes the heavy per-scenario JSON; the UI list page
        only needs ``suite_name``, ``overall_verdict``, and timestamps.
        """

        def _select(
            conn: sqlite3.Connection,
        ) -> tuple[list[EvalSuiteResultSummary], int]:
            # pylint: disable=import-outside-toplevel
            from rewind.enums import EvalVerdict
            from rewind.evaluate import EvalSuiteResultSummary as _Summary

            # pylint: enable=import-outside-toplevel
            total = conn.execute("SELECT COUNT(*) AS n FROM eval_runs").fetchone()["n"]
            rows = conn.execute(
                """
                SELECT run_id, suite_name, started_at, finished_at, overall_verdict
                FROM eval_runs
                ORDER BY started_at DESC, rowid DESC
                LIMIT ? OFFSET ?
                """,
                (limit, offset),
            ).fetchall()
            summaries = [
                _Summary(
                    run_id=r["run_id"],
                    suite_name=r["suite_name"],
                    started_at=r["started_at"],
                    finished_at=r["finished_at"],
                    overall_verdict=EvalVerdict(r["overall_verdict"]),
                )
                for r in rows
            ]
            return summaries, total

        return self._execute(_select)

    @staticmethod
    def _eval_scenario_from_row(row: sqlite3.Row) -> ScenarioResult:
        """Reconstruct a :class:`ScenarioResult` from an ``eval_scenarios`` row."""
        # pylint: disable=import-outside-toplevel
        from rewind.evaluate import scenario_result_from_dict

        # pylint: enable=import-outside-toplevel

        scen_dict = {
            "name": row["scenario_name"],
            "seed_trace_id": row["seed_trace_id"],
            "branch_id": row["branch_id"] or None,
            "verdict": row["verdict"],
            "outcomes": json.loads(row["outcomes"]),
            "rollup": json.loads(row["rollup"]),
            "latency": json.loads(row["latency"]),
            "error_message": row["error_message"],
        }
        return scenario_result_from_dict(scen_dict)

    def delete_eval_run(self, run_id: str) -> bool:
        """Delete an eval run + its scenarios. Returns ``True`` if a row was deleted.

        The ``eval_scenarios`` foreign key has ``ON DELETE CASCADE`` so a single
        ``DELETE FROM eval_runs`` cascades all scenario rows in the same txn.
        """
        run_id_str = str(run_id)

        def _delete(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM eval_runs WHERE run_id = ?",
                (run_id_str,),
            )
            return cursor.rowcount > 0

        return self._execute(_delete)

    # -- interactive sessions (Phase 9) --------------------------------------
    def upsert_interactive_session(self, session: InteractiveSession) -> None:
        """Insert or update an interactive stepping session row.

        ``status`` transitions (running → paused → done/errored) all flow
        through here so the UI's list view reflects current runner state.
        The ``run_control`` intent is persisted alongside so a page refresh
        or SSE reconnect doesn't lose "pause after current" / "run until
        breakpoint" (Phase 1.2).
        """
        # pylint: disable=import-outside-toplevel
        from rewind.stepping import RunControlIntent
        # pylint: enable=import-outside-toplevel

        run_control_json = json.dumps(
            session.run_control.to_dict()
            if isinstance(session.run_control, RunControlIntent)
            else RunControlIntent.from_dict(session.run_control).to_dict(),
            sort_keys=True,
        )

        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO interactive_sessions
                    (session_id, trace_id, branch_id, runner_ref, status,
                     error_message, created_at, updated_at, run_control)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    status        = excluded.status,
                    error_message = excluded.error_message,
                    updated_at    = excluded.updated_at,
                    run_control   = excluded.run_control
                """,
                (
                    session.session_id,
                    session.trace_id,
                    session.branch_id,
                    session.runner_ref,
                    session.status,
                    session.error_message,
                    session.created_at,
                    session.updated_at,
                    run_control_json,
                ),
            )

        self._execute(_upsert)

    def get_interactive_session(self, session_id: str) -> InteractiveSession | None:
        """Return one interactive session row, or ``None`` if not found."""
        # pylint: disable=import-outside-toplevel
        from rewind.stepping import InteractiveSession, RunControlIntent
        # pylint: enable=import-outside-toplevel

        def _select(conn: sqlite3.Connection) -> InteractiveSession | None:
            row = conn.execute(
                """
                SELECT session_id, trace_id, branch_id, runner_ref,
                       status, error_message, created_at, updated_at,
                       run_control
                FROM interactive_sessions
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()
            if row is None:
                return None
            return InteractiveSession(
                session_id=row["session_id"],
                trace_id=row["trace_id"],
                branch_id=row["branch_id"],
                runner_ref=row["runner_ref"],
                status=row["status"],
                error_message=row["error_message"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
                run_control=RunControlIntent.from_dict(
                    json.loads(row["run_control"] or "{}")
                ),
            )

        return self._execute(_select)

    def list_interactive_sessions(
        self, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[InteractiveSession], int]:
        """List interactive sessions newest-first, with a total count."""
        # pylint: disable=import-outside-toplevel
        from rewind.stepping import InteractiveSession, RunControlIntent
        # pylint: enable=import-outside-toplevel

        clamped_limit = max(1, min(limit, 500))

        def _select(conn: sqlite3.Connection) -> tuple[list[InteractiveSession], int]:
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM interactive_sessions"
            ).fetchone()
            total = int(total_row["n"]) if total_row is not None else 0
            rows = conn.execute(
                """
                SELECT session_id, trace_id, branch_id, runner_ref,
                       status, error_message, created_at, updated_at,
                       run_control
                FROM interactive_sessions
                ORDER BY created_at DESC
                LIMIT ? OFFSET ?
                """,
                (clamped_limit, max(0, offset)),
            ).fetchall()
            items = [
                InteractiveSession(
                    session_id=r["session_id"],
                    trace_id=r["trace_id"],
                    branch_id=r["branch_id"],
                    runner_ref=r["runner_ref"],
                    status=r["status"],
                    error_message=r["error_message"],
                    created_at=r["created_at"],
                    updated_at=r["updated_at"],
                    run_control=RunControlIntent.from_dict(
                        json.loads(r["run_control"] or "{}")
                    ),
                )
                for r in rows
            ]
            return items, total

        return self._execute(_select)

    def delete_interactive_session(self, session_id: str) -> bool:
        """Delete an interactive session row. Returns ``True`` if a row was deleted.

        Does NOT cascade into ``spans`` — the captured spans under
        ``branch_id`` are independent timeline data the UI may still render.
        """
        def _delete(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM interactive_sessions WHERE session_id = ?",
                (session_id,),
            )
            return cursor.rowcount > 0

        return self._execute(_delete)

    # -- prompt versions (Phase 2.1 durable records) ------------------------
    def upsert_prompt_version(self, row: dict[str, Any]) -> None:
        """Insert or update a prompt-version experiment row.

        ``row`` is a plain dict so the storage layer doesn't take a hard
        dependency on a Pydantic view model. Keys mirror the
        ``prompt_versions`` columns; ``base_messages``/``messages`` are
        JSON-encoded.
        """
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO prompt_versions (
                    version_id, trace_id, cursor_index, base_messages, messages,
                    base_model, model, status, created_at, branch_id,
                    parent_version_id, parameters, author_note, updated_at,
                    assertions, evaluator_names
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id) DO NOTHING
                """,
                (
                    row["version_id"],
                    row["trace_id"],
                    row["cursor_index"],
                    json.dumps(row.get("base_messages", []), sort_keys=True),
                    json.dumps(row.get("messages", []), sort_keys=True),
                    row.get("base_model", ""),
                    row.get("model", ""),
                    row.get("status", "running"),
                    row.get("created_at", ""),
                    row.get("branch_id", ""),
                    row.get("parent_version_id"),
                    json.dumps(row.get("parameters", {}), sort_keys=True),
                    row.get("author_note", ""),
                    row.get("updated_at", row.get("created_at", "")),
                    json.dumps(row.get("assertions", {}), sort_keys=True),
                    json.dumps(row.get("evaluator_names", []), sort_keys=True),
                ),
            )

        self._execute(_upsert)

    def set_prompt_version_result(self, row: dict[str, Any]) -> None:
        """Persist the immutable result once, then merge annotations.

        The response, usage, pricing, and latency are write-once. Later UI
        interactions may add assertion/review/evaluator annotations without
        being able to replace the recorded model result.
        """
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO prompt_version_results
                    (version_id, result, usage, latency_ms, completed_at,
                     reasoning, pricing, assertion_result, review_verdict,
                     review_note, evaluator_results)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(version_id) DO NOTHING
                """,
                (
                    row["version_id"],
                    row.get("result"),
                    json.dumps(row.get("usage", {}), sort_keys=True),
                    row.get("latency_ms"),
                    row.get("completed_at", ""),
                    row.get("reasoning"),
                    json.dumps(row.get("pricing", {}), sort_keys=True),
                    json.dumps(row.get("assertion_result", {}), sort_keys=True),
                    row.get("review_verdict"),
                    row.get("review_note"),
                    json.dumps(row.get("evaluator_results", {}), sort_keys=True),
                ),
            )
            existing = conn.execute(
                """
                SELECT assertion_result, review_verdict, review_note,
                       evaluator_results
                FROM prompt_version_results WHERE version_id = ?
                """,
                (row["version_id"],),
            ).fetchone()
            if existing is not None:
                previous_evaluators = json.loads(existing["evaluator_results"] or "{}")
                incoming_evaluators = row.get("evaluator_results") or {}
                merged_evaluators = {
                    **(previous_evaluators if isinstance(previous_evaluators, dict) else {}),
                    **(incoming_evaluators if isinstance(incoming_evaluators, dict) else {}),
                }
                assertion_result = row.get("assertion_result") or json.loads(
                    existing["assertion_result"] or "{}"
                )
                review_verdict = row.get("review_verdict")
                review_note = row.get("review_note")
                conn.execute(
                    """
                    UPDATE prompt_version_results
                    SET assertion_result = ?, review_verdict = ?, review_note = ?,
                        evaluator_results = ?
                    WHERE version_id = ?
                    """,
                    (
                        json.dumps(assertion_result, sort_keys=True),
                        review_verdict if review_verdict is not None else existing["review_verdict"],
                        review_note if review_note is not None else existing["review_note"],
                        json.dumps(merged_evaluators, sort_keys=True),
                        row["version_id"],
                    ),
                )
            conn.execute(
                "UPDATE prompt_versions SET status = 'completed' WHERE version_id = ?",
                (row["version_id"],),
            )

        self._execute(_upsert)

    def list_prompt_versions(
        self, trace_id: str, cursor_index: int | None = None
    ) -> list[dict[str, Any]]:
        """List prompt versions for a step, joined with their results."""
        def _select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            query = """
                SELECT pv.*, pvr.result, pvr.usage AS result_usage,
                       pvr.latency_ms, pvr.completed_at, pvr.reasoning,
                       pvr.pricing, pvr.assertion_result, pvr.review_verdict,
                       pvr.review_note, pvr.evaluator_results
                FROM prompt_versions pv
                LEFT JOIN prompt_version_results pvr
                    ON pv.version_id = pvr.version_id
                WHERE pv.trace_id = ?
            """
            params: tuple[Any, ...] = (trace_id,)
            if cursor_index is not None:
                query += " AND pv.cursor_index = ?"
                params += (cursor_index,)
            query += " ORDER BY pv.created_at"
            rows = conn.execute(query, params).fetchall()
            return [_prompt_version_row_to_dict(r) for r in rows]

        return self._execute(_select)

    def get_prompt_version(self, version_id: str) -> dict[str, Any] | None:
        """Return one prompt version (with result) by id, or ``None``."""
        def _select(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                """
                SELECT pv.*, pvr.result, pvr.usage AS result_usage,
                       pvr.latency_ms, pvr.completed_at, pvr.reasoning,
                       pvr.pricing, pvr.assertion_result, pvr.review_verdict,
                       pvr.review_note, pvr.evaluator_results
                FROM prompt_versions pv
                LEFT JOIN prompt_version_results pvr
                    ON pv.version_id = pvr.version_id
                WHERE pv.version_id = ?
                """,
                (version_id,),
            ).fetchone()
            return _prompt_version_row_to_dict(row) if row else None

        return self._execute(_select)

    def delete_prompt_version(self, version_id: str) -> bool:
        """Delete a prompt version (cascades to its result). Returns True if deleted."""
        def _delete(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM prompt_versions WHERE version_id = ?",
                (version_id,),
            )
            return cursor.rowcount > 0

        return self._execute(_delete)

    # -- assertion profiles (Phase 2.1) -------------------------------------
    def upsert_assertion_profile(self, row: dict[str, Any]) -> None:
        """Insert or update a reusable assertion profile."""
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO assertion_profiles (
                    profile_id, name, required_text, forbidden_text,
                    require_json, require_citations, max_tokens, max_cost_usd,
                    created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(profile_id) DO UPDATE SET
                    name               = excluded.name,
                    required_text      = excluded.required_text,
                    forbidden_text     = excluded.forbidden_text,
                    require_json       = excluded.require_json,
                    require_citations  = excluded.require_citations,
                    max_tokens         = excluded.max_tokens,
                    max_cost_usd       = excluded.max_cost_usd
                """,
                (
                    row["profile_id"],
                    row["name"],
                    json.dumps(row.get("required_text", []), sort_keys=True),
                    json.dumps(row.get("forbidden_text", []), sort_keys=True),
                    1 if row.get("require_json") else 0,
                    1 if row.get("require_citations") else 0,
                    row.get("max_tokens"),
                    row.get("max_cost_usd"),
                    row.get("created_at", ""),
                ),
            )

        self._execute(_upsert)

    def list_assertion_profiles(self) -> list[dict[str, Any]]:
        """List all assertion profiles, newest-first."""
        def _select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM assertion_profiles ORDER BY created_at DESC"
            ).fetchall()
            return [_assertion_profile_row_to_dict(r) for r in rows]

        return self._execute(_select)

    # -- step reviews (Phase 2.1) -------------------------------------------
    def upsert_step_review(self, row: dict[str, Any]) -> None:
        """Insert or update a developer review for a step (note + verdict)."""
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO step_reviews
                    (trace_id, cursor_index, review_note, review_verdict, updated_at,
                     assertions, assertion_result)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(trace_id, cursor_index) DO UPDATE SET
                    review_note    = excluded.review_note,
                    review_verdict = excluded.review_verdict,
                    updated_at     = excluded.updated_at,
                    assertions     = excluded.assertions,
                    assertion_result = excluded.assertion_result
                """,
                (
                    row["trace_id"],
                    row["cursor_index"],
                    row.get("review_note"),
                    row.get("review_verdict"),
                    row.get("updated_at", ""),
                    json.dumps(row.get("assertions", {}), sort_keys=True),
                    json.dumps(row.get("assertion_result", {}), sort_keys=True),
                ),
            )

        self._execute(_upsert)

    def list_step_reviews(self, trace_id: str) -> list[dict[str, Any]]:
        """List all reviews for a trace, ordered by cursor."""
        def _select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT trace_id, cursor_index, review_note, review_verdict, updated_at,
                       assertions, assertion_result
                FROM step_reviews
                WHERE trace_id = ?
                ORDER BY cursor_index
                """,
                (trace_id,),
            ).fetchall()
            return [
                {
                    "trace_id": r["trace_id"],
                    "cursor_index": r["cursor_index"],
                    "review_note": r["review_note"],
                    "review_verdict": r["review_verdict"],
                    "updated_at": r["updated_at"],
                    "assertions": json.loads(r["assertions"] or "{}"),
                    "assertion_result": json.loads(r["assertion_result"] or "{}"),
                }
                for r in rows
            ]

        return self._execute(_select)

    # -- regression cases + runs (Phase 4) ----------------------------------
    def upsert_regression_case(self, row: dict[str, Any]) -> None:
        """Insert or update a regression case (golden trace + expected checks)."""
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO regression_cases
                    (case_id, name, seed_trace_id, expected, created_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(case_id) DO UPDATE SET
                    name          = excluded.name,
                    seed_trace_id = excluded.seed_trace_id,
                    expected      = excluded.expected
                """,
                (
                    row["case_id"],
                    row["name"],
                    row["seed_trace_id"],
                    json.dumps(row.get("expected", {}), sort_keys=True),
                    row.get("created_at", ""),
                ),
            )

        self._execute(_upsert)

    def get_regression_case(self, case_id: str) -> dict[str, Any] | None:
        """Return one regression case by id, or ``None``."""
        def _select(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM regression_cases WHERE case_id = ?",
                (case_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "case_id": row["case_id"],
                "name": row["name"],
                "seed_trace_id": row["seed_trace_id"],
                "expected": json.loads(row["expected"] or "{}"),
                "created_at": row["created_at"],
            }

        return self._execute(_select)

    def list_regression_cases(self) -> list[dict[str, Any]]:
        """List all regression cases, newest-first."""
        def _select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                "SELECT * FROM regression_cases ORDER BY created_at DESC"
            ).fetchall()
            return [
                {
                    "case_id": r["case_id"],
                    "name": r["name"],
                    "seed_trace_id": r["seed_trace_id"],
                    "expected": json.loads(r["expected"] or "{}"),
                    "created_at": r["created_at"],
                }
                for r in rows
            ]

        return self._execute(_select)

    def delete_regression_case(self, case_id: str) -> bool:
        """Delete a regression case (cascades to runs). Returns True if deleted."""
        def _delete(conn: sqlite3.Connection) -> bool:
            cursor = conn.execute(
                "DELETE FROM regression_cases WHERE case_id = ?",
                (case_id,),
            )
            return cursor.rowcount > 0

        return self._execute(_delete)

    def insert_regression_run(self, row: dict[str, Any]) -> None:
        """Persist a regression run result."""
        def _insert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO regression_runs
                    (run_id, case_id, passed, detail, branch_id,
                     started_at, finished_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    row["run_id"],
                    row["case_id"],
                    1 if row.get("passed") else 0,
                    row.get("detail"),
                    row.get("branch_id"),
                    row.get("started_at", ""),
                    row.get("finished_at", ""),
                ),
            )

        self._execute(_insert)

    def list_regression_runs(self, case_id: str) -> list[dict[str, Any]]:
        """List regression runs for a case, newest-first."""
        def _select(conn: sqlite3.Connection) -> list[dict[str, Any]]:
            rows = conn.execute(
                """
                SELECT * FROM regression_runs
                WHERE case_id = ?
                ORDER BY started_at DESC
                """,
                (case_id,),
            ).fetchall()
            return [
                {
                    "run_id": r["run_id"],
                    "case_id": r["case_id"],
                    "passed": bool(r["passed"]),
                    "detail": r["detail"],
                    "branch_id": r["branch_id"],
                    "started_at": r["started_at"],
                    "finished_at": r["finished_at"],
                }
                for r in rows
            ]

        return self._execute(_select)

    # -- run environment / reproducibility (Phase 5.3) ----------------------
    def upsert_run_environment(self, manifest: dict[str, Any]) -> None:
        """Persist a reproducibility manifest (upsert by content hash)."""
        def _upsert(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO run_environment (env_hash, manifest, captured_at)
                VALUES (?, ?, ?)
                ON CONFLICT(env_hash) DO UPDATE SET
                    captured_at = excluded.captured_at
                """,
                (
                    manifest.get("content_hash", ""),
                    json.dumps(manifest, sort_keys=True),
                    manifest.get("captured_at", ""),
                ),
            )

        self._execute(_upsert)

    def get_run_environment(self, env_hash: str) -> dict[str, Any] | None:
        """Return one manifest by hash, or ``None``."""
        def _select(conn: sqlite3.Connection) -> dict[str, Any] | None:
            row = conn.execute(
                "SELECT * FROM run_environment WHERE env_hash = ?",
                (env_hash,),
            ).fetchone()
            if row is None:
                return None
            loaded: Any = json.loads(row["manifest"] or "{}")
            return loaded if isinstance(loaded, dict) else {}

        return self._execute(_select)


# -- row mappers (Phase 2.1) ------------------------------------------------


def _prompt_version_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Reconstruct a prompt-version dict (with optional result) from a DB row."""
    return {
        "version_id": row["version_id"],
        "trace_id": row["trace_id"],
        "cursor_index": row["cursor_index"],
        "base_messages": json.loads(row["base_messages"] or "[]"),
        "messages": json.loads(row["messages"] or "[]"),
        "base_model": row["base_model"],
        "model": row["model"],
        "branch_id": row["branch_id"],
        "parent_version_id": row["parent_version_id"],
        "parameters": json.loads(row["parameters"] or "{}"),
        "author_note": row["author_note"],
        "updated_at": row["updated_at"],
        "assertions": json.loads(row["assertions"] or "{}"),
        "evaluator_names": json.loads(row["evaluator_names"] or "[]"),
        "status": row["status"],
        "created_at": row["created_at"],
        "result": row["result"],
        "usage": json.loads(row["result_usage"] or "{}"),
        "latency_ms": row["latency_ms"],
        "completed_at": row["completed_at"],
        "reasoning": row["reasoning"],
        "pricing": json.loads(row["pricing"] or "{}"),
        "assertion_result": json.loads(row["assertion_result"] or "{}"),
        "review_verdict": row["review_verdict"],
        "review_note": row["review_note"],
        "evaluator_results": json.loads(row["evaluator_results"] or "{}"),
    }


def _assertion_profile_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    """Reconstruct an assertion-profile dict from a DB row."""
    return {
        "profile_id": row["profile_id"],
        "name": row["name"],
        "required_text": json.loads(row["required_text"] or "[]"),
        "forbidden_text": json.loads(row["forbidden_text"] or "[]"),
        "require_json": bool(row["require_json"]),
        "require_citations": bool(row["require_citations"]),
        "max_tokens": row["max_tokens"],
        "max_cost_usd": row["max_cost_usd"],
        "created_at": row["created_at"],
    }


__all__ = ["SCHEMA_VERSION", "TraceStore"]
