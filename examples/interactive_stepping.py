"""Phase 9 example — interactive step-through debugging of an agent.

This is the developer-facing worked example for the stepping feature. It
shows the three-line wiring pattern:

1. Write your agent as an ``async def`` runner that takes a
   :class:`~timetravel.replay.ReplaySession` and drives the agent to completion.
2. Register it with :func:`timetravel.stepping_api.register_runner`.
3. Start the server with ``timetravel ui`` and open the browser to step through.

The example agent here is deliberately minimal — two LLM calls back-to-back
via a stubbed OpenAI client, no real model required. In a real session you
would call ``await agent.run()`` inside the runner; the OpenAI/PydanticAI
interceptors built into TimeTravel would pause at each LLM call automatically.

Run it::

    # 1. Seed a trace to step through (or reuse one you already captured).
    timetravel replay <trace_id> --db ~/.timetravel/timetravel.db  # read-only, just loads it

    # 2. Start the stepping server with this runner registered.
    python examples/interactive_stepping.py --db ~/.timetravel/timetravel.db

    # 3. In another terminal, run the UI.
    timetravel ui --db ~/.timetravel/timetravel.db

    # 4. Open http://127.0.0.1:8484/ui, click "sessions", enter the trace id
    #    + runner ref ("example") and click "start session". The agent will
    #    pause at each LLM call; approve, edit, or stop from the UI.

The runner contract
-------------------
A runner is an ``async def`` accepting one positional arg: the
:class:`~timetravel.replay.ReplaySession` the server has already bound to the
active ContextVar. Inside the runner you typically:

* Call ``await agent.run()`` (or ``graph.ainvoke(...)`` etc.) — the
  interceptors pause at each LLM/tool call automatically.
* Use the session arg to read ``session.cursor``, ``session.branch_id`` if
  your agent needs branching-aware logic (most don't).

The runner must NOT call :func:`timetravel.replay.replay` itself — the server
has already opened the context. The runner just drives the agent; the
interceptors do the pausing.
"""

from __future__ import annotations

import argparse
import sys

from agent_timetravel.replay import ReplaySession
from agent_timetravel.stepping_api import register_runner

# ----------------------------------------------------------------------
# A minimal stub agent — two LLM calls, no real model required.
# ----------------------------------------------------------------------
# In a real session this would be your framework's agent: a LangGraph graph,
# a PydanticAI Agent, a CrewAI crew, etc. The interceptors built into
# TimeTravel (``openai_intercept.patch`` for the OpenAI SDK, or the per-framework
# adapters) pause at each LLM call automatically when a stepping channel is
# attached. Here we simulate two calls directly through the gate so the
# example runs without any framework installed.


async def _example_runner(session: ReplaySession) -> None:
    """Drive a two-step agent, pausing at each LLM call via the gate.

    Uses :func:`timetravel.stepping.gate_async` directly so the example is
    framework-free. In a real agent you wouldn't call this — the
    OpenAI/PydanticAI interceptor would, on every ``chat.completions.create``
    or ``model.request`` call.
    """
    # pylint: disable=import-outside-toplevel
    from agent_timetravel.replay import active_session
    from agent_timetravel.stepping import DecisionKind, Step, StepKind, gate_async

    sess = active_session()
    if sess is None:  # pragma: no cover — the server always binds one
        raise RuntimeError("no active replay session; was the runner called outside replay()?")

    # Step 1: "plan".
    step1 = Step(
        kind=StepKind.LLM,
        payload={
            "model": "example-model",
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Plan a 3-step approach to: deploy a web app."},
            ],
        },
        cursor=sess.cursor,
    )
    decision1 = await gate_async(sess, step1)
    # The developer's decision (APPROVE / EDIT / STOP / STEP_ONCE) has been
    # applied. In a real run the materialised response would be returned by
    # the interceptor; here we just proceed to the next step.
    if decision1 is not None and decision1.kind is DecisionKind.STOP:
        return

    # Step 2: "execute".
    step2 = Step(
        kind=StepKind.LLM,
        payload={
            "model": "example-model",
            "messages": [
                {"role": "user", "content": "Execute step 1 of the plan."},
            ],
        },
        cursor=sess.cursor,
    )
    await gate_async(sess, step2)


# ----------------------------------------------------------------------
# Server startup
# ----------------------------------------------------------------------
def main() -> int:
    """Register the example runner and start the stepping server."""
    register_runner("example", _example_runner)

    # Import here so ``register_runner`` runs first and is visible even on
    # ``--help``.
    # pylint: disable=import-outside-toplevel
    import uvicorn

    from agent_timetravel.receiver import create_app
    from agent_timetravel.storage import TraceStore

    parser = argparse.ArgumentParser(
        description="Interactive stepping example server.",
    )
    parser.add_argument(
        "--db",
        default=str(__import__("pathlib").Path.home() / ".timetravel" / "agent_timetravel.db"),
        help="Path to the timetravel SQLite DB (default: ~/.timetravel/timetravel.db).",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host (default: 127.0.0.1 — local-only).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=8484,
        help="Bind port (default: 8484).",
    )
    args = parser.parse_args()

    store = TraceStore(args.db)
    app = create_app(store)
    print(
        f"[example] runner 'example' registered. "
        f"Serving on http://{args.host}:{args.port}/ui",
        file=sys.stderr,
    )
    print(
        "[example] open the UI, click 'sessions', and start a session with "
        "runner ref 'example'.",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
