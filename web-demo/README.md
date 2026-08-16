# Rewind × Deep Research — web demo

The polished, browser-based debugger for Rewind. A three-panel UI:

- **Left** — span timeline (numbered nodes, role badges, latency, live/cached).
- **Right** — span detail (system prompt + user input + output) with a
  **"Branch from here"** action and an editable prompt box.
- **Modal** — side-by-side **branch diff** with token-level add/remove marks
  and the first-divergence index flagged.

It runs a flattened 8-step deep-research agent (clarify → brief → supervisor →
researcher × 2 → complete → final report) against a local model via the
OpenAI-compatible API, and mirrors each span into Rewind's OTLP receiver so
the Python engine sees the same run.

> **For the full step-by-step run guide, see
> [`../docs/demo-run.md`](../docs/demo-run.md).**

## Quick start

```bash
cd rewind/web-demo/

# Point at your local model server (Unsloth / Ollama / OpenAI-compatible).
cat > .env.local <<'EOF'
OPENAI_BASE_URL=http://localhost:8888/v1
OPENAI_API_KEY=<your-key>
REWIND_MODEL=unsloth/Qwen3.6-27B-MTP-GGUF
OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318
EOF

bun install
nohup ./node_modules/.bin/next dev -H 127.0.0.1 -p 3000 > /tmp/web-demo-dev.log 2>&1 &
disown
```

Open http://localhost:3000.

> **Local-only warning:** Do not expose this demo through a reverse proxy. The
> prompt-edit branch route intentionally accepts developer-authored system
> prompts and has no multi-user authentication.

## Layout

```
src/
  app/
    page.tsx                       Top bar + resizable 3-panel layout
    api/rewind/run/route.ts        POST — capture a fresh trace
    api/rewind/branch/route.ts     POST — fork + run the divergent tail live
  lib/
    deep-research/agent.ts         The 8-step agent (OpenAI SDK → your model)
    deep-research/prompts.ts       Per-span prompt templates + suggested fixes
    rewind/store.ts                Zustand client state (traces, cursor, mode)
    rewind/diff.ts                 Word-level + span-level diff engine
    rewind/types.ts                Span / Trace / BranchDiff types
  components/
    rewind/span-timeline.tsx       The timeline rail + span rows
    rewind/span-detail.tsx         Span inspector + branch-mode prompt editor
    rewind/diff-view.tsx           Side-by-side branch diff
    ui/                            shadcn/ui primitives
public/
  rewind.svg                       The Rewind mark (favicon + in-app logo)
```

## Notes

- The agent calls `openai.ChatCompletion.create` against `OPENAI_BASE_URL`.
  Switch backends (Unsloth ↔ Ollama ↔ OpenAI) by changing env vars only.
- Each live span is mirrored to Rewind's receiver as a `gen_ai.llm` span so
  `rewind ui` and the Python engine see the same data. The mirror is
  best-effort: if the receiver is down, this UI still works.
- Qwen3.x emits `<think>` blocks by default; the agent disables thinking via
  `chat_template_kwargs.enable_thinking=false`.
