"""``python -m rewind`` entrypoint — delegates to the Click CLI."""

from __future__ import annotations

from rewind.cli import cli

if __name__ == "__main__":
    cli()
