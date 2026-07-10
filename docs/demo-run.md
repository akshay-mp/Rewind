# Demo Run — Rewind × Deep Research (live, with Unsloth)

> The end-to-end live demo: capture a real deep-research agent run, branch it
> from any span, and watch the divergent tail go live — all against a local
> model, all in the polished three-panel UI.

This is the demo to **show people**. It runs the full capture → branch → diff
loop on a real multi-node agent (8 LLM calls) against a local Qwen3.6 model
served by Unsloth Studio, in the browser.

---

## 0. What you need

| Component | Purpose | How to check |
|---|---|---|
| **Unsloth Studio** serving a model | The LLM backend | `curl localhost:8888/v1/models` → lists a model |
| **Rewind receiver** (`rewind serve`) | Ingests spans into SQLite | `curl localhost:4318/healthz` → `200` |
| **The web-demo** (`rewind/web-demo/`) | The polished Next.js UI | `curl localhost:3000` → `200` |

All three run on your machine. Nothing leaves localhost.

---

## 1. Start the model server (Unsloth Studio)

Load a model and start the API server. For this demo we use Qwen3.6:

```bash
unsloth studio run   # prints an API key + serves on :8888
```

Confirm it's up and note the **API key** it prints:

```bash
curl http://localhost:8888/v1/models \
  -H "Authorization: Bearer <your-unsloth-api-key>"
# → {"data":[{"id":"unsloth/Qwen3.6-27B-MTP-GGUF", ...}]}
```

---

## 2. Start the Rewind receiver

Captures every LLM span into `~/.rewind/rewind.db` (the Python engine's store):

```bash
# from rewind/
source /Users/akshaymp/Projects/Agentic_AI/.venv/bin/activate
rewind serve --port 4318 --db /tmp/rewind-demo.db
```

Confirm: `curl localhost:4318/healthz` → `200`.

> The web-demo mirrors each span here too, so you can inspect the same run in
> both the polished Next.js UI **and** `rewind ui` (the Python timeline).

---

## 3. Configure + start the web-demo UI

```bash
cd rewind/web-demo/

# Point the agent at your Unsloth server.
cat > .env.local <<'EOF'
OPENAI_BASE_URL=http://localhost:8888/v1
OPENAI_API_KEY=<your-unsloth-api-key>
REWIND_MODEL=unsloth/Qwen3.6-27B-MTP-GGUF
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
EOF

# Install JS deps (first run only; ~1 min).
bun install

# Start the dev server (do NOT use `bun run dev` — its tee pipe breaks on
# long-running requests; invoke next directly).
nohup ./node_modules/.bin/next dev -p 3000 > /tmp/web-demo-dev.log 2>&1 &
disown
```

Open **http://localhost:3000** in your browser.

> **Works with Ollama too** — just change `OPENAI_BASE_URL=http://localhost:11434/v1`
> and `REWIND_MODEL=qwen3:32b`. No code changes.

---

## 4. Capture a trace (Phase A)

1. The query box is pre-filled with *"Compare RLHF vs DPO for aligning large
   language models, with citations."* (edit if you like).
2. Click **⚡ Capture trace**.
3. The agent runs 8 LLM calls (clarify → brief → supervisor → researcher × 2
   → complete → final report). On a 27B model this takes **~3-4 minutes**;
   a smaller model is faster.
4. The timeline fills in left-to-right as spans land. Each shows its role
   badge (Clarify / Brief / Supervisor / Researcher / Final report), latency,
   and a **live** tag.

When it finishes you'll see the full research report in the last span.

---

## 5. Step down through the recording (the "rewind")

Use the **◀ ▶** buttons (top bar) to walk the span timeline:

- **Step down ◀** — move to an earlier span (rewind).
- **Step up ▶** — move forward.

The right panel shows the **system prompt**, **user input**, and **output**
for the selected span — exactly what the model saw and said at that point.

---

## 6. Branch from a span (Phase B — the headline feature)

1. Click any span (try the **Supervisor** span at index #3 — it decides what
   to research).
2. Click **↩ Branch from here**.
3. The panel switches to **branch mode**: you get an editable system prompt +
   one-click **"Suggested prompt fix"** options (e.g. *"Force 3 non-overlapping
   topics"*).
4. Apply a suggestion (or edit by hand), give the branch a label, click
   **▶ Run branch live**.

What happens:
- Spans **#1–#3 are FROZEN-replayed** from the recording — `cached` badge,
  zero LLM calls, zero cost.
- Spans **#3 onward run live** against Qwen3.6 with your edited prompt — the
  only part you pay for.

---

## 7. Compare branches (the diff)

1. With the branch selected, click **⎇ Compare** (top bar) to diff it against
   the original.
2. The **Diff view** opens: each span pair shows
   - **identical** (green) or **diverged** (fuchsia) badge
   - a **token-level output diff** — green additions, red strikethrough removals
   - the **system-prompt diff** if you changed it
3. The **first divergence index** is flagged — this is Rewind's headline metric:
   *"exactly which span first diverged."*

---

## 8. Inspect in the Python timeline (optional)

Because the web-demo mirrors spans to the Rewind receiver, the same run is
queryable by the Python engine:

```bash
rewind ui --port 8484 --db /tmp/rewind-demo.db
# → http://127.0.0.1:8484/ui/
```

Or query the DB directly:

```bash
sqlite3 /tmp/rewind-demo.db \
  "SELECT substr(trace_id,1,8), kind, substr(name,1,28) FROM spans ORDER BY start_time;"
```

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| `Capture trace` spins forever | The dev server's `tee` pipe broke. Restart with `./node_modules/.bin/next dev` directly (not `bun run dev`). |
| Spans don't appear in `rewind ui` | The web-demo's mirror is best-effort; confirm `rewind serve` is on 4318. The web-demo UI still works without it. |
| Model returns `<think>...` blocks | The agent disables thinking via `chat_template_kwargs`. If your server doesn't support it, switch to a non-thinking model. |
| `address already in use` on 3000/4318/8888 | Another instance is running. `pkill -fl "next dev\|rewind serve"` and retry. |
| `jsonschema-rs` build error on Python 3.14 | That's the `deep_research.py` (ODR) path, not this demo. The web-demo uses the OpenAI SDK directly and has no such dependency. |

---

## File map

| Path | Role |
|---|---|
| `rewind/web-demo/` | The Next.js UI (this demo's frontend). |
| `rewind/web-demo/src/lib/deep-research/agent.ts` | The 8-step agent; calls your model via the OpenAI SDK. |
| `rewind/web-demo/src/lib/rewind/{store,diff,types}.ts` | Client-side trace/branch state + diff engine. |
| `rewind/web-demo/src/app/api/rewind/{run,branch}/route.ts` | Capture + branch API endpoints. |
| `rewind/examples/deep_research_demo.py` | A **Python** equivalent of the same demo (CLI, three-phase) — useful for headless/CI. |
| `rewind/examples/deep_research.py` | A heavier variant using the full `open_deep_research` LangGraph graph (see its README for the Python-3.14 caveat). |
