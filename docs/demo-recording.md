# Recording-ready demo checklist

This is a production checklist for the 2:40 LinkedIn demo. It is not video
capture, editing, or caption-burning tooling implemented by TimeTravel.

## Before recording

- Start with a clean demo DB, for example `/tmp/timetravel-demo.db`, so the session
  opens fresh and saved navigation is easy to follow.
- Put `OPENAI_BASE_URL`, `OPENAI_API_KEY`, and `TIMETRAVEL_MODEL` in the shell
  environment or local `.env`. Keep the key out of source, terminal captures,
  screenshots, logs, and the final video.
- Prewarm the local OpenAI-compatible Gemma/Unsloth model at
  `http://127.0.0.1:8888/v1` and confirm `/v1/models` responds before opening
  the workbench.
- Prepare a deterministic fallback for long generations so the recording fits
  the time budget. This is demo-runner preparation, not an implemented TimeTravel
  recording feature.

## Suggested 2:40 sequence

1. Start a fresh session and show a substantive call pausing after its final
   response, with **Thinking** separate from **Final response** and the
   token/cost/latency/context panels visible.
2. Let `PROCEED` auto-advance, then open a checkpoint and navigate saved steps
   backward and forward without another model or tool call.
3. Continue from the saved checkpoint to show the successor run.
4. Edit a prompt, run a variant, compare it with the baseline, and show the
   assertion and review state where those controls are available.
5. Save a regression case and show the saved-session result.
6. Burn captions as a separate post-production step; do not expose credentials
   while capturing the terminal or browser.

For exact startup commands and the full acceptance walkthrough, see
[`interactive-workbench-testing.md`](interactive-workbench-testing.md).
