"""Start the Rewind stepping server with the Deep Research Agent runner registered.

Usage:
    python examples/start_deep_research_stepping.py --db /tmp/rewind-demo.db

Then open http://127.0.0.1:8484/ui to step through the agent interactively step by step.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path
from typing import Any

import openai

from rewind import checkpoint, tool
from rewind.openai_intercept import patch
from rewind.replay import ReplaySession
from rewind.stepping_api import register_runner


def load_local_env() -> None:
    """Load simple KEY=VALUE entries from the repository's local .env file."""
    env_path = Path(__file__).resolve().parents[1] / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", maxsplit=1)
        os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


load_local_env()

GEMMA_BASE_URL = os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:8888/v1")
GEMMA_API_KEY = os.environ.get("OPENAI_API_KEY", "local")
GEMMA_MODEL = os.environ.get("REWIND_MODEL", "unsloth/gemma-4-12b-it-GGUF")
DEMO_TRACE_ID = "d3e0f00d1234567890abcdef12345678"

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


def _report_step(index: int, name: str, output: str) -> None:
    """Keep optional console diagnostics from terminating an agent run."""
    try:
        print(
            f"[runner] step {index + 1}/8 ({name}): {output[:60]}...",
            file=sys.stderr,
            flush=True,
        )
    except BrokenPipeError:
        # The stepping server can outlive the terminal that launched it.
        # Losing its diagnostic pipe must not turn a successful LLM response
        # into an errored debugging session.
        pass


def _final_response(content: str) -> str:
    """Remove an explicit provider reasoning block from a chat completion."""
    start = content.lower().find("<think>")
    end = content.lower().find("</think>")
    if start >= 0 and end > start:
        return content[end + len("</think>") :].strip()
    return content.strip()


def _seed_demo_trace(store: Any) -> str:
    """Ensure a clean demo database has a replay root for interactive mode."""
    if store.get_trace(DEMO_TRACE_ID) is None:
        from rewind.models import Trace

        store.upsert_trace(Trace(trace_id=DEMO_TRACE_ID))
    return DEMO_TRACE_ID


@tool("prepare_research_context")
def prepare_research_context(research_request: str) -> dict[str, Any]:
    """Build a deterministic local context packet for the research stages.

    The demo stays offline, while still exercising the same inspect/edit/run
    flow that a real search, database, or MCP tool would use.
    """
    words = [word.strip(".,:;()[]#*\"").lower() for word in research_request.split()]
    keywords = list(dict.fromkeys(word for word in words if len(word) > 4))[:8]
    return {
        "source": "local deterministic research-context tool",
        "keywords": keywords,
        "guidance": "Prioritize concrete trade-offs, assumptions, and cited claims.",
    }


async def deep_research_runner(session: ReplaySession) -> None:
    """Re-run the 8-step Deep Research Agent under interactive stepping against Gemma 4 via Unsloth Studio."""
    client = openai.AsyncOpenAI(base_url=GEMMA_BASE_URL, api_key=GEMMA_API_KEY)
    query = "Compare RLHF vs DPO for aligning large language models, with citations."
    outputs: dict[int, str] = {}

    with patch():
        for i, (name, system_prompt, user_template) in enumerate(PROMPTS):
            user_in = _fill(user_template, query, outputs)
            if name == "conduct_research":
                context_packet = await asyncio.to_thread(prepare_research_context, user_in)
                user_in = f"{user_in}\n\nLocal research context:\n{context_packet}"
            resp = await client.chat.completions.create(
                model=GEMMA_MODEL,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_in},
                ],
                max_tokens=1024,
                temperature=0.3,
            )
            msg = resp.choices[0].message
            raw_output = msg.content or getattr(msg, "reasoning_content", None) or "(empty)"
            out = _final_response(raw_output) or "(empty)"
            outputs[i] = out
            with checkpoint(
                f"after-{name}-{i + 1}",
                label=f"After {name.replace('_', ' ')}",
            ) as state:
                if not state.restored:
                    state.capture(
                        {
                            "completed_step": i + 1,
                            "step_name": name,
                            "original_query": query,
                            "outputs": outputs.copy(),
                        }
                    )
                # Restored checkpoints are stateful execution boundaries too.
                # Re-emit them so a restarted branch remains inspectable.
                channel = session.approval
                emit = getattr(channel, "emit", None)
                if emit is not None:
                    emit(
                        {
                            "type": "checkpoint",
                            "name": state.name,
                            "label": f"After {name.replace('_', ' ')}",
                            "cursor": session.cursor,
                            "keys": ["completed_step", "step_name", "original_query", "outputs"],
                        }
                    )
            _report_step(i, name, out)


def main() -> int:
    register_runner("deep-research", deep_research_runner)
    register_runner("gemma", deep_research_runner)

    import uvicorn
    from rewind.receiver import create_app
    from rewind.storage import TraceStore

    parser = argparse.ArgumentParser(description="Deep Research Stepping Server.")
    parser.add_argument("--db", default="/tmp/rewind-demo.db", help="SQLite DB path.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8484)
    args = parser.parse_args()

    store = TraceStore(args.db)
    demo_trace_id = _seed_demo_trace(store)
    app = create_app(store)
    print(
        f"[deep-research-stepping] runner 'deep-research' registered.\n"
        f"[deep-research-stepping] Serving on http://{args.host}:{args.port}/ui\n"
        f"[deep-research-stepping] Demo trace: {demo_trace_id}\n"
        f"[deep-research-stepping] Open the UI → click 'Start Agent' to step interactively.",
        file=sys.stderr,
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
