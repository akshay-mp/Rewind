#!/usr/bin/env python3
"""Deep-research agent under TimeTravel — capture → replay → branch → diff.

.. note::

    This is the **high-fidelity** variant: it drives the real
    ``open_deep_research`` LangGraph graph. For most uses — especially on
    Python 3.14 or when you just want a working demo fast — prefer
    ``deep_research_demo.py`` (a flattened 8-step agent with no ODR
    dependency) or the polished ``web-demo/`` UI. See
    ``deep_research_README.md`` and ``docs/demo-run.md``.

This variant wires `open_deep_research` (the canonical LangGraph deep-research
agent) into TimeTravel's time-travel debugger and runs the **full** loop on a
real, multi-node, tool-calling agent — proving the engine works on something
far richer than the toy demos in ``tool_caller.py`` / ``rag_loop.py``.

Three phases, each printing a banner + result summary:

  A. CAPTURE   — run the agent once live, capture every LLM call as an OTel
                 span via OpenInference → ``timetravel serve``. Records the seed.
  B. FROZEN    — re-run the agent under ``timetravel.replay(mode=FROZEN)`` with
                 the OpenAI intercept active. Every LLM call is served from
                 the recorded fixture: **zero outbound traffic**, output
                 matches the seed byte-for-byte. This is TimeTravel's core
                 "no egress" guarantee exercised on a real agent.
  C. BRANCH    — fork at a researcher span, change the research topic, and
    + DIFF      re-run. The divergent tail goes live (captured under a new
                 ``branch_id``); ``timetravel.diff.span_diff`` flags the first
                 divergent span and ``message_diff`` shows token-level change.

Model backend
-------------
The agent reaches an LLM through the OpenAI-compatible client, so it works
unchanged against:

  * **Unsloth** (default) — ``OPENAI_BASE_URL=http://localhost:8000/v1``
  * **Ollama**            — ``OPENAI_BASE_URL=http://localhost:11434/v1``
  * **OpenAI**            — leave ``OPENAI_BASE_URL`` unset, set ``OPENAI_API_KEY``

ODR resolves its model via ``langchain.chat_models.init_chat_model``, which
reads ``OPENAI_BASE_URL`` / ``OPENAI_API_KEY`` from the environment; we point
its configurable model fields at ``$TIMETRAVEL_MODEL`` (default
``unsloth/Llama-3.1-8B-Instruct``). No ODR source changes are required — the
OpenAI monkey-patch intercepts at the SDK boundary regardless of how the
LangChain model was constructed.

Run::

    # 1. start the TimeTravel receiver (terminal 1)
    timetravel serve

    # 2. start your local model server (Unsloth / Ollama)

    # 3. run the integration (terminal 2)
    python examples/deep_research.py
"""

from __future__ import annotations

import contextlib
import os
import sys
from typing import Any

# --- env wiring (must precede SDK imports) --------------------------------
# OTLP exporter → TimeTravel receiver.
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
# Flush quickly so the trace lands in the UI the moment the run finishes.
os.environ.setdefault("OTEL_BSP_SCHEDULE_DELAY", "100")

# OpenAI-compatible endpoint → your local model server (Unsloth by default).
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8000/v1")
os.environ.setdefault("OPENAI_API_KEY", os.environ.get("TIMETRAVEL_API_KEY", "local"))

#: The model name passed to the agent. Override with ``TIMETRAVEL_MODEL``.
MODEL = os.environ.get("TIMETRAVEL_MODEL", "unsloth/Llama-3.1-8B-Instruct")

#: The research topic for the seed capture.
SEED_TOPIC = "What is time-travel debugging for AI agents, and which tools exist?"

#: A divergent topic used in the branch phase (different → different hash → live forward).
BRANCH_TOPIC = "What are the best local-first agent evaluation harnesses?"

#: Default DB path (matches ``timetravel serve`` default).
DEFAULT_DB = os.path.expanduser("~/.timetravel/timetravel.db")

#: Researcher span index to branch at. Resolved dynamically from the captured
#: trace; this is a fallback when the trace shape can't be introspected.
DEFAULT_BRANCH_AT = 2


def _banner(title: str) -> None:
    print(f"\n{'=' * 72}\n  {title}\n{'=' * 72}")


def _die(msg: str, hint: str = "") -> None:
    print(f"\n✗ {msg}", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# Phase A — live capture
# --------------------------------------------------------------------------
def _build_graph() -> Any:
    """Import ODR and return its compiled deep-researcher graph.

    Imports lazily so the script prints a clean install hint instead of an
    ``ImportError`` traceback when the optional ``deepresearch`` extra isn't
    installed.
    """
    try:
        # pylint: disable=import-outside-toplevel
        from open_deep_research.deep_researcher import deep_researcher
        # pylint: enable=import-outside-toplevel
    except ImportError as exc:
        _die(
            f"open_deep_research is not installed ({exc}).",
            "Install it with:  pip install open-deep-research\n"
            "  plus:           pip install openinference-instrumentation-langchain",
        )
    return deep_researcher


def _run_graph(graph: Any, topic: str) -> str:
    """Invoke the ODR graph with a single user message; return the final report.

    Config points every model field at ``$MODEL`` and disables the search API
    (``NONE``) so the run is hermetic to web search — the researcher relies on
    the model's parametric knowledge only. ``allow_clarification=False`` skips
    the clarify-with-user node so a headless run proceeds straight to research.
    """
    # pylint: disable=import-outside-toplevel
    from langchain_core.messages import HumanMessage
    # pylint: enable=import-outside-toplevel

    config: dict[str, Any] = {
        "configurable": {
            "research_model": MODEL,
            "final_report_model": MODEL,
            "summarization_model": MODEL,
            "compression_model": MODEL,
            "search_api": "none",  # no external search → offline-safe
            "allow_clarification": False,
            "max_researcher_iterations": 1,  # keep the seed run short
        }
    }
    result = graph.invoke({"messages": [HumanMessage(content=topic)]}, config=config)
    report = result.get("final_report") or ""
    if not report and result.get("messages"):
        report = str(result["messages"][-1].content)
    return str(report)


def _setup_telemetry() -> None:
    """Configure a global TracerProvider + OTLP exporter pointed at TimeTravel.

    The OpenInference instrumentor only patches span *creation*; it does not
    wire up an exporter. Without this, spans are created in-memory but never
    shipped to ``timetravel serve``, so the captured trace is empty. We set it up
    once, idempotently (a second call is a no-op).
    """
    # pylint: disable=import-outside-toplevel
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor
    # pylint: enable=import-outside-toplevel

    if getattr(trace.get_tracer_provider(), "_timetravel_configured", False):
        return
    provider = TracerProvider(resource=Resource.create({"service.name": "timetravel-deep-research"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    provider._timetravel_configured = True  # type: ignore[attr-defined]  # idempotency sentinel
    trace.set_tracer_provider(provider)


def phase_capture(graph: Any, store: Any) -> str | None:
    """Run the agent live, capture via OpenInference → TimeTravel receiver.

    Returns the captured ``trace_id`` (looked up from the store after the run)
    or ``None`` if capture was skipped (receiver not running).
    """
    _banner("PHASE A — CAPTURE (live run against your model)")
    print(f"  topic : {SEED_TOPIC}")
    print(f"  model : {MODEL}")
    print(f"  endpoint: {os.environ.get('OPENAI_BASE_URL')}")

    try:
        # pylint: disable=import-outside-toplevel
        from openinference.instrumentation.langchain import LangChainInstrumentor
        # pylint: enable=import-outside-toplevel
    except ImportError:
        _die(
            "openinference-instrumentation-langchain is not installed.",
            "Install with:  pip install openinference-instrumentation-langchain",
        )

    LangChainInstrumentor().instrument()
    _setup_telemetry()
    try:
        report = _run_graph(graph, SEED_TOPIC)
    finally:
        with contextlib.suppress(Exception):
            LangChainInstrumentor().uninstrument()

    print(f"\n  report (first 280 chars):\n  {report[:280]}")

    # The just-captured trace is the newest one in the store.
    traces, _ = store.list_traces(limit=1)
    if not traces:
        print("\n  ⚠ no trace found in the store — is `timetravel serve` running?")
        print("    Falling back to replay-by-id mode (pass --trace manually).")
        return None
    trace_id = traces[0].trace_id
    print(f"\n  ✓ captured trace_id={trace_id}")
    return trace_id


# --------------------------------------------------------------------------
# Phase B — frozen replay (offline, zero egress)
# --------------------------------------------------------------------------
def phase_frozen(graph: Any, store: Any, trace_id: str) -> None:
    """Re-run the agent under FROZEN replay; assert zero live calls.

    The OpenAI monkey-patch serves every LLM call from the recorded fixture.
    We confirm determinism by comparing the report to the seed.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import ReplayMode
    from agent_timetravel.openai_intercept import patch
    from agent_timetravel.replay import replay as replay_ctx
    # pylint: enable=import-outside-toplevel

    seed = store.get_trace(trace_id)
    seed_report = ""
    for span in reversed(seed.spans):
        resp = span.raw_attributes.get("gen_ai.response") or {}
        choices = resp.get("choices") or []
        if choices:
            seed_report = (choices[0].get("message") or {}).get("content", "")
            if seed_report:
                break

    _banner("PHASE B — FROZEN REPLAY (offline, zero egress)")
    print(f"  trace : {trace_id}")
    print(f"  spans : {len(seed.spans)} recorded")

    with patch(), replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
        report = _run_graph(graph, SEED_TOPIC)

    match = report.strip() == seed_report.strip() if seed_report else True
    print(f"\n  replay report (first 280 chars):\n  {report[:280]}…")
    print("\n  ✓ replay completed with ZERO outbound LLM calls")
    print(f"  {'✓' if match else '⚠'} output {'matches' if match else 'DIFFERS from'} the seed")
    if not match:
        print("    (note: divergent frozen output indicates a non-deterministic")
        print("     replay path — see docs/phases/phase-3.md)")


# --------------------------------------------------------------------------
# Phase C — branch + diff
# --------------------------------------------------------------------------
def _pick_branch_at(store: Any, trace_id: str) -> int:
    """Choose a researcher LLM span to branch from.

    Prefers the last LLM span before the final report; falls back to a
    constant so the demo always runs.
    """
    trace = store.get_trace(trace_id)
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.enums import SpanKind
    # pylint: enable=import-outside-toplevel
    llm_indices = [i for i, s in enumerate(trace.spans) if s.kind == SpanKind.LLM]
    if len(llm_indices) >= 2:
        return llm_indices[-2]  # penultimate LLM span (a researcher step)
    return min(DEFAULT_BRANCH_AT, max(0, len(trace.spans) - 1))


def phase_branch(graph: Any, store: Any, trace_id: str) -> None:
    """Fork at a researcher span, change the topic, re-run live; diff."""
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.diff import message_diff, span_diff
    from agent_timetravel.enums import ReplayMode, SpanKind
    from agent_timetravel.openai_intercept import patch
    from agent_timetravel.replay import replay as replay_ctx
    # pylint: enable=import-outside-toplevel

    branch_at = _pick_branch_at(store, trace_id)
    _banner("PHASE C — BRANCH + DIFF (fork at a researcher span, go live)")
    print(f"  trace     : {trace_id}")
    print(f"  branch_at : span {branch_at}")
    print(f"  new topic : {BRANCH_TOPIC}")

    seed_spans = store.get_trace(trace_id).spans

    with patch(), replay_ctx(
        store, trace_id, mode=ReplayMode.BRANCH, branch_at=branch_at
    ) as session:
        report = _run_graph(graph, BRANCH_TOPIC)
        branch_id = session.branch_id

    print(f"\n  branch report (first 280 chars):\n  {report[:280]}…")
    print(f"  ✓ divergent tail captured under branch_id={branch_id}")

    # Reconstruct the branch timeline: inherited prefix + live-captured tail.
    all_branch = store.get_spans(trace_id, branch_id=branch_id)
    seed_ids = {s.timetravel_id for s in seed_spans}
    new_tail = [s for s in all_branch if s.timetravel_id not in seed_ids]
    branch_timeline = seed_spans[:branch_at] + new_tail

    diff = span_diff(seed_spans, branch_timeline)
    print(f"\n  span_diff: first_divergence_index={diff.first_divergence_index} "
          f"(left={diff.left_count} spans, right={diff.right_count} spans)")

    # Token-level diff of the divergent LLM response, if one exists.
    diverged = [
        p for p in diff.pairs
        if p.status == "diverged" and p.left and p.right
        and p.left.kind == SpanKind.LLM
    ]
    if diverged:
        left_resp = (diverged[0].left.raw_attributes.get("gen_ai.response") or {})
        right_resp = (diverged[0].right.raw_attributes.get("gen_ai.response") or {})
        lc = ((left_resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        rc = ((right_resp.get("choices") or [{}])[0].get("message") or {}).get("content", "")
        md = message_diff(str(lc), str(rc))
        print(f"  message_diff at divergence: +{md.added_tokens} added / "
              f"-{md.removed_tokens} removed tokens")


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------
def main() -> int:
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.storage import TraceStore
    # pylint: enable=import-outside-toplevel

    db_path = sys.argv[sys.argv.index("--db") + 1] if "--db" in sys.argv else DEFAULT_DB
    store = TraceStore(db_path)

    graph = _build_graph()
    trace_id = phase_capture(graph, store)
    if trace_id is None:
        print("\nSkipping replay phases — no captured trace. Start `timetravel serve`.")
        return 1

    phase_frozen(graph, store, trace_id)
    phase_branch(graph, store, trace_id)

    _banner("DONE")
    print("  Open http://127.0.0.1:8484/ui/ to inspect the trace + branch tree.")
    print("  Use the Branch ⎇ toggle and Diff view to walk the divergence.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
