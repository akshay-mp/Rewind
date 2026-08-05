"""Top-level Rewind CLI.

Phase 0 exposes only ``rewind --version``. Phase 1 adds ``serve``; Phase 2
adds ``ui``; Phase 3 adds ``replay``; Phase 4 adds ``checkpoint``; P5.5
adds ``eval``; Phase 7 adds ``enrich`` and ``render-template``.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

from rewind import __version__

if TYPE_CHECKING:
    from uuid import UUID

    from rewind.evaluate import EvalSuiteResult
    from rewind.models import Checkpoint
    from rewind.storage import TraceStore


#: Default bind address for ``rewind serve``. Loopback only — this is a
#: local-only debug surface; binding to 0.0.0.0 requires an explicit flag and
#: is documented as riskier in the Phase 1 threat model.
_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 4318
#: Default database location: ``~/.rewind/rewind.db``. Phase 8 packaging
#: decision — a single well-known path means the README quickstart works
#: from any CWD without ``--db`` flags. The directory is created on first
#: use by :func:`_ensure_default_db_path`.
_DEFAULT_DB = Path.home() / ".rewind" / "rewind.db"


def _ensure_default_db_path(db_path: Path) -> Path:
    """If ``db_path`` is under the default ``~/.rewind/``, create the dir.

    Phase 8 contract: ``pipx install`` → ``rewind serve`` must not fail just
    because ``~/.rewind/`` doesn't exist yet. Only auto-creates the default
    path (a user passing ``--db /tmp/foo.db`` is on their own — explicit).
    """
    if db_path == _DEFAULT_DB:
        db_path.parent.mkdir(parents=True, exist_ok=True)
    return db_path


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

    db_path = _ensure_default_db_path(db_path)
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


@cli.command(name="eval", context_settings={"help_option_names": ["-h", "--help"]})
@click.argument(
    "suite_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read seed traces from and persist the run into.",
)
@click.option(
    "--save/--no-save",
    "persist",
    default=True,
    show_default=True,
    help="Persist the run into eval_runs; --no-save runs the suite in dry-run mode.",
)
@click.option(
    "--suite-name",
    default=None,
    help="Override the suite name (otherwise read from YAML's top-level 'name').",
)
def eval_cmd(
    suite_path: Path,
    db_path: Path,
    persist: bool,
    suite_name: str | None,
) -> None:
    """Run an eval suite defined as YAML and print the per-scenario verdicts.

    \b
    SUITE_PATH is a YAML file matching the docs/phases/phase-5.5.md contract:
    a top-level ``name`` + ``scenarios`` list. The suite runs through the
    async orchestrator (asyncio.gather + bounded semaphore) so scenarios
    execute in parallel up to ``concurrency``.

    \b
    Exit codes:
      0  overall verdict PASS (all scenarios PASS or SKIP)
      1  overall verdict FAIL (at least one scenario FAIL)
      2  overall verdict ERROR (anything raised inside the orchestrator)

    \b
    Examples:
      rewind eval tests/fixtures/suite.yaml --db rewind.db
      rewind eval suite.yaml --no-save           # dry-run, prints verdicts only
      rewind eval suite.yaml --suite-name dev    # override name
    """
    # pylint: disable=import-outside-toplevel
    import asyncio

    from rewind.eval_api import parse_suite_from_yaml
    from rewind.evaluate import EvalSuite, SuiteValidationError, evaluate
    from rewind.storage import TraceStore

    # pylint: enable=import-outside-toplevel

    suite_yaml = suite_path.read_text(encoding="utf-8")
    try:
        suite = parse_suite_from_yaml(suite_yaml)
    except SuiteValidationError as exc:
        click.echo(f"rewind eval: suite validation failed: {exc}", err=True)
        raise click.exceptions.Exit(2) from exc
    if suite_name is not None:
        suite = EvalSuite(
            name=suite_name,
            scenarios=suite.scenarios,
            concurrency=suite.concurrency,
            scenario_timeout_s=suite.scenario_timeout_s,
            judge=suite.judge,
        )

    store = TraceStore(db_path=str(db_path))
    try:
        result = asyncio.run(evaluate(suite, store=store))
    except SuiteValidationError as exc:
        click.echo(f"rewind eval: suite validation failed: {exc}", err=True)
        raise click.exceptions.Exit(2) from exc

    if persist:
        store.upsert_eval_run(result, suite_yaml=suite_yaml)

    _print_eval_summary(result)
    if result.overall_verdict.value == "pass":
        return
    if result.overall_verdict.value == "fail":
        raise click.exceptions.Exit(1)
    raise click.exceptions.Exit(2)


def _print_eval_summary(result: EvalSuiteResult) -> None:
    """Render the per-scenario verdicts as a compact table.

    Uses :mod:`rich.table` for column alignment when available; falls back
    to plain ``click.echo`` otherwise (e.g. when tests stub stdout).
    """
    # pylint: disable=import-outside-toplevel
    try:
        from rich.console import Console
        from rich.table import Table
    except ImportError:  # pragma: no cover - rich is a hard dep but be safe
        Console = None  # type: ignore[assignment, misc]
        Table = None  # type: ignore[assignment, misc]

    overall = result.overall_verdict.value.upper()
    suite_name_disp = result.suite_name
    run_id = result.run_id

    if Console is None or Table is None:  # pragma: no cover
        click.echo(f"suite       {suite_name_disp}")
        click.echo(f"run_id      {run_id}")
        click.echo(f"overall     {overall}")
        for scen in result.scenarios:
            click.echo(
                f"  {scen.verdict.value.upper():5s}  {scen.name}"
                + (f"  ({scen.error_message})" if scen.error_message else "")
            )
        return

    console = Console()
    table = Table(title=f"Eval suite: {suite_name_disp}", show_lines=False)
    table.add_column("Scenario", overflow="fold")
    table.add_column("Verdict", justify="right")
    table.add_column("Branch")
    table.add_column("Tokens")
    table.add_column("Detail")
    verdict_color = {"PASS": "green", "FAIL": "red", "SKIP": "yellow", "ERROR": "red"}
    for scen in result.scenarios:
        v = scen.verdict.value.upper()
        detail = ""
        if scen.error_message:
            detail = scen.error_message
        elif scen.outcomes:
            detail = scen.outcomes[0].detail
        # Cap detail to 60 chars to keep the table narrow.
        if len(detail) > 60:
            detail = detail[:57] + "..."
        branch = str(scen.branch_id)[:8] if scen.branch_id else "-"
        tokens = (
            str(scen.rollup.total_tokens) if scen.rollup.total_tokens else "-"
        )
        table.add_row(
            scen.name,
            f"[{verdict_color.get(v, 'white')}]{v}[/{verdict_color.get(v, 'white')}]",
            branch,
            tokens,
            detail,
        )
    console.print(table)
    console.print(f"overall: [{verdict_color.get(overall, 'white')}]{overall}")
    console.print(f"run_id:  {run_id}")


def main() -> None:
    """Entrypoint referenced by ``[project.scripts] rewind``."""
    cli()


@cli.group()
def checkpoint() -> None:
    """Inspect state snapshots captured by :func:`rewind.checkpoint`.

    Phase 4 added named state checkpoints to the replay engine: an agent
    that mutates the world can call ``rewind.checkpoint(name, payload)``
    inside a re-run, and FETCH the same state back on subsequent FROZEN
    runs without re-running the side effect.

    This group is read-only: it lets you list and dump snapshots for a
    given trace/branch. Captures happen via the agent calling
    :func:`rewind.checkpoint` inside an active replay session.
    """


@checkpoint.command("list")
@click.argument("trace_id")
@click.option(
    "--branch",
    "branch_id",
    default=None,
    help="Constrain to one branch id. Omit to scan every branch in the trace.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read.",
)
def checkpoint_list(
    trace_id: str, branch_id: str | None, db_path: Path
) -> None:
    """List checkpoints captured for ``trace_id``.

    Without ``--branch`` the command iterates every branch and prints
    branch-grouped checkpoints. With ``--branch`` it lists only that
    branch's snapshots. Output is a tab-separated table feedable to
    downstream tools.
    """
    # pylint: disable=import-outside-toplevel
    from uuid import UUID

    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))

    if branch_id is not None:
        try:
            bid = UUID(branch_id)
        except ValueError as exc:
            click.echo(f"rewind: --branch must be a UUID, got {branch_id!r}", err=True)
            raise click.exceptions.Exit(2) from exc
        _print_checkpoints_for_branch(store, bid)
        return

    # No branch filter: scan all branches in the trace, group output.
    branches = store.list_branches(trace_id)
    if not branches:
        click.echo(f"rewind: no branches found for trace {trace_id}", err=True)
        raise click.exceptions.Exit(1)

    for branch in branches:
        click.echo(f"# branch {branch.branch_id} ({branch.label or branch.mode})")
        _print_checkpoints_for_branch(store, branch.branch_id)


def _print_checkpoints_for_branch(
    store: TraceStore, bid: UUID
) -> None:
    """Print one branch's checkpoints as a TSV block.

    Helper for ``checkpoint list``; kept at module top-level so test
    fixtures can call it directly without going through click.

    Args:
        store: A :class:`~rewind.storage.TraceStore` instance.
        bid: Branch UUID to list checkpoints for.
    """
    cps: list[Checkpoint] = store.list_checkpoints(bid)
    if not cps:
        click.echo("(no checkpoints)")
        return
    for cp in cps:
        click.echo(
            f"{cp.name}\tcursor={cp.cursor_index}\t"
            f"created={cp.created_at}\tlabel={cp.label or '-'}\t"
            f"keys={sorted(cp.payload.keys())!r}"
        )


@checkpoint.command("restore")
@click.argument("trace_id")
@click.argument("name")
@click.option(
    "--branch",
    "branch_id",
    required=True,
    help="Branch id the checkpoint was captured under (required: names are "
    "scoped per-branch).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read.",
)
def checkpoint_restore(
    trace_id: str, name: str, branch_id: str, db_path: Path
) -> None:
    """Print a stored checkpoint's payload as JSON.

    Use this to inspect a snapshot captured during a prior agent run, or
    to pipe into a ``diff`` against the live state. The ``trace_id`` is
    accepted for symmetry with the rest of the CLI but is not used in
    the lookup — ``(branch_id, name)`` is the unique key.
    """
    # pylint: disable=import-outside-toplevel
    import json as _json
    from uuid import UUID

    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    _ = trace_id  # parsed but unused — present for CLI symmetry
    store = TraceStore(db_path=str(db_path))
    try:
        bid = UUID(branch_id)
    except ValueError as exc:
        click.echo(f"rewind: --branch must be a UUID, got {branch_id!r}", err=True)
        raise click.exceptions.Exit(2) from exc

    cp = store.get_checkpoint(bid, name)
    if cp is None:
        click.echo(
            f"rewind: no checkpoint named {name!r} on branch {branch_id}",
            err=True,
        )
        raise click.exceptions.Exit(1)
    click.echo(_json.dumps(cp.payload, sort_keys=True, indent=2))


# ----------------------------------------------------------------------
# Phase 7 — local-model enrichment commands
# ----------------------------------------------------------------------


@cli.command()
@click.argument("trace_id")
@click.option(
    "--branch",
    "branch_id",
    default=None,
    help="Branch id to enrich. Omit to enrich the trace's root timeline.",
)
@click.option(
    "--quant/--no-quant",
    "parse_quant",
    default=True,
    show_default=True,
    help="Parse model_name for GGUF quant tags (q4_K_M, q8_0, f16, ...) and "
    "stamp the result onto span.raw_attributes['rewind.local.quant'].",
)
@click.option(
    "--vram/--no-vram",
    "sample_vram_flag",
    default=False,
    show_default=True,
    help="Sample GPU memory and utilisation once per LLM span "
    "(nvidia-smi / asitop / macmon / psutil). Off by default: sampler probes "
    "an external process and adds ~2ms per LLM span.",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read + write.",
)
def enrich(
    trace_id: str,
    branch_id: str | None,
    parse_quant: bool,
    sample_vram_flag: bool,
    db_path: Path,
) -> None:
    """Apply Phase 7 local-model enrichment to every span in a trace/branch.

    Walks every span, calls :func:`rewind.enrichment.enrich_span`, and
    persists the updated ``raw_attributes`` back into the store. Idempotent —
    re-running with the same flags produces the same output.

    \b
    Examples:
      rewind enrich <trace>                           # quant only (default)
      rewind enrich <trace> --vram                    # quant + GPU sample
      rewind enrich <trace> --branch <uuid> --no-quant
    """
    # pylint: disable=import-outside-toplevel
    from uuid import UUID

    from rewind.enrichment import enrich_span
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))
    bid: UUID | None = UUID(branch_id) if branch_id else None

    # get_trace() loads the root branch only; for non-root branches we pull
    # spans via get_spans() to span both the original timeline and branches.
    if bid is None:
        trace = store.get_trace(trace_id)
        if trace is None:
            click.echo(f"rewind: no such trace {trace_id!r}", err=True)
            raise click.exceptions.Exit(1)
        spans = trace.spans
    else:
        spans = store.get_spans(trace_id, branch_id=bid)
        if not spans:
            click.echo(
                f"rewind: no spans found for trace {trace_id!r} branch {branch_id!r}",
                err=True,
            )
            raise click.exceptions.Exit(1)

    enriched = 0
    for span in spans:
        # enrich_span mutates raw_attributes in place; insert_span's
        # ON CONFLICT clause persists the updated JSON back to the row.
        enrich_span(span, parse_model_quant=parse_quant, sample_gpu=sample_vram_flag)
        store.insert_span(span, branch_id=bid)
        enriched += 1
    click.echo(
        f"rewind enrich → enriched {enriched} span(s) "
        f"(quant={parse_quant}, vram={sample_vram_flag})",
        err=True,
    )


@cli.command(name="render-template")
@click.argument("trace_id")
@click.argument("span_index", type=int)
@click.option(
    "--branch",
    "branch_id",
    default=None,
    help="Branch id containing the span. Omit for the root timeline.",
)
@click.option(
    "--model",
    "model_override",
    default=None,
    help="Override the model name when resolving a HuggingFace tokenizer. "
    "Useful when the recorded model_name is an Ollama tag and you want to "
    "render against the upstream HF repo (e.g. 'Qwen/Qwen3-32B-Instruct').",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
    help="SQLite database file to read.",
)
def render_template(
    trace_id: str,
    span_index: int,
    branch_id: str | None,
    model_override: str | None,
    db_path: Path,
) -> None:
    """Render the post-chat-template prompt for one LLM span.

    Phase 7 inspection tool: local-model failures routinely hide in the
    chat template (missing ``<|im_start|>``, wrong role tags). This
    renders the exact string the model would see — using transformers'
    ``apply_chat_template`` when the tokenizer is available, or a
    readable ``[role] content`` fallback otherwise.

    \b
    Exit codes:
      0  span rendered (transformers or fallback)
      1  span not found / not an LLM span / no messages attribute
    """
    # pylint: disable=import-outside-toplevel
    from uuid import UUID

    from rewind.enrichment import render_chat_template
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))
    bid: UUID | None = UUID(branch_id) if branch_id else None
    spans = store.get_spans(trace_id, branch_id=bid)
    if not spans:
        click.echo(f"rewind: no such trace {trace_id!r}", err=True)
        raise click.exceptions.Exit(1)
    if span_index < 0 or span_index >= len(spans):
        click.echo(
            f"rewind: span_index {span_index} out of range "
            f"(trace has {len(spans)} spans)",
            err=True,
        )
        raise click.exceptions.Exit(1)

    span = spans[span_index]
    messages = span.raw_attributes.get("gen_ai.prompt") or span.raw_attributes.get(
        "llm.input_messages"
    )
    if not isinstance(messages, list):
        click.echo(
            "rewind: span has no messages to render "
            "(expected gen_ai.prompt or llm.input_messages list)",
            err=True,
        )
        raise click.exceptions.Exit(1)

    rendered = render_chat_template(
        messages,
        model_name=model_override or span.model_name,
    )
    click.echo(rendered)


# ----------------------------------------------------------------------
# Phase 4.3 — regression command group
# ----------------------------------------------------------------------
@cli.group()
def regression() -> None:
    """Executable regression suite management (Phase 4).

    Create regression cases from golden traces, run them deterministically
    (frozen replay), and surface pass/fail for CI.
    """


@regression.command("create")
@click.option("--name", required=True, help="Human-readable case name.")
@click.option(
    "--trace-id",
    "seed_trace_id",
    required=True,
    help="The golden trace id to freeze as the regression baseline.",
)
@click.option(
    "--expect-span-count",
    type=int,
    default=None,
    help="Expected span count (optional assertion).",
)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
)
def regression_create(
    name: str,
    seed_trace_id: str,
    expect_span_count: int | None,
    db_path: Path,
) -> None:
    """Create a regression case from a golden trace."""
    # pylint: disable=import-outside-toplevel
    from uuid import uuid4

    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    db_path = _ensure_default_db_path(db_path)
    store = TraceStore(db_path=str(db_path))
    if store.get_trace(seed_trace_id) is None:
        click.echo(f"rewind: seed trace {seed_trace_id!r} not found", err=True)
        raise click.exceptions.Exit(1)

    expected: dict[str, Any] = {}
    if expect_span_count is not None:
        expected["span_count"] = expect_span_count

    case_id = str(uuid4())
    store.upsert_regression_case(
        {
            "case_id": case_id,
            "name": name,
            "seed_trace_id": seed_trace_id,
            "expected": expected,
        }
    )
    click.echo(f"created regression case {case_id} ({name})")


@regression.command("run")
@click.argument("case_ids", nargs=-1, required=True)
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
)
@click.option(
    "--concurrency",
    type=int,
    default=4,
    show_default=True,
    help="Max parallel cases.",
)
def regression_run(
    case_ids: tuple[str, ...],
    db_path: Path,
    concurrency: int,
) -> None:
    """Run one or more regression cases and exit 1 on any failure.

    \b
    CASE_IDS is one or more regression-case ids (as created by
    ``rewind regression create``). Pass ``all`` to run every case.

    \b
    Exit codes:
      0  all cases passed
      1  at least one case failed
      2  no cases found / error
    """
    # pylint: disable=import-outside-toplevel
    import asyncio

    from rewind.storage import TraceStore
    from rewind.suite_runner import SuiteRunner
    # pylint: enable=import-outside-toplevel

    db_path = _ensure_default_db_path(db_path)
    store = TraceStore(db_path=str(db_path))

    if len(case_ids) == 1 and case_ids[0] == "all":
        all_cases = store.list_regression_cases()
        ids = [c["case_id"] for c in all_cases]
    else:
        ids = list(case_ids)

    if not ids:
        click.echo("rewind: no regression cases to run", err=True)
        raise click.exceptions.Exit(2)

    runner = SuiteRunner(store, case_ids=ids, concurrency=concurrency)

    async def _drive() -> bool:
        async for event in runner.run():
            if event["type"] == "case_done":
                verdict = "PASS" if event["passed"] else "FAIL"
                click.echo(f"  [{verdict}] {event['case_id']}: {event['detail']}")
            elif event["type"] == "suite_finished":
                return bool(event["passed"])
        return False

    passed = asyncio.run(_drive())
    s = runner.summary
    click.echo(
        f"regression suite: {s['passed']} passed, {s['failed']} failed, "
        f"{s['errored']} errored (total {s['total']})"
    )
    if passed:
        return
    raise click.exceptions.Exit(1)


@regression.command("list")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
)
def regression_list(db_path: Path) -> None:
    """List all regression cases."""
    # pylint: disable=import-outside-toplevel
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    store = TraceStore(db_path=str(db_path))
    cases = store.list_regression_cases()
    if not cases:
        click.echo("(no regression cases)")
        return
    for c in cases:
        click.echo(f"  {c['case_id']}  {c['name']}  trace={c['seed_trace_id']}")


# ----------------------------------------------------------------------
# Phase 5.4 — export with configurable redaction
# ----------------------------------------------------------------------
@cli.command()
@click.argument("trace_id")
@click.option(
    "--db",
    "db_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_DB,
    show_default=True,
)
@click.option(
    "-o",
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Output file (JSON). Defaults to stdout.",
)
@click.option(
    "--redact-field",
    "redact_fields",
    multiple=True,
    help="raw_attributes key to drop entirely (repeatable).",
)
@click.option(
    "--redact-pattern",
    "redact_patterns",
    multiple=True,
    help="Regex pattern to mask in string values (repeatable).",
)
@click.option(
    "--preview",
    is_flag=True,
    default=False,
    help="Show what would be redacted without exporting.",
)
def export(
    trace_id: str,
    db_path: Path,
    output: Path | None,
    redact_fields: tuple[str, ...],
    redact_patterns: tuple[str, ...],
    preview: bool,
) -> None:
    """Export a trace as JSON with optional field/pattern redaction (Phase 5.4).

    \b
    Examples:
      rewind export <trace_id> -o trace.json
      rewind export <trace_id> --redact-field gen_ai.response
      rewind export <trace_id> --redact-pattern '\\b\\d{3}-\\d{2}-\\d{4}\\b'
      rewind export <trace_id> --preview --redact-pattern '[A-Z]{4}'
    """
    # pylint: disable=import-outside-toplevel
    import json

    from rewind.redaction import RedactionPolicy, apply_redaction, preview_redaction
    from rewind.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    db_path = _ensure_default_db_path(db_path)
    store = TraceStore(db_path=str(db_path))
    trace = store.get_trace(trace_id)
    if trace is None:
        click.echo(f"rewind: trace {trace_id!r} not found", err=True)
        raise click.exceptions.Exit(1)

    policy = RedactionPolicy.from_cli(
        redact_fields=list(redact_fields) or None,
        redact_patterns=list(redact_patterns) or None,
    )

    if preview:
        counts = preview_redaction(trace.spans, policy)
        click.echo(
            f"redaction preview: {counts['fields_dropped']} fields dropped, "
            f"{counts['pattern_matches']} pattern matches across "
            f"{len(trace.spans)} spans"
        )
        return

    spans = apply_redaction(trace.spans, policy)
    payload = {
        "trace_id": trace.trace_id,
        "root_branch_id": str(trace.root_branch_id),
        "created_at": trace.created_at,
        "spans": [s.model_dump(mode="json") for s in spans],
    }
    text = json.dumps(payload, indent=2, default=str)
    if output is not None:
        output.write_text(text, encoding="utf-8")
        click.echo(f"exported {len(spans)} spans to {output}")
    else:
        click.echo(text)


if __name__ == "__main__":
    main()
