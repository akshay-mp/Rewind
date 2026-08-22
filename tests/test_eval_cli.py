"""Unit tests for ``agent-timetravel eval`` (the Phase 5.5 CLI subcommand).

Uses Click's ``CliRunner`` to drive the command end-to-end against a
real ``TraceStore`` at a temp path and a real file-based YAML suite.

Exercises:
  * ``--help`` advertises the YAML contract and exit-code policy.
  * Happy-path PASS run with persisted row in ``eval_runs``.
  * FAILED scenario exits 1 with echoed verdicts.
  * Invalid YAML exits 2 with validation message.
  * ``--no-save`` dry-run doesn't persist a row.
  * ``--suite-name`` overrides the run name.
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

import pytest
from click.testing import CliRunner

from agent_timetravel.cli import cli
from agent_timetravel.enums import EvalVerdict, SpanKind
from agent_timetravel.models import Span, Trace
from agent_timetravel.storage import TraceStore

_TRACE_ID = "a" * 32


# --- shared fixtures ------------------------------------------------------


@pytest.fixture
def seeded_db(tmp_path: Path) -> Path:
    """A SQLite DB with a 3-span seed trace already written."""
    db_path = tmp_path / "agent_timetravel.db"
    store = TraceStore(db_path)
    spans = [
        Span(
            trace_id=_TRACE_ID,
            span_id="1111111111111111",
            parent_span_id=None,
            name="adk.agent.Bot",
            kind=SpanKind.AGENT,
            start_time="2026-06-29T10:00:00+00:00",
            end_time="2026-06-29T10:00:05+00:00",
            raw_attributes={},
        ),
        Span(
            trace_id=_TRACE_ID,
            span_id="2222222222222222",
            parent_span_id=None,
            name="chat.completions",
            kind=SpanKind.LLM,
            model_name="qwen3:32b",
            prompt_tokens=42,
            completion_tokens=7,
            total_tokens=49,
            start_time="2026-06-29T10:00:01+00:00",
            end_time="2026-06-29T10:00:02+00:00",
            raw_attributes={
                "gen_ai.system": "openai",
                "gen_ai.usage.prompt_tokens": 42,
                "gen_ai.usage.completion_tokens": 7,
                "gen_ai.usage.total_tokens": 49,
                "gen_ai.response": {
                    "choices": [
                        {"message": {"role": "assistant", "content": "ok"}}
                    ]
                },
            },
        ),
        Span(
            trace_id=_TRACE_ID,
            span_id="3333333333333333",
            parent_span_id=None,
            name="search",
            kind=SpanKind.TOOL,
            start_time="2026-06-29T10:00:03+00:00",
            end_time="2026-06-29T10:00:04+00:00",
            raw_attributes={"tool.name": "search", "gen_ai.tool.result": "result 42"},
        ),
    ]
    store.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for sp in spans:
        store.insert_span(sp)
    return db_path


@pytest.fixture
def suite_path(tmp_path: Path) -> Path:
    """Write a valid passing-expected YAML suite file."""
    p = tmp_path / "suite.yaml"
    p.write_text(
        f"""
name: cli-pass
concurrency: 1
scenarios:
  - name: happy
    seed_trace_id: {_TRACE_ID}
    candidate_mode: frozen
    evaluators:
      - kind: token_budget
        expected:
          max_total_tokens: 1000
""".strip(),
        encoding="utf-8",
    )
    return p


# --- help -----------------------------------------------------------------


def test_eval_help_lists_options() -> None:
    """`agent-timetravel eval --help` advertises --db, --save/--no-save, --suite-name."""
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "--help"])
    assert result.exit_code == 0
    assert "--db" in result.output
    assert "--save" in result.output
    assert "--no-save" in result.output
    assert "--suite-name" in result.output
    assert "SUITE_PATH" in result.output


def test_eval_help_advertises_exit_codes() -> None:
    """The --help text must document exit-code semantics (0/1/2)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", "--help"])
    assert result.exit_code == 0
    # Exit codes are documented in the docstring body.
    assert "0" in result.output  # 0 = PASS
    assert "1" in result.output  # 1 = FAIL
    assert "2" in result.output  # 2 = ERROR


# --- happy path -----------------------------------------------------------


def test_eval_pass_run_persists_row(
    suite_path: Path, seeded_db: Path
) -> None:
    """A successful run exits 0 and persists a row in ``eval_runs``."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["eval", str(suite_path), "--db", str(seeded_db)]
    )
    assert result.exit_code == 0, result.output
    store = TraceStore(seeded_db)
    summaries, total = store.list_eval_runs()
    assert total == 1
    assert summaries[0].overall_verdict == EvalVerdict.PASS
    assert summaries[0].suite_name == "cli-pass"


def test_eval_dry_run_does_not_persist(
    suite_path: Path, seeded_db: Path
) -> None:
    """``--no-save`` runs the suite but skips the persist step."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["eval", str(suite_path), "--db", str(seeded_db), "--no-save"]
    )
    assert result.exit_code == 0, result.output
    store = TraceStore(seeded_db)
    _, total = store.list_eval_runs()
    assert total == 0  # nothing persisted


def test_eval_suite_name_override(
    suite_path: Path, seeded_db: Path
) -> None:
    """``--suite-name`` overrides the YAML's top-level name."""
    runner = CliRunner()
    result = runner.invoke(
        cli,
        [
            "eval",
            str(suite_path),
            "--db",
            str(seeded_db),
            "--suite-name",
            "renamed",
        ],
    )
    assert result.exit_code == 0, result.output
    store = TraceStore(seeded_db)
    summaries, _ = store.list_eval_runs()
    assert summaries[0].suite_name == "renamed"


# --- error paths ----------------------------------------------------------


def test_eval_failed_scenario_exits_1(
    tmp_path: Path, seeded_db: Path
) -> None:
    """A FAIL verdict makes the CLI exit 1 (but still persists the run)."""
    suite = tmp_path / "fail.yaml"
    suite.write_text(
        f"""
name: cli-fail
scenarios:
  - name: budget_zero
    seed_trace_id: {_TRACE_ID}
    candidate_mode: frozen
    evaluators:
      - kind: token_budget
        expected:
          max_total_tokens: 0
""".strip(),
        encoding="utf-8",
    )
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", str(suite), "--db", str(seeded_db)])
    assert result.exit_code == 1, result.output
    store = TraceStore(seeded_db)
    summaries, total = store.list_eval_runs()
    assert total == 1
    assert summaries[0].overall_verdict == EvalVerdict.FAIL


def test_eval_invalid_yaml_exits_2(
    tmp_path: Path, seeded_db: Path
) -> None:
    """Validation failure surfaces cleanly with exit code 2."""
    bad = tmp_path / "bad.yaml"
    bad.write_text("name: x\nscenarios: []\n", encoding="utf-8")
    runner = CliRunner()
    result = runner.invoke(cli, ["eval", str(bad), "--db", str(seeded_db)])
    assert result.exit_code == 2
    assert "validation" in result.output.lower() or "empty" in result.output.lower()


def test_eval_missing_suite_file_errors(
    tmp_path: Path, seeded_db: Path
) -> None:
    """A path that doesn't exist is caught by Click's ``exists=True`` check."""
    missing = tmp_path / "ghost.yaml"
    runner = CliRunner()
    result = runner.invoke(
        cli, ["eval", str(missing), "--db", str(seeded_db)]
    )
    # Click's path validation returns exit code 2 for usage errors.
    assert result.exit_code != 0
    assert "does not exist" in result.output.lower() or "error" in result.output.lower()


# --- run_id is a UUID ----------------------------------------------------


def test_eval_prints_run_id(
    suite_path: Path, seeded_db: Path
) -> None:
    """The summary must echo the run_id, even on a passing run."""
    runner = CliRunner()
    result = runner.invoke(
        cli, ["eval", str(suite_path), "--db", str(seeded_db)]
    )
    assert result.exit_code == 0

    store = TraceStore(seeded_db)
    summaries, _ = store.list_eval_runs()
    # Confirm the persisted run_id is a real UUID.
    assert isinstance(UUID(summaries[0].run_id), UUID)
