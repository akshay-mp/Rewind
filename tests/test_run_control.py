"""Tests for Phase 1.2 / 1.3 / 1.4 — server-owned run-control and new decisions.

Covers:

* **Phase 1.2** — ``RunControlIntent`` round-trips through the
  ``interactive_sessions`` row; the GET/PATCH run-control endpoints
  persist and return intent; the migration adds the ``run_control`` column
  to DBs created under schema v4.
* **Phase 1.3** — ``DecisionKind.RUN_UNTIL_BREAKPOINT`` arms the
  ``run_until_breakpoint`` flag and the channel auto-approves subsequent
  steps until a breakpoint fires. ``pause_after_current`` surfaces the next
  step instead of auto-advancing.
* **Phase 1.4** — ``DecisionKind.REJECT`` returns a structured reject dict
  from the tool interceptor without invoking the live tool.

The HTTP-driven SSE transport cannot be driven through TestClient (see the
note in ``test_stepping_api.py``), so channel mechanics are tested directly.
"""

from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent_timetravel.enums import SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.stepping import (
    Decision,
    DecisionKind,
    InteractiveSession,
    RunControlBreakpoint,
    RunControlIntent,
    Step,
    StepKind,
    decide_with_validation,
)
from agent_timetravel.stepping_api import (
    SSEApprovalChannel,
    mount_stepping,
)
from agent_timetravel.storage import SCHEMA_VERSION, TraceStore

_TRACE_ID = "abcd1234abcd1234abcd1234abcd1234"


# --- helpers ----------------------------------------------------------------


def _llm_span(span_id: str, messages: list[dict[str, str]]) -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name="qwen3:32b",
        messages_hash=hash_payload(messages),
        raw_attributes={
            "gen_ai.request.model": "qwen3:32b",
            "gen_ai.response": {
                "choices": [{"message": {"role": "assistant", "content": "hi"}}]
            },
        },
    )


@pytest.fixture
def store(tmp_path: Path) -> TraceStore:
    s = TraceStore(str(tmp_path / "run_control.db"))
    msgs = [{"role": "user", "content": "hello"}]
    spans = [_llm_span("a" * 16, msgs)]
    s.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for sp in spans:
        s.insert_span(sp)
    return s


@pytest.fixture
def app(store: TraceStore) -> FastAPI:
    a = FastAPI()
    a.state.store = store
    mount_stepping(a)
    return a


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture(autouse=True)
def _isolate_registry() -> Any:
    """Snapshot + restore the live-session registry per test."""
    from agent_timetravel import stepping_api

    saved_live = dict(stepping_api._SESSIONS._live)
    stepping_api._SESSIONS._live.clear()
    yield
    stepping_api._SESSIONS._live.clear()
    stepping_api._SESSIONS._live.update(saved_live)


# ===========================================================================
# Phase 1.2 — RunControlIntent persistence
# ===========================================================================


class TestRunControlIntentSerialization:
    """``RunControlIntent.to_dict`` / ``from_dict`` round-trip."""

    def test_defaults_are_all_false(self) -> None:
        rc = RunControlIntent()
        assert rc.pause_after_current is False
        assert rc.run_until_breakpoint is False

    def test_round_trip(self) -> None:
        rc = RunControlIntent(pause_after_current=True, run_until_breakpoint=False)
        restored = RunControlIntent.from_dict(rc.to_dict())
        assert restored.pause_after_current is True
        assert restored.run_until_breakpoint is False

    def test_breakpoint_rules_round_trip(self) -> None:
        rc = RunControlIntent(
            run_until_breakpoint=True,
            breakpoints=(
                RunControlBreakpoint(
                    type="message_contains",
                    value="continue",
                    label="message contains: continue",
                ),
            ),
        )
        restored = RunControlIntent.from_dict(rc.to_dict())
        assert restored.breakpoints == rc.breakpoints

    def test_from_dict_tolerates_garbage(self) -> None:
        # Old rows (pre-Phase 1.2) or corrupted JSON must not crash.
        assert RunControlIntent.from_dict(None) == RunControlIntent()
        assert RunControlIntent.from_dict("not a dict") == RunControlIntent()
        assert RunControlIntent.from_dict({}) == RunControlIntent()
        assert RunControlIntent.from_dict({"unknown_key": True}) == RunControlIntent()


class TestRunControlStorageRoundTrip:
    """The ``run_control`` column persists and restores intent."""

    def test_default_intent_round_trips(self, store: TraceStore) -> None:
        sid = "11111111-2222-3333-4444-555555555555"
        store.upsert_interactive_session(
            InteractiveSession(
                session_id=sid,
                trace_id=_TRACE_ID,
                branch_id="b" * 36,
                runner_ref="r",
                created_at="2026-08-03T00:00:00+00:00",
                updated_at="2026-08-03T00:00:00+00:00",
            )
        )
        row = store.get_interactive_session(sid)
        assert row is not None
        assert row.run_control == RunControlIntent()

    def test_custom_intent_round_trips(self, store: TraceStore) -> None:
        sid = "22222222-3333-4444-5555-666666666666"
        store.upsert_interactive_session(
            InteractiveSession(
                session_id=sid,
                trace_id=_TRACE_ID,
                branch_id="b" * 36,
                runner_ref="r",
                status="running",
                created_at="2026-08-03T00:00:00+00:00",
                updated_at="2026-08-03T00:00:00+00:00",
                run_control=RunControlIntent(run_until_breakpoint=True),
            )
        )
        row = store.get_interactive_session(sid)
        assert row is not None
        assert row.run_control.run_until_breakpoint is True
        assert row.run_control.pause_after_current is False

    def test_status_update_preserves_intent(self, store: TraceStore) -> None:
        sid = "33333333-3333-4444-5555-777777777777"
        store.upsert_interactive_session(
            InteractiveSession(
                session_id=sid,
                trace_id=_TRACE_ID,
                branch_id="b" * 36,
                runner_ref="r",
                created_at="2026-08-03T00:00:00+00:00",
                updated_at="2026-08-03T00:00:00+00:00",
                run_control=RunControlIntent(pause_after_current=True),
            )
        )
        # Simulate the runner marking status — run_control must survive.
        from agent_timetravel.stepping_api import _set_status

        _set_status(store, sid, "paused")
        row = store.get_interactive_session(sid)
        assert row is not None
        assert row.status == "paused"
        assert row.run_control.pause_after_current is True


class TestRunControlMigration:
    """A DB created under schema v4 gets the ``run_control`` column added."""

    def test_migration_adds_column_to_v4_db(self, tmp_path: Path) -> None:
        db = tmp_path / "v4.db"
        # Build a schema-v4 DB by hand (no run_control column).
        conn = sqlite3.connect(db)
        conn.executescript(
            """
            CREATE TABLE traces (
                trace_id TEXT PRIMARY KEY, root_branch_id TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE branches (
                branch_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                parent_branch_id TEXT, branch_at_index INTEGER,
                mode TEXT NOT NULL DEFAULT 'frozen', label TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL
            );
            CREATE TABLE spans (
                timetravel_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL, parent_span_id TEXT, branch_id TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL, kind TEXT NOT NULL, start_time TEXT NOT NULL,
                end_time TEXT NOT NULL, status TEXT NOT NULL, status_message TEXT,
                model_name TEXT, prompt_tokens INTEGER, completion_tokens INTEGER,
                total_tokens INTEGER, messages_hash TEXT, tools_hash TEXT,
                raw_attributes TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE checkpoints (
                checkpoint_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                branch_id TEXT NOT NULL, name TEXT NOT NULL, cursor_index INTEGER,
                label TEXT, payload TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL,
                UNIQUE(branch_id, name)
            );
            CREATE TABLE eval_runs (
                run_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL, suite_name TEXT NOT NULL,
                status TEXT NOT NULL, started_at TEXT NOT NULL, completed_at TEXT,
                summary TEXT, config TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE eval_scenarios (
                run_id TEXT NOT NULL REFERENCES eval_runs(run_id) ON DELETE CASCADE,
                seq INTEGER NOT NULL, name TEXT NOT NULL, trace_id TEXT NOT NULL,
                label TEXT, candidate_mode TEXT NOT NULL, verdict TEXT NOT NULL,
                detail TEXT, PRIMARY KEY (run_id, seq)
            );
            CREATE TABLE interactive_sessions (
                session_id TEXT PRIMARY KEY, trace_id TEXT NOT NULL,
                branch_id TEXT NOT NULL, runner_ref TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'running', error_message TEXT,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            PRAGMA user_version = 4;
            """
        )
        # Seed a v4-style row (no run_control).
        conn.execute(
            "INSERT INTO interactive_sessions (session_id, trace_id, branch_id, "
            "runner_ref, status, created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            ("44444444-4444-4444-4444-444444444444", _TRACE_ID, "b" * 36,
             "r", "running", "2026-08-03T00:00:00+00:00", "2026-08-03T00:00:00+00:00"),
        )
        conn.commit()
        conn.close()

        # Reopen via TraceStore — the migration should add run_control.
        store = TraceStore(str(db))
        assert SCHEMA_VERSION >= 5
        row = store.get_interactive_session("44444444-4444-4444-4444-444444444444")
        assert row is not None
        # Pre-existing row gets the default intent ('{}' → all False).
        assert row.run_control == RunControlIntent()


# ===========================================================================
# Phase 1.2 — GET/PATCH run-control endpoints
# ===========================================================================


class TestRunControlEndpoints:
    """``GET``/``PATCH /api/v1/sessions/{id}/run-control``."""

    def _seed_session(self, store: TraceStore) -> str:
        sid = "55555555-5555-5555-5555-555555555555"
        store.upsert_interactive_session(
            InteractiveSession(
                session_id=sid,
                trace_id=_TRACE_ID,
                branch_id="b" * 36,
                runner_ref="r",
                created_at="2026-08-03T00:00:00+00:00",
                updated_at="2026-08-03T00:00:00+00:00",
            )
        )
        return sid

    def test_get_returns_default_intent(self, client: TestClient, store: TraceStore) -> None:
        sid = self._seed_session(store)
        resp = client.get(f"/api/v1/sessions/{sid}/run-control")
        assert resp.status_code == 200
        body = resp.json()
        assert body["pause_after_current"] is False
        assert body["run_until_breakpoint"] is False

    def test_patch_updates_and_persists(self, client: TestClient, store: TraceStore) -> None:
        sid = self._seed_session(store)
        resp = client.patch(
            f"/api/v1/sessions/{sid}/run-control",
            json={"pause_after_current": True, "run_until_breakpoint": False},
        )
        assert resp.status_code == 200
        assert resp.json()["pause_after_current"] is True
        # Persisted — a fresh GET sees it.
        get_resp = client.get(f"/api/v1/sessions/{sid}/run-control")
        assert get_resp.json()["pause_after_current"] is True

    def test_patch_round_trips_server_breakpoints(
        self,
        client: TestClient,
        store: TraceStore,
    ) -> None:
        sid = self._seed_session(store)
        rule = {
            "type": "token_limit",
            "value": "512",
            "label": "max tokens at least: 512",
            "enabled": True,
            "id": "local-only-id",
        }
        resp = client.patch(
            f"/api/v1/sessions/{sid}/run-control",
            json={"run_until_breakpoint": True, "breakpoints": [rule]},
        )
        assert resp.status_code == 200
        assert resp.json()["breakpoints"][0]["value"] == "512"
        assert "id" not in resp.json()["breakpoints"][0]
        persisted = client.get(f"/api/v1/sessions/{sid}/run-control").json()
        assert persisted["breakpoints"] == resp.json()["breakpoints"]

    def test_get_missing_session_404(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sessions/66666666-6666-6666-6666-666666666666/run-control")
        assert resp.status_code == 404

    def test_patch_missing_session_404(self, client: TestClient) -> None:
        resp = client.patch(
            "/api/v1/sessions/77777777-7777-7777-7777-777777777777/run-control",
            json={"pause_after_current": True},
        )
        assert resp.status_code == 404

    def test_bad_uuid_400(self, client: TestClient) -> None:
        resp = client.get("/api/v1/sessions/not-a-uuid/run-control")
        assert resp.status_code == 400


# ===========================================================================
# Phase 1.3 — RUN_UNTIL_BREAKPOINT + channel auto-advance
# ===========================================================================


class TestRunUntilBreakpoint:
    """``DecisionKind.RUN_UNTIL_BREAKPOINT`` arms auto-advance."""

    def test_validation_accepts_run_until_breakpoint(self) -> None:
        d = decide_with_validation(Decision(kind=DecisionKind.RUN_UNTIL_BREAKPOINT))
        assert d.kind is DecisionKind.RUN_UNTIL_BREAKPOINT

    def test_validation_rejects_overrides_on_run_until_breakpoint(self) -> None:
        with pytest.raises(ValueError, match="must not carry overrides"):
            decide_with_validation(
                Decision(
                    kind=DecisionKind.RUN_UNTIL_BREAKPOINT,
                    messages=[{"role": "user", "content": "x"}],
                )
            )

    def test_channel_auto_approves_when_run_until_breakpoint(self) -> None:
        """With run_until_breakpoint armed, submit short-circuits to APPROVE."""
        ch = SSEApprovalChannel()
        ch.set_run_control(RunControlIntent(run_until_breakpoint=True))
        step = Step(kind=StepKind.LLM, payload={"model": "m"}, cursor=0)
        # _maybe_auto_decide returns APPROVE without touching the browser queue.
        decision = ch._maybe_auto_decide(step)
        assert decision is not None
        assert decision.kind is DecisionKind.APPROVE

    def test_channel_surfaces_breakpoint_step(self) -> None:
        """A step carrying a ``breakpoint`` marker is NOT auto-approved."""
        ch = SSEApprovalChannel()
        ch.set_run_control(RunControlIntent(run_until_breakpoint=True))
        step = Step(
            kind=StepKind.LLM,
            payload={"model": "m", "breakpoint": "stop-here"},
            cursor=1,
        )
        decision = ch._maybe_auto_decide(step)
        # Breakpoint fires → surfaced to browser (None) and flag disarmed.
        assert decision is None
        assert ch.run_control.run_until_breakpoint is False

    def test_channel_surfaces_server_configured_message_breakpoint(self) -> None:
        ch = SSEApprovalChannel()
        ch.set_run_control(
            RunControlIntent(
                run_until_breakpoint=True,
                breakpoints=(RunControlBreakpoint(type="message_contains", value="stop here"),),
            )
        )
        step = Step(
            kind=StepKind.LLM,
            payload={"messages": [{"role": "user", "content": "Please stop here"}]},
            cursor=2,
        )
        assert ch._maybe_auto_decide(step) is None
        assert ch.run_control.run_until_breakpoint is False

    def test_channel_auto_approves_nonmatching_server_rule(self) -> None:
        ch = SSEApprovalChannel()
        ch.set_run_control(
            RunControlIntent(
                run_until_breakpoint=True,
                breakpoints=(RunControlBreakpoint(type="model_name", value="gemma"),),
            )
        )
        step = Step(kind=StepKind.LLM, payload={"model": "qwen"}, cursor=2)
        decision = ch._maybe_auto_decide(step)
        assert decision is not None
        assert decision.kind is DecisionKind.APPROVE

    @pytest.mark.parametrize(
        ("rule", "payload"),
        [
            (RunControlBreakpoint(type="tool_name", value="delete"), {"name": "delete_file"}),
            (RunControlBreakpoint(type="model_name", value="gemma"), {"model": "unsloth/gemma"}),
            (
                RunControlBreakpoint(type="token_limit", value="1024"),
                {"params": {"max_tokens": 2048}},
            ),
        ],
    )
    def test_server_breakpoint_rule_types_match(
        self,
        rule: RunControlBreakpoint,
        payload: dict[str, Any],
    ) -> None:
        ch = SSEApprovalChannel()
        ch.set_run_control(RunControlIntent(run_until_breakpoint=True, breakpoints=(rule,)))
        assert ch._maybe_auto_decide(Step(kind=StepKind.TOOL, payload=payload, cursor=3)) is None

    def test_pause_after_current_surfaces_then_clears(self) -> None:
        """``pause_after_current`` surfaces the step and consumes the flag."""
        ch = SSEApprovalChannel()
        ch.set_run_control(RunControlIntent(pause_after_current=True))
        step = Step(kind=StepKind.LLM, payload={"model": "m"}, cursor=2)
        decision = ch._maybe_auto_decide(step)
        assert decision is None  # surfaced to browser
        # One-shot consumed.
        assert ch.run_control.pause_after_current is False

    @pytest.mark.asyncio
    async def test_sse_pause_reason_and_consumed_intent_are_observable(self) -> None:
        persisted: list[RunControlIntent] = []
        ch = SSEApprovalChannel(persist_run_control=persisted.append)
        ch.set_run_control(RunControlIntent(pause_after_current=True))
        step = Step(kind=StepKind.LLM, payload={"model": "m"}, cursor=4)

        pending = asyncio.create_task(ch.submit(step))
        event = await ch.next_event()
        assert event["type"] == "paused"
        assert event["pause_reason"] == "pause_after_current"
        assert persisted == [RunControlIntent()]

        ch.decide(Decision(kind=DecisionKind.APPROVE))
        assert (await pending).kind is DecisionKind.APPROVE

    @pytest.mark.asyncio
    async def test_server_breakpoint_pause_carries_reason(self) -> None:
        ch = SSEApprovalChannel()
        ch.set_run_control(
            RunControlIntent(
                run_until_breakpoint=True,
                breakpoints=(RunControlBreakpoint(type="model_name", value="gemma"),),
            )
        )
        step = Step(kind=StepKind.LLM, payload={"model": "gemma"}, cursor=5)

        pending = asyncio.create_task(ch.submit(step))
        event = await ch.next_event()
        assert event["pause_reason"] == "breakpoint"
        ch.decide(Decision(kind=DecisionKind.APPROVE))
        assert (await pending).kind is DecisionKind.APPROVE


# ===========================================================================
# Phase 1.4 — REJECT decision kind
# ===========================================================================


class TestRejectDecision:
    """``DecisionKind.REJECT`` validation (interceptor handling is tested in
    ``test_tool_intercept.py`` alongside MOCK/SKIP)."""

    def test_reject_is_valid_decision_kind(self) -> None:
        assert DecisionKind("reject") is DecisionKind.REJECT

    def test_validation_accepts_bare_reject(self) -> None:
        d = decide_with_validation(Decision(kind=DecisionKind.REJECT))
        assert d.kind is DecisionKind.REJECT

    def test_validation_accepts_reject_with_reason(self) -> None:
        d = decide_with_validation(
            Decision(kind=DecisionKind.REJECT, reason="too dangerous")
        )
        assert d.reason == "too dangerous"

    def test_validation_rejects_call_overrides(self) -> None:
        with pytest.raises(ValueError, match="must not carry overrides"):
            decide_with_validation(
                Decision(
                    kind=DecisionKind.REJECT,
                    args=[1, 2],
                )
            )
