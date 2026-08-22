# Branching & Diff Walkthrough

This is the core debugging workflow TimeTravel unlocks. You captured a trace
of your agent; something about its behaviour surprised you; now you want
to ask "what would have happened if the prompt were phrased differently?"

The branch + diff loop lets you answer that **without re-running the
entire agent from scratch, without losing the original recording, and
without leaving the Web UI**.

## The mental model

A **Trace** is a tree of spans, captured once. A **Branch** shares the
first N spans with its parent trace and diverges from span N+1 onwards.
Branches form a tree, and **every span in a branch is real** — either
served from the frozen recording, or captured from a live model call.

The TimeTravel Replay Engine has three modes:

| Mode | Behaviour | Use when |
|---|---|---|
| **Frozen** | Every call ≤ cursor returns the recorded response. Zero egress, deterministic. | Stepping backward through a recorded trace to inspect any span's state. |
| **Branch** | Calls ≤ cursor return fixtures; call at cursor + 1 goes live and is captured under a new branch id. | "What if the prompt were different from this span onwards?" |
| **Full re-run** | Re-execute every call live. | "Is this run reproducible end-to-end?" (Sanity check — by definition non-deterministic.) |

Branch is the default in the UI.

## Walkthrough: branching a tool-caller

Use [`examples/tool_caller.py`](../examples/tool_caller.py) as the seed —
a 2 LLM + 1 tool span trace. Goal: change the user prompt and see the
second LLM call diverge.

### Step 1 — Capture and open

```bash
timetravel serve &
timetravel ui &
python examples/tool_caller.py
```

Click the trace in the Timeline. You'll see three spans:

1. `gen_ai.llm` — first LLM call (decides to call the tool).
2. `gen_ai.tool` — tool execution.
3. `gen_ai.llm` — second LLM call (summarises the tool result).

### Step 2 — Branch

Click the *first LLM span*. The inspector panel has a **Branch from here**
button in the top-right.

Enter a new user message — e.g. `"What's the weather in Tokyo?"` — and
click **Run live**.

The UI drops into a spinner: the Replay Engine creates a new branch
under the trace's tree, runs the LLM call live (against your model of
choice), and captures the new response as span 1' on the new branch.

### Step 3 — Diff

Click **Diff against original**. Two columns appear:

```
Branch A (original)             │   Branch B (your edit)
─────────────────────────────────┼─────────────────────────────────
[user] What's the weather in     │   [user] What's the weather in
       Lisbon?                   │          Tokyo?
[assistant] (calls tool.city=    │   [assistant] (calls tool.city=
             Lisbon)             │              Tokyo)
[tool]   62°F and sunny in       │   [tool]   38°F and rainy in
         Lisbon                  │            Tokyo
[assistant] It's 62°F and sunny  │   [assistant] It's 38°F and rainy
             in Lisbon today.    │            in Tokyo today.
```

The **first divergent span** is highlighted; TimeTravel also surface any
"hidden" divergences — e.g. if both branches happen to produce the
*same* textual answer but the model is configured differently
(`q4_K_M` vs `q8_0`), the `quant_diverges` flag from Phase 7 lights up
a yellow 🔶 badge.

### Step 4 — Walk the algorithm

What actually happens behind the scenes when you click "Run live":

1. **Clone**: the first N spans (up to and including the cursor LLM span)
   are copied to a new branch id. No new model calls — pure SQLite write.
2. **Switch mode**: the active `ReplaySession` for branch B is set to
   `BRANCH`. Calls to the agent from this point onward are routed
   through TimeTravel.
3. **Serve the first N from fixtures**: any call whose hash matches a
   recorded span at index ≤ cursor returns the cached response. No HTTP
   egress.
4. **Forward the (N+1)th call live**: the agent's prompt-change
   generates a new `messages_hash`; TimeTravel doesn't recognise it; the
   call is forwarded to your real model; the response is captured into
   the branch under a new span id.

For the full mechanics (including how streaming responses are
fixture-matched) see [`docs/phases/phase-3.md`](./phases/phase-3.md).

## Walkthrough: diffing two pre-recorded branches

You don't need to branch interactively. If you already have two traces
(perhaps captured on two different machines with different model
quantisation), you can diff them directly:

```bash
timetravel diff TRACE_A TRACE_B            # CLI form
```

Or in the UI: select two traces in the trace list → **Diff**. The diff
algorithm is the same, but divergences start from span 0 rather than
from a cursor.

## Quant divergence (Phase 7)

When the **same base model** appears with different quant suffixes on
two branches (e.g. `qwen2.5:7b-q4_K_M` on one, `qwen2.5:7b-q8_0` on
another), the diff surfaces a `quant_diverges` badge on the diverging
span. This catches the silent quality regression where lower-VRAM
hardware downgrades a run without changing any prompts.

Run `timetravel enrich TRACE --sample-vram` to additionally capture one-shot
VRAM samples per LLM span — those appear next to each span in the UI and
are surfaceable in the diff.

## Common workflows

| "I want to…" | Action |
|---|---|
| See what an agent would have done with a different prompt | Branch from the relevant LLM span, edit the message, Run live. |
| Find the first divergence between two captured runs | Open both traces, click Diff. |
| Verify a model swap didn't break anything | Diff before/after; check `quant_diverges` flag. |
| Roll back a side-effecting agent | Use `timetravel.checkpoint()` in your agent + Phase 4's rollback handler. |
| Bulk-test 50 prompt variants | See [`docs/phases/phase-5.5.md`](./phases/phase-5.5.md) for the eval harness. |

## What you cannot do with branching

- **You can't replay a tool's *live side-effect*.** Frozen replay serves
  the recorded tool result; the actual filesystem / API / database
  call is **not** re-executed. This is deliberate — replay is for
  inspection, not for re-running side effects. For side-effecting
  agents, see Phase 4's `timetravel.checkpoint()`.
- **You can't branch a streaming generation mid-token.** Branching
  granularity is per-span, not per-chunk. (Streaming fixtures are
  chunk-level inside a span, but the branch point itself is always a
  span boundary.)
- **You can't branch across traces.** A branch always shares its
  parent's trace id. To compare traces, use Diff, not Branch.
