"""Start the stepping server with a real Gemma runner registered.

Usage:
    python examples/start_gemma_stepping.py --db /tmp/rewind-demo.db

Then open http://127.0.0.1:8484/ui, click "sessions", and start a session
with the captured trace id + runner ref "gemma".

Prerequisites:
  * A captured trace in the DB (run capture_gemma_trace.py first).
  * llama-server running Gemma at http://127.0.0.1:49998.

How it works:
  The runner re-issues the same two chat.completions.create calls that were
  captured, inside an INTERACTIVE replay context. The OpenAI interceptor
  (patched via openai_intercept.patch()) pauses at each call via the gate;
  the developer approves / edits / stops from the browser UI. An EDIT to the
  messages diverges from the recorded signature, so the call forwards LIVE
  to Gemma (BRANCH behavior past the cursor).
"""

from __future__ import annotations

import argparse
import sys

import openai

from rewind.openai_intercept import patch
from rewind.replay import ReplaySession
from rewind.stepping_api import register_runner

GEMMA_BASE_URL = "http://127.0.0.1:49301/v1"
GEMMA_MODEL = "gemma-4-12b-it-UD-Q4_KL"


async def gemma_runner(session: ReplaySession) -> None:
    """Re-run the 2-step Gemma agent under the stepping gate.

    The server has already opened the replay(mode=INTERACTIVE) context and
    bound it to the active session ContextVar. We just install the OpenAI
    patch and re-issue the calls — the interceptor pauses at each.

    Uses ``AsyncOpenAI`` so calls hit ``_dispatch_async`` (the gated path).
    The sync ``openai.OpenAI`` client would hit ``_dispatch_sync``, which
    does not yet carry the stepping gate — see phase-9.md §6.8 (future work:
    fan out the sync gate). This matches the tested, validated async path.
    """
    with patch():
        client = openai.AsyncOpenAI(base_url=GEMMA_BASE_URL, api_key="not-needed")

        # Step 1: planner (same call as captured).
        plan = await client.chat.completions.create(
            model=GEMMA_MODEL,
            messages=[
                {"role": "system", "content": "You are a concise planning assistant. Respond in 2 short bullet points."},
                {"role": "user", "content": "Plan how to brew a single cup of pour-over coffee."},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        plan_text = plan.choices[0].message.content or "(empty)"
        print(f"[runner] step 1 plan: {plan_text[:80]}", file=sys.stderr, flush=True)

        # Step 2: executor (same call as captured).
        exec_resp = await client.chat.completions.create(
            model=GEMMA_MODEL,
            messages=[
                {"role": "system", "content": "You execute plans step by step. Give the first action only."},
                {"role": "user", "content": f"Here is the plan:\n{plan_text}\n\nWhat is the first concrete action?"},
            ],
            max_tokens=400,
            temperature=0.3,
        )
        exec_text = exec_resp.choices[0].message.content or "(empty)"
        print(f"[runner] step 2 exec: {exec_text[:80]}", file=sys.stderr, flush=True)


def main() -> int:
    register_runner("gemma", gemma_runner)

    # pylint: disable=import-outside-toplevel
    import uvicorn

    from rewind.receiver import create_app
    from rewind.storage import TraceStore

    parser = argparse.ArgumentParser(description="Gemma stepping server.")
    parser.add_argument("--db", default="/tmp/rewind-demo.db", help="SQLite DB path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    args = parser.parse_args()

    store = TraceStore(args.db)
    app = create_app(store)
    print(
        f"[gemma-stepping] runner 'gemma' registered. "
        f"Serving on http://{args.host}:{args.port}/ui",
        file=sys.stderr,
    )
    print(
        "[gemma-stepping] Open the UI → sessions → start with runner ref 'gemma' "
        "and the captured trace id.",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
