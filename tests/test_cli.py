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
