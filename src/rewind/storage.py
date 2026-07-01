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
from typing import TYPE_CHECKING, TypeVar
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

#: Default schema version of the on-disk DB. Bump + migrate on breaking changes.
#:
#: History:
#: * v1 - traces / branches / spans (Phases 0-3).
#: * v2 - adds ``checkpoints`` table for Phase 4 state rollback.
#: * v3 - adds ``eval_runs`` + ``eval_scenarios`` tables for Phase 5.5 batch eval.
SCHEMA_VERSION = 3

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


__all__ = ["SCHEMA_VERSION", "TraceStore"]
