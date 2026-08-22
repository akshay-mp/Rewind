"""``python -m agent_timetravel`` entrypoint — delegates to the Click CLI."""

from __future__ import annotations

from agent_timetravel.cli import cli

if __name__ == "__main__":
    cli()
