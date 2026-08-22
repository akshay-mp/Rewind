"""Capture a real 2-step agent trace against the local Gemma model.

Prerequisites:
  * llama-server running Gemma at http://127.0.0.1:49998 (Unsloth)
  * TimeTravel OTLP receiver running at http://127.0.0.1:4318
    (start it first:  python -m agent_timetravel.cli serve --port 4318 --db ~/.agent-timetravel/timetravel.db)

This script:
  1. Configures OpenTelemetry to export to the TimeTravel receiver via OTLP/HTTP.
  2. Instruments the OpenAI SDK with OpenInference (captures gen_ai.* spans).
  3. Points an OpenAI client at the local Gemma endpoint.
  4. Runs a 2-step "agent": a planner call, then an executor call.
  5. Flushes the trace and prints the trace id.

The captured trace is what we then step through interactively in the UI
(see start_gemma_stepping.py).
"""

from __future__ import annotations

import sys
import time

# --- OpenAI client pointed at local Gemma --------------------------------
import openai

# --- OpenInference OpenAI instrumentation --------------------------------
from openinference.instrumentation.openai import OpenAIInstrumentor

# --- OpenTelemetry → TimeTravel OTLP receiver --------------------------------
from opentelemetry import trace
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

GEMMA_BASE_URL = "http://127.0.0.1:49301/v1"
GEMMA_MODEL = "gemma-4-12b-it-UD-Q4_K_XL"
REWIND_OTLP_ENDPOINT = "http://127.0.0.1:4318"
DB_PATH = "~/.agent-timetravel/timetravel.db"


def setup_tracing() -> None:
    """Wire OpenTelemetry → TimeTravel's OTLP receiver, and instrument OpenAI."""
    resource = Resource.create(
        {"service.name": "gemma-capture", "openinference.scope": "timetravel-demo"}
    )
    provider = TracerProvider(resource=resource)
    exporter = OTLPSpanExporter(endpoint=f"{REWIND_OTLP_ENDPOINT}/v1/traces")
    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    OpenAIInstrumentor().instrument()
    print(f"[capture] OTLP → {REWIND_OTLP_ENDPOINT}, OpenAI instrumented", file=sys.stderr)


def main() -> int:
    setup_tracing()

    # Point the OpenAI client at the local llama-server running Gemma. The
    # api_key is unused by llama-server but required by the SDK's type check.
    client = openai.OpenAI(base_url=GEMMA_BASE_URL, api_key="not-needed")

    tracer = trace.get_tracer("gemma-agent")

    # Step 1: planner — ask Gemma to break down a task.
    with tracer.start_as_current_span("agent.plan") as plan_span:
        plan_span.set_attribute("openinference.span.kind", "AGENT")
        print("[capture] step 1: planning…", file=sys.stderr, flush=True)
        plan_response = client.chat.completions.create(
            model=GEMMA_MODEL,
            messages=[
                {"role": "system", "content": "You are a concise planning assistant. Respond in 2 short bullet points."},
                {"role": "user", "content": "Plan how to brew a single cup of pour-over coffee."},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        plan_text = plan_response.choices[0].message.content or "(empty)"
        print(f"[capture]   plan: {plan_text[:120]}", file=sys.stderr, flush=True)

    # Step 2: executor — feed the plan back, ask for the first concrete action.
    with tracer.start_as_current_span("agent.execute") as exec_span:
        exec_span.set_attribute("openinference.span.kind", "AGENT")
        print("[capture] step 2: executing…", file=sys.stderr, flush=True)
        exec_response = client.chat.completions.create(
            model=GEMMA_MODEL,
            messages=[
                {"role": "system", "content": "You execute plans step by step. Give the first action only."},
                {"role": "user", "content": f"Here is the plan:\n{plan_text}\n\nWhat is the first concrete action?"},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        exec_text = exec_response.choices[0].message.content or "(empty)"
        print(f"[capture]   exec: {exec_text[:120]}", file=sys.stderr, flush=True)

    # Flush the batch span processor so spans reach the receiver before exit.
    provider = trace.get_tracer_provider()
    if hasattr(provider, "force_flush"):
        provider.force_flush()
    # Small grace period for the HTTP export + receiver ingest to complete.
    time.sleep(1.0)
    print(f"[capture] done. Trace shipped to {REWIND_OTLP_ENDPOINT}.", file=sys.stderr)
    print(f"[capture] DB: {DB_PATH}. Open the UI and look for a 2-LLM-span trace.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
