# Interactive Workbench Verification

This guide covers the local, step-by-step debugger at `/ui/`. It is separate
from [`e2e-ui-testing.md`](e2e-ui-testing.md), which covers the original
read-only trace timeline.

## Start The Demo

From the repository root, start the seeded interactive backend:

```bash
./.venv/bin/python examples/start_deep_research_stepping.py \
  --db /tmp/rewind-demo.db \
  --host 127.0.0.1 \
  --port 8484
```

Start the Vite UI in another terminal if it is not already running:

```bash
cd web
npm run dev -- --host 127.0.0.1 --port 5174
```

Open <http://127.0.0.1:5174/ui/> and click **Start Agent**.

## Acceptance Walkthrough

1. **Start and review an LLM call.** The first call should execute, then show
   a collapsed **Thinking** section, a separate **Final response**, usage,
   and cost. **Next Step** controls appear only after the response is ready.
2. **Continue the workflow.** Approve the response and confirm the next
   intercepted call appears in the execution path. `PROCEED` outputs should
   advance automatically to the next substantive step.
3. **Inspect a saved step.** Click any completed execution-path item. The
   saved response should open without another model or tool call.
4. **Rewind and continue.** Use **Step Back / Rewind**, move forward through
   saved history, and use **Continue from here**. A successor run is created
   only when execution is actually resumed.
5. **Edit a prompt.** Choose **Edit Prompt & Run**, change the messages or
   model, and run the variant. The prompt-version list should retain the
   baseline and edited variant, including parameters, reasoning, final text,
   usage, pricing, assertions, and review state.
6. **Compare variants.** Select two completed variants from the same
   checkpoint and open the comparison matrix. Verify prompt and response
   diffs, token/cost/latency deltas, assertion results, and review verdicts.
   Reasoning must be separate from the displayed final response.
7. **Verify pricing.** Set an output price such as `2.5` dollars per million
   tokens. Step and session totals should update without changing token counts.
   Set all prices to `0` for a local model.
8. **Verify browser refresh.** Refresh while an LLM step is paused after its
   response. The same step, response, thinking section, usage, cost, and
   review state should return without another LLM call. The server retains the
   in-flight `paused -> dispatching -> step_completed` snapshot for the new
   SSE connection.
9. **Verify tool safety.** Pause at a tool and test **Run Tool**, **Mock**,
   **Skip**, and **Reject**. Mock, skip, and reject must not invoke the live
   tool. Use the integration suite to verify exactly-once behavior.

### Synchronous OpenAI Calls

Synchronous OpenAI calls can be stepped through when they run in a worker
thread, for example with `await asyncio.to_thread(sync_agent_call)`. A sync
call cannot wait for browser approval while it is executing on the same
asyncio event loop that serves the SSE connection; Rewind fails fast with a
clear error in that case. Prefer the async OpenAI client in an async runner,
or move the synchronous call to a worker thread. Sync and async responses both
publish the response usage used by the token and cost panels.

## Automated Checks

Run the focused backend contract tests:

```bash
./.venv/bin/pytest -q tests/test_stepping_api.py tests/test_prompt_versions.py
./.venv/bin/python -m mypy src/rewind
cd web && npm run typecheck && npm run build
```

The reconnect contract is pinned by
`TestSSEApprovalChannel.test_replay_snapshot_preserves_llm_review_after_refresh`.
The browser walkthrough is retained as a manual check because the repository
does not currently ship a browser-test runner dependency.

## Troubleshooting

- Use one active workbench tab during a stepping run. Multiple tabs sharing
  one browser profile can reconnect to the same session and compete for SSE
  decisions.
- If the UI shows `0 steps` after a backend restart, start a new session.
  Live sessions are process-local; durable prompt versions and saved cases
  remain in SQLite, but an in-memory runner cannot survive process shutdown.
- A local endpoint that does not report usage shows estimated token counts and
  cost. The workbench labels these as local estimates.
