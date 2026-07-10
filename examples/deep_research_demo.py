#!/usr/bin/env python3
"""Live Rewind demo — deep-research agent, capture → frozen → branch + diff.

This is the **headline live demo**. It runs a flattened deep-research agent
(mirroring langchain-ai/open_deep_research's node sequence but as a linear
chain of LLM calls) through Rewind's *real* capture/replay engine against a
local model server (Unsloth / Ollama / OpenAI-compatible).

Three phases, each printed live:

  A. CAPTURE  — 8 LLM calls, each captured as an OTel span via OpenInference.
  B. FROZEN   — re-run under `rewind.openai_intercept.patch()` + FROZEN replay.
                ZERO outbound calls; output matches the seed byte-for-byte.
  C. BRANCH   — fork at the supervisor span, edit the system prompt, re-run.
    + DIFF      The tail goes live; `span_diff` + `message_diff` show the change.

The agent pattern is ported from the Z.ai workspace demo (linear spans with
{output:N} prompt chaining) so it's reliable and fast — no structured-output
retries or parallel subgraphs. Each span is a real `openai.ChatCompletion`
call, which Rewind intercepts at the SDK boundary.

Run::

    # 1. start the Rewind receiver
    rewind serve --port 4318 --db /tmp/rewind-demo.db

    # 2. start your local model server (Unsloth Studio / Ollama)

    # 3. run the demo
    python examples/deep_research_demo.py
"""

from __future__ import annotations

import contextlib
import os
import sys
import time
from typing import Any

# --- env wiring (must precede SDK imports) --------------------------------
os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
os.environ.setdefault("OTEL_BSP_SCHEDULE_DELAY", "100")
os.environ.setdefault("OPENAI_BASE_URL", "http://localhost:8888/v1")
os.environ.setdefault("OPENAI_API_KEY", os.environ.get("REWIND_API_KEY", "sk-unsloth-local"))

MODEL = os.environ.get("REWIND_MODEL", "unsloth/Qwen3.6-27B-MTP-GGUF")
DEFAULT_DB = os.environ.get("REWIND_DB", "/tmp/rewind-demo.db")
SEED_QUERY = "Compare RLHF vs DPO for aligning large language models, with citations."

# --------------------------------------------------------------------------
# Telemetry — wire a TracerProvider + OTLP exporter so spans reach Rewind.
# --------------------------------------------------------------------------
_TELEMETRY_READY = False


def _setup_telemetry() -> None:
    global _TELEMETRY_READY
    if _TELEMETRY_READY:
        return
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": "rewind-demo"}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
    trace.set_tracer_provider(provider)
    _TELEMETRY_READY = True


def _banner(title: str) -> None:
    print(f"\n{'═' * 72}\n  {title}\n{'═' * 72}")


def _die(msg: str, hint: str = "") -> None:
    print(f"\n✗ {msg}", file=sys.stderr)
    if hint:
        print(hint, file=sys.stderr)
    sys.exit(2)


# --------------------------------------------------------------------------
# The flattened deep-research agent.
# --------------------------------------------------------------------------
# Each step: (node name, system prompt, user-input template).
# Templates use {query} and {output:N} (output of span at index N).
PROMPTS: list[tuple[str, str, str]] = [
    (
        "clarify_with_user",
        "You are a research intake assistant. Decide whether the user's research query needs "
        "clarification. If it is already specific enough, output exactly PROCEED. "
        "Be concise — one short paragraph at most.",
        "Research query: {query}",
    ),
    (
        "write_research_brief",
        "You are a lead researcher. Convert the conversation into a detailed research brief. "
        "Cover: scope, 3-5 key sub-questions, intended audience, and success criteria. "
        "Output as a short markdown document with headings: Scope, Key Questions, Audience, Success Criteria.",
        "Original query: {query}\n\nClarify step output:\n{output:0}\n\nWrite the research brief now.",
    ),
    (
        "supervisor_think",
        "You are the research supervisor. Reflect on the brief and identify the first 2 specific "
        "research topics to investigate. For each topic, state the topic and one sentence on why "
        "it matters. Output as a numbered list.",
        "Research brief:\n{output:1}",
    ),
    (
        "conduct_research",
        "You are a researcher. Given a research topic, produce compressed research notes: 3-5 key "
        "findings, each one sentence, with a plausible citation in the form [Source: description]. "
        "Do not browse the web — reason from your training knowledge.",
        "Research brief:\n{output:1}\n\nSupervisor topics:\n{output:2}\n\nInvestigate topic #1 in depth.",
    ),
    (
        "supervisor_think",
        "You are the research supervisor. Review the findings so far. Decide whether more research "
        "is needed. If yes, identify ONE follow-up topic. If no, output exactly COMPLETE.",
        "Brief:\n{output:1}\n\nFindings from topic #1:\n{output:3}",
    ),
    (
        "conduct_research",
        "You are a researcher. Given a research topic, produce compressed research notes: 3-5 key "
        "findings, each one sentence, with a plausible citation in the form [Source: description]. "
        "Do not browse the web — reason from your training knowledge.",
        "Research brief:\n{output:1}\n\nSupervisor follow-up:\n{output:4}\n\nInvestigate the follow-up topic.",
    ),
    (
        "research_complete",
        "You are the research supervisor. Summarize the research that was conducted. List the "
        "consolidated key findings that will feed into the final report. Output as a bulleted list.",
        "Brief:\n{output:1}\n\nFindings #1:\n{output:3}\n\nFindings #2:\n{output:5}",
    ),
    (
        "final_report",
        "You are a senior research writer. Synthesize the brief and findings into a structured "
        "markdown research report. Use sections: Executive Summary, Key Findings, Analysis, "
        "Conclusion. Keep it under 400 words.",
        "Research brief:\n{output:1}\n\nConsolidated findings:\n{output:6}\n\nWrite the final report now.",
    ),
]


def _fill(template: str, query: str, outputs: dict[int, str]) -> str:
    out = template.replace("{query}", query)
    for idx, val in outputs.items():
        out = out.replace(f"{{output:{idx}}}", val)
    return out


def _call_llm(client: Any, system_prompt: str, user_input: str) -> str:
    """One LLM call through the OpenAI SDK. Rewind intercepts this."""
    resp = client.chat.completions.create(
        model=MODEL,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_input},
        ],
        max_tokens=400,
        temperature=0.3,
        # Qwen3.6 emits <think> blocks; we don't want them polluting the output.
        extra_body={"chat_template_kwargs": {"enable_thinking": False}},
    )
    return resp.choices[0].message.content or ""


def run_agent(client: Any, query: str, *, edit_at: int | None = None,
              edited_system: str | None = None,
              cached_outputs: dict[int, str] | None = None) -> list[dict[str, Any]]:
    """Run the 8-step agent inside ONE parent OTel span.

    The parent span (kind=AGENT) is what makes all 8 child LLM spans share a
    single ``trace_id`` — without it, OpenInference emits each ``create()``
    as its own one-span trace, and frozen replay can't match a multi-call
    agent against a single trace. This mirrors how real agent frameworks
    (LangGraph / ADK) emit: one parent agent span, many child LLM spans.
    """
    from opentelemetry import trace  # pylint: disable=import-outside-toplevel

    tracer = trace.get_tracer("rewind-demo")
    outputs: dict[int, str] = dict(cached_outputs or {})
    spans: list[dict[str, Any]] = []
    start_from = edit_at or 0

    with tracer.start_as_current_span("deep_research_agent") as parent:
        parent.set_attribute("gen_ai.system", "rewind-demo")
        parent.set_attribute("gen_ai.operation.name", "agent")
        for i, (name, system_prompt, user_template) in enumerate(PROMPTS):
            if i < start_from:
                outputs.setdefault(i, outputs.get(i, ""))
                spans.append({"index": i, "name": name, "output": outputs[i], "source": "cached"})
                continue
            sys_p = edited_system if (edit_at is not None and i == edit_at) else system_prompt
            user_in = _fill(user_template, query, outputs)
            t0 = time.time()
            out = _call_llm(client, sys_p, user_in)
            dt = time.time() - t0
            outputs[i] = out
            spans.append({"index": i, "name": name, "output": out, "source": "live",
                          "system_prompt": sys_p, "user_input": user_in, "latency_s": round(dt, 1)})
    return spans


# --------------------------------------------------------------------------
# Phases
# --------------------------------------------------------------------------
def _make_client() -> Any:
    try:
        from openai import OpenAI  # pylint: disable=import-outside-toplevel
    except ImportError as exc:
        _die(f"openai SDK not installed ({exc})", "pip install openai")
    return OpenAI()


def phase_capture(client: Any) -> dict[str, Any]:
    """Phase A — run the agent live; OpenInference captures every span."""
    _banner("PHASE A — CAPTURE (live run against Qwen3.6-27B)")
    print(f"  query    : {SEED_QUERY}")
    print(f"  model    : {MODEL}")
    print(f"  endpoint : {os.environ.get('OPENAI_BASE_URL')}")
    print(f"  spans    : {len(PROMPTS)} LLM calls (clarify → brief → research → report)\n")

    try:
        from openinference.instrumentation.openai import OpenAIInstrumentor
    except ImportError:
        _die("openinference-instrumentation-openai not installed.",
             "pip install openinference-instrumentation-openai")

    _setup_telemetry()
    OpenAIInstrumentor().instrument()

    try:
        # The OpenAI SDK is what both the agent AND Rewind's intercept use.
        # OpenInference wraps openai.ChatCompletion.create → emits gen_ai.*
        # spans to the Rewind receiver.
        spans = run_agent(client, SEED_QUERY)
    finally:
        with contextlib.suppress(Exception):
            OpenAIInstrumentor().uninstrument()

    print("\n  --- span summary ---")
    for s in spans:
        if s["source"] == "live":
            preview = s["output"][:70].replace("\n", " ")
            print(f"  [{s['index']}] {s['name']:<22} {s.get('latency_s','?'):>5}s  {preview}…")

    final = spans[-1]["output"]
    print(f"\n  FINAL REPORT (first 300 chars):\n  {final[:300]}")
    return {"spans": spans, "final_report": final, "query": SEED_QUERY}


def phase_frozen(client: Any, captured: dict[str, Any]) -> None:
    """Phase B — FROZEN replay: zero outbound calls, output matches seed."""
    from rewind.enums import ReplayMode
    from rewind.openai_intercept import patch
    from rewind.replay import replay as replay_ctx
    from rewind.storage import TraceStore

    store = TraceStore(DEFAULT_DB)
    traces, _ = store.list_traces(limit=1)
    if not traces:
        print("\n  ⚠ no captured trace found — skipping frozen replay")
        return
    trace_id = traces[0].trace_id
    seed = store.get_trace(trace_id)
    seed_report = captured["final_report"]

    _banner("PHASE B — FROZEN REPLAY (zero egress, deterministic)")
    print(f"  trace_id : {trace_id}")
    print(f"  spans    : {len(seed.spans)} recorded LLM calls")
    print("  mode     : FROZEN — every call served from the fixture cache\n")

    # Count outbound calls by wrapping the client's create.
    call_count = {"n": 0}
    orig_create = client.chat.completions.create

    def _counting_create(*a: Any, **kw: Any) -> Any:
        call_count["n"] += 1
        return orig_create(*a, **kw)

    client.chat.completions.create = _counting_create  # type: ignore[method-assign]
    try:
        with patch(), replay_ctx(store, trace_id, mode=ReplayMode.FROZEN):
            replayed = run_agent(client, captured["query"])
    finally:
        client.chat.completions.create = orig_create  # type: ignore[method-assign]

    replay_report = replayed[-1]["output"]
    match = replay_report.strip() == seed_report.strip()
    print(f"  outbound LLM calls during replay : {call_count['n']}  (must be 0)")
    print(f"  output matches seed              : {'✓ YES' if match else '✗ NO'}")
    if match:
        print("\n  ✓ FROZEN REPLAY CONFIRMED — full 8-span agent replayed offline,")
        print("    byte-for-byte identical to the original live run. Zero tokens spent.")


def phase_branch(client: Any, captured: dict[str, Any]) -> None:
    """Phase C — BRANCH at the supervisor span (index 2), edit the prompt."""
    from rewind.diff import message_diff, span_diff
    from rewind.enums import ReplayMode, SpanKind
    from rewind.models import Span, hash_payload
    from rewind.openai_intercept import patch
    from rewind.replay import replay as replay_ctx
    from rewind.storage import TraceStore

    store = TraceStore(DEFAULT_DB)
    traces, _ = store.list_traces(limit=1)
    if not traces:
        print("\n  ⚠ no captured trace — skipping branch")
        return
    trace_id = traces[0].trace_id
    seed_spans = store.get_trace(trace_id).spans
    branch_at = 2  # the supervisor_think span

    edited_system = (
        "You are the research supervisor. Reflect on the brief and identify exactly 3 specific, "
        "NON-OVERLAPPING research topics. The three must cover different facets (one historical, "
        "one technical, one comparative). Output a numbered list (1., 2., 3.)."
    )

    _banner("PHASE C — BRANCH + DIFF (fork @ supervisor span, edit prompt)")
    print(f"  trace_id    : {trace_id}")
    print(f"  branch_at   : span {branch_at} (supervisor_think)")
    print("  prompt edit : '2 topics' → '3 non-overlapping topics (historical/technical/comparative)'\n")

    with patch(), replay_ctx(store, trace_id, mode=ReplayMode.BRANCH, branch_at=branch_at) as session:
        # Rebuild the agent run with the edited prompt at the branch point.
        cached = {i: captured["spans"][i]["output"] for i in range(branch_at)}
        branched = run_agent(client, captured["query"], edit_at=branch_at,
                             edited_system=edited_system, cached_outputs=cached)
        branch_id = session.branch_id

    # Capture the new spans into the store so the UI shows the branch.
    new_spans: list[Span] = []
    for i, s in enumerate(branched):
        if s["source"] != "live":
            continue
        msgs = [
            {"role": "system", "content": s.get("system_prompt", "")},
            {"role": "user", "content": s.get("user_input", "")},
        ]
        new_spans.append(Span(
            trace_id=trace_id,
            span_id=f"{i:016x}",
            parent_span_id=None,
            name=s["name"],
            kind=SpanKind.LLM,
            model_name=MODEL,
            messages_hash=hash_payload(msgs),
            raw_attributes={
                "gen_ai.request.model": MODEL,
                "gen_ai.response": {
                    "choices": [{"message": {"role": "assistant", "content": s["output"]}}],
                },
            },
        ))
    for sp in new_spans:
        store.insert_span(sp, branch_id=branch_id)

    print(f"\n  branch_id          : {branch_id}")
    print(f"  live spans captured: {len(new_spans)} (the divergent tail)")
    branch_report = branched[-1]["output"]
    print(f"\n  BRANCH REPORT (first 300 chars):\n  {branch_report[:300]}")

    # Diff the final report seed vs branch.
    _banner("DIFF — seed final_report vs branch final_report")
    md = message_diff(captured["final_report"], branch_report)
    print(f"  token-level diff: +{md.added_tokens} added / -{md.removed_tokens} removed")
    print(f"  fragments: {len(md.fragments)} ({sum(1 for f in md.fragments if f.kind == 'equal')} equal, "
          f"{sum(1 for f in md.fragments if f.kind in ('added','changed'))} added/changed, "
          f"{sum(1 for f in md.fragments if f.kind in ('removed','changed'))} removed)")
    print("\n  First divergent fragment:")
    for f in md.fragments:
        if f.kind != "equal":
            print(f"    [{f.kind}] {f.text[:120]}")
            break

    # span_diff between seed and reconstructed branch timeline.
    branch_timeline = seed_spans[:branch_at] + new_spans
    diff = span_diff(seed_spans, branch_timeline)
    print(f"\n  span_diff: first_divergence_index = {diff.first_divergence_index} "
          f"(expected {branch_at})")
    assert diff.first_divergence_index == branch_at, "divergence should be at the branch point"


# --------------------------------------------------------------------------
def main() -> int:
    _banner("REWIND x DEEP RESEARCH — live demo (Qwen3.6-27B via Unsloth)")
    client = _make_client()
    captured = phase_capture(client)
    phase_frozen(client, captured)
    phase_branch(client, captured)
    _banner("DONE")
    print(f"  DB: {DEFAULT_DB}")
    print("  Open http://127.0.0.1:8484/ui/ (if `rewind ui` is running) to inspect")
    print("  the trace + branch tree, or query the DB directly:")
    # Static query (no user input) — S608 is a false positive here.
    print(f"    sqlite3 {DEFAULT_DB} \"SELECT branch_id, kind, name FROM spans "  # noqa: S608
          "ORDER BY start_time;\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
