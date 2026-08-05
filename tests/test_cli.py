"""Tests for the CLI: ``python -m rewind --version`` runs (Phase 0)."""

from __future__ import annotations

import subprocess
import sys

from click.testing import CliRunner

from rewind import __version__
from rewind.cli import cli


def test_version_constant_matches_module() -> None:
    assert __version__ == "0.1.0"


def test_python_m_rewind_version() -> None:
    """Phase 0 exit criterion: ``python -m rewind --version`` runs."""
    proc = subprocess.run(
        [sys.executable, "-m", "rewind", "--version"],
        capture_output=True,
        text=True,
        check=True,
    )
    assert __version__ in (proc.stdout + proc.stderr)


def test_serve_is_registered_subcommand() -> None:
    """Phase 1: ``rewind serve`` is wired up and accepts --host/--port/--db."""
    runner = CliRunner()
    result = runner.invoke(cli, ["serve", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--host" in out
    assert "--port" in out
    assert "--otlp-port" in out  # alias
    assert "--db" in out


def test_serve_help_advertises_default_port() -> None:
    """The OTel-canonical 4318 should be the documented default."""
    runner = CliRunner()
    result = runner.invoke(cli, ["serve", "--help"])
    assert "4318" in result.output


def test_ui_is_registered_subcommand() -> None:
    """Phase 2: ``rewind ui`` is wired up and accepts --host/--port/--db."""
    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--help"])
    assert result.exit_code == 0
    out = result.output
    assert "--host" in out
    assert "--port" in out
    assert "--otlp-port" in out
    assert "--db" in out


def test_ui_help_advertises_default_port() -> None:
    """Phase 2 default UI port is 8484 (distinct from the OTLP 4318)."""
    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--help"])
    assert "8484" in result.output


def test_ui_default_host_is_loopback() -> None:
    """The UI should not bind to public interfaces by default."""
    runner = CliRunner()
    result = runner.invoke(cli, ["ui", "--help"])
    assert "127.0.0.1" in result.output


# --- Phase 4.3 — regression subcommands -----------------------------------


def test_regression_group_registered() -> None:
    """``rewind regression`` is a registered subcommand group."""
    runner = CliRunner()
    result = runner.invoke(cli, ["regression", "--help"])
    assert result.exit_code == 0
    assert "create" in result.output
    assert "run" in result.output
    assert "list" in result.output


def test_regression_create_and_run(tmp_path: object) -> None:
    """End-to-end: create a case from a seeded trace, then run it (exit 0)."""
    from pathlib import Path

    from rewind.enums import SpanKind, SpanStatus
    from rewind.models import Span, Trace
    from rewind.storage import TraceStore

    db = Path(str(tmp_path)) / "regression_cli.db"
    store = TraceStore(str(db))
    trace_id = "d" * 32
    span = Span(
        trace_id=trace_id,
        span_id="1" * 16,
        parent_span_id=None,
        name="agent",
        kind=SpanKind.AGENT,
        status=SpanStatus.UNSET,
        raw_attributes={},
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[span]))
    store.insert_span(span)

    runner = CliRunner()
    # Create the case.
    create_result = runner.invoke(
        cli,
        [
            "regression", "create",
            "--name", "cli-smoke",
            "--trace-id", trace_id,
            "--expect-span-count", "1",
            "--db", str(db),
        ],
    )
    assert create_result.exit_code == 0
    assert "created regression case" in create_result.output

    # Grab the case id from the store (the CLI echoes it).
    cases = store.list_regression_cases()
    assert len(cases) == 1
    case_id = cases[0]["case_id"]

    # Run it — should pass (exit 0).
    run_result = runner.invoke(
        cli, ["regression", "run", case_id, "--db", str(db)]
    )
    assert run_result.exit_code == 0, run_result.output
    assert "1 passed" in run_result.output


def test_regression_run_exits_1_on_failure(tmp_path: object) -> None:
    """A failing case causes ``rewind regression run`` to exit 1."""
    from pathlib import Path

    from rewind.enums import SpanKind, SpanStatus
    from rewind.models import Span, Trace
    from rewind.storage import TraceStore

    db = Path(str(tmp_path)) / "regression_fail.db"
    store = TraceStore(str(db))
    trace_id = "e" * 32
    span = Span(
        trace_id=trace_id,
        span_id="2" * 16,
        parent_span_id=None,
        name="agent",
        kind=SpanKind.AGENT,
        status=SpanStatus.UNSET,
        raw_attributes={},
    )
    store.upsert_trace(Trace(trace_id=trace_id, spans=[span]))
    store.insert_span(span)
    store.upsert_regression_case(
        {
            "case_id": "fail-case",
            "name": "will-fail",
            "seed_trace_id": trace_id,
            "expected": {"span_count": 99},  # mismatch → fail
        }
    )

    runner = CliRunner()
    result = runner.invoke(
        cli, ["regression", "run", "fail-case", "--db", str(db)]
    )
    assert result.exit_code == 1
    assert "1 failed" in result.output


def test_regression_list_empty(tmp_path: object) -> None:
    """``rewind regression list`` on an empty DB prints a placeholder."""
    from pathlib import Path

    runner = CliRunner()
    result = runner.invoke(
        cli, ["regression", "list", "--db", str(Path(str(tmp_path)) / "empty.db")]
    )
    assert result.exit_code == 0
    assert "no regression cases" in result.output
