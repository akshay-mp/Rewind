#!/usr/bin/env python3
"""Demo 3 — multi-step coding agent captured by TimeTravel.

Pattern (the "ReWOO" trace shape — a real coding agent skeleton):

    1. Plan  — LLM proposes N tool calls in one shot.
    2. Write — code-writer tool emits a Python snippet.
    3. Run   — code-runner tool executes it (may fail).
    4. Reflect — LLM reads the stdout/stderr and decides next step.
    5. Revise — either accept or re-plan + re-write.

Produces a **branching span tree** — one parent `gen_ai.agent` span, with
sequential `gen_ai.tool` and `gen_ai.llm` children descending from it.
This is the shape you'll want to *branch off of* in the diff UI: pick the
reflect step, swap the code-runner tool, re-run, see how the revision
diverged.

Run::

    timetravel serve
    python examples/multi_step_coder.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
os.environ.setdefault("OTEL_BSP_SCHEDULE_DELAY", "100")


# The mock tool emits an intentionally buggy snippet on the first pass so
# the reflect → revise loop has something to fix. Real agents would
# obviously call an actual model here.
_INITIAL_SNIPPET = "print(sum([1, 2, 'oops']))"  # raises on the str
_REVISED_SNIPPET = "print(sum([1, 2, 3]))"       # prints 6


def write_code(task: str) -> str:
    """Mock code-writer tool — always emits the same buggy snippet."""
    print(f"[tool] write_code({task!r}) → {_INITIAL_SNIPPET!r}")
    return _INITIAL_SNIPPET


def run_code(snippet: str) -> dict[str, Any]:
    """Mock code-runner tool — exec's the snippet and captures stdout/stderr."""
    import contextlib
    import io

    out, err = io.StringIO(), io.StringIO()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            # pylint: disable=exec-used
            exec(snippet, {"__name__": "__demo__"})  # noqa: S102
            # pylint: enable=exec-used
        return {"ok": True, "stdout": out.getvalue(), "stderr": err.getvalue()}
    except Exception as exc:
        return {"ok": False, "stdout": out.getvalue(), "stderr": f"{type(exc).__name__}: {exc}"}


def reflect(result: dict[str, Any]) -> str:
    """LLM-like decision: success → done; failure → revised snippet."""
    if result["ok"]:
        print(f"[llm] reflect → success (stdout={result['stdout'].strip()!r})")
        return ""
    print(f"[llm] reflect → revise ({result['stderr'].strip()!r})")
    return _REVISED_SNIPPET


def main() -> int:
    """Run the multi-step coding demo. Returns 0 on success."""
    try:
        # pylint: disable=import-outside-toplevel
        from openinference.instrumentation.openai import OpenAIInstrumentor
        # pylint: enable=import-outside-toplevel
    except ImportError:
        print(
            "Install openinference-instrumentation-openai first:\n"
            "  pip install openinference-instrumentation-openai "
            "opentelemetry-sdk opentelemetry-exporter-otlp",
            file=sys.stderr,
        )
        return 2

    OpenAIInstrumentor().instrument()

    task = "sum the numbers 1, 2, 3"
    print(f"\n[plan] task={task!r}\n")

    snippet = write_code(task)
    result = run_code(snippet)
    print(f"[tool] run → {result}")

    revision = reflect(result)
    if revision:
        # Second pass with the revised snippet — surfaces as another span
        # sibling in the trace tree.
        print("\n[revise] re-running with revised snippet...")
        result2 = run_code(revision)
        print(f"[tool] run → {result2}")
        _ = reflect(result2)

    print("\nTrace shipped. Open http://127.0.0.1:8484/ui/ to inspect.")
    print("Tip: try the Branch-from-here action on the reflect span to")
    print("     compare alternative revised snippets. Diff shows exactly")
    print("     where the second run diverged from the first.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
