"""Top-level Rewind CLI.

Phase 0 exposes only ``rewind --version``. Phase 1 adds ``serve``; Phase 2
adds ``ui``; Phase 3 adds ``replay``; P5.5 adds ``eval``.
"""

from __future__ import annotations

from pathlib import Path

import click

from rewind import __version__

#: Default bind address for ``rewind serve``. Loopback only — this is a
#: local-only debug surface; binding to 0.0.0.0 requires an explicit flag and
#: is documented as riskier in the Phase 1 threat model.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 4318
_DEFAULT_DB = "rewind.db"


@click.group()
@click.version_option(version=__version__, prog_name="rewind")
def cli() -> None:
    """Rewind — time-travel debugging for AI agents."""


@cli.command()
def version() -> None:
    """Print the installed Rewind version."""
    click.echo(__version__)


@cli.command()
@click.option(
    "--host",
    default=_DEFAULT_HOST,
    show_default=True,
    help="Bind address. Default is loopback-only; pass 0.0.0.0 to expose.",
)
@click.option(
    "--port",
    "--otlp-port",
    "port",
    type=int,
    default=_DEFAULT_PORT,
    show_default=True,
    help="TCP port for the OTLP/HTTP receiver (4318 = OTel default).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file. One DB per workspace.",
)
def serve(host: str, port: int, db_path: Path) -> None:
    """Start the local OTLP/HTTP ingestion server.

    Listens on ``host:port`` and persists every received span into ``--db``.
    Use ``CTRL+C`` to stop. See ``docs/wiring/`` for how to point agents here.
    """
    # Imported lazily so ``rewind --version`` does not pay the FastAPI import
    # cost, and so the import-time graph stays shallow for testing.
    # pylint: disable=import-outside-toplevel
    import uvicorn

    from rewind.receiver import create_app
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))
    app = create_app(store)
    click.echo(
        f"rewind serve → http://{host}:{port}/v1/traces  "
        f"(db={db_path}, version={__version__})",
        err=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


#: Default port for ``rewind ui``. Distinct from the OTLP receiver port
#: (4318) so both can run side-by-side in dev or be split for production.
#: Mirrors Streamlit's 8501 by convention for "developer UI port".
_DEFAULT_UI_PORT = 8484


@cli.command()
@click.option(
    "--host",
    default=_DEFAULT_HOST,
    show_default=True,
    help="Bind address. Default is loopback-only; pass 0.0.0.0 to expose.",
)
@click.option(
    "--port",
    "port",
    type=int,
    default=_DEFAULT_UI_PORT,
    show_default=True,
    help="TCP port for the timeline UI (8484 default; separate from OTLP 4318).",
)
@click.option(
    "--otlp-port",
    "otlp_port",
    type=int,
    default=_DEFAULT_PORT,
    show_default=True,
    help="TCP port for the OTLP/HTTP receiver mounted on the same process.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read.",
)
def ui(host: str, port: int, otlp_port: int, db_path: Path) -> None:
    """Start the timeline UI.

    Runs a single FastAPI process that serves the read-only timeline API
    (``/api/v1/*``), the OTLP receiver (``/v1/traces``), and the built UI
    (``/ui``). If the UI build is absent, ``/ui`` returns a 404 with build
    instructions but the rest keeps running.

    ``--otlp-port`` is accepted for symmetry with ``serve`` but currently the
    OTLP receiver shares the UI's port (so the app is one origin). The flag's
    value is validated but only the UI port is bound when they differ; a
    future split-process mode will honour the divergence. For now, point
    agents at ``http://127.0.0.1:<ui-port>/v1/traces``.
    """
    if otlp_port != port:
        click.echo(
            "note: --otlp-port is accepted but the OTLP receiver currently shares "
            "the UI port. Point agents at http://127.0.0.1:" + str(port)
            + "/v1/traces.",
            err=True,
        )
    # pylint: disable=import-outside-toplevel
    import uvicorn

    from rewind.receiver import create_app
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))
    app = create_app(store)
    click.echo(
        f"rewind ui → http://{host}:{port}/ui  "
        f"(db={db_path}, version={__version__})",
        err=True,
    )
    click.echo(
        f"          OTLP receiver → http://{host}:{port}/v1/traces",
        err=True,
    )
    uvicorn.run(app, host=host, port=port, log_level="warning")


@cli.command()
@click.argument("trace_id")
@click.option(
    "--branch-at",
    "branch_at",
    type=int,
    default=None,
    help="Fork a new branch starting at this 0-based span index. "
    "Without it, replay continues from the start of the trace.",
)
@click.option(
    "--mode",
    type=click.Choice(["frozen", "branch", "full"]),
    default="frozen",
    show_default=True,
    help="frozen = serve only recorded fixtures (zero outbound); "
    "branch = serve recorded, then go live past the cursor; "
    "full = re-run everything live against the original seed.",
)
@click.option(
    "--label",
    default="",
    help="Human-readable label for the new branch (branch/full modes only).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read the trace from.",
)
def replay(
    trace_id: str,
    branch_at: int | None,
    mode: str,
    label: str,
    db_path: Path,
) -> None:
    """Inspect a recorded trace and print the replay plan.

    This is the **read-only** entry point: it loads the trace, opens a replay
    session (optionally forked), and prints the cursor + branch id + the
    equivalent frozen query. To actually drive replay through an agent loop,
    use the :func:`rewind.replay` context manager from Python — the CLI is
    for inspection and CI integration (`rewind replay <id>` exits non-zero
    if the trace cannot be loaded).

    Streaming / interactive driving lands with Phase 5 polish.
    """
    # pylint: disable=import-outside-toplevel
    from rewind.enums import ReplayMode
    from rewind.replay import ReplayError
    from rewind.replay import replay as replay_ctx
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    mode_enum = ReplayMode(mode)
    store = TraceStore(db_path=str(db_path))

    try:
        with replay_ctx(
            store,
            trace_id,
            branch_at=branch_at,
            mode=mode_enum,
            label=label or f"cli-{mode}",
        ) as session:
            click.echo(
                f"trace       {session.trace_id}\n"
                f"branch_id   {session.branch_id}\n"
                f"mode        {session.mode.value}\n"
                f"cursor      {session.cursor} / {len(session.recorded_spans())}\n"
                f"label       {session.label!r}\n"
                f"forked_at   {session.forked_at if session.forked_at is not None else '-'}"
            )
    except ReplayError as exc:
        click.echo(f"rewind: replay error: {exc}", err=True)
        raise click.exceptions.Exit(1) from exc


def main() -> None:
    """Entrypoint referenced by ``[project.scripts] rewind``."""
    cli()


if __name__ == "__main__":
    main()
