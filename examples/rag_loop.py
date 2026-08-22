#!/usr/bin/env python3
"""Demo 2 — retrieval-augmented chat loop captured by TimeTravel.

Pattern:
    user asks
        └─▶ retrieve(top-k docs from a tiny in-memory store)
        └─▶ LLM answers using retrieved context
    user follows up
        └─▶ retrieve (changed context based on follow-up)
        └─▶ LLM answers using new context

Produces a **sequential span tree** — each turn of conversation emits one
retrieval `gen_ai.tool` span + one `gen_ai.llm` span, child of a single
parent `gen_ai.agent` span. Useful for diffing two retrieval strategies:
branch from the retrieved-docs span, swap the retriever, re-run.

Run::

    agent-timetravel serve
    python examples/rag_loop.py
"""

from __future__ import annotations

import os
import sys
from typing import Any

os.environ.setdefault("OTEL_EXPORTER_OTLP_ENDPOINT", "http://127.0.0.1:4318")
os.environ.setdefault("OTEL_BSP_SCHEDULE_DELAY", "100")


# Tiny in-memory "knowledge base" — enough to exercise the retriever + the
# LLM context-builder with something more interesting than fixed strings.
KNOWLEDGE_BASE: list[dict[str, str]] = [
    {"id": "doc-1", "text": "TimeTravel is time-travel debugging for AI agents."},
    {"id": "doc-2", "text": "TimeTravel ingests OTLP/HTTP traces via a local receiver."},
    {"id": "doc-3", "text": "Branching a trace lets you re-run with a changed prompt."},
    {"id": "doc-4", "text": "Diffing two branches shows exactly where they diverged."},
    {"id": "doc-5", "text": "Phase 5.5 added parallel eval suite scoring."},
]


def retrieve(query: str, *, top_k: int = 2) -> list[dict[str, str]]:
    """Naive lexical retrieval — no embeddings, just substring match.

    Returns top_k docs whose text contains any word from the query. Enough
    to register as a real retrieval tool span; not a real RAG retriever.
    """
    query_terms = {w.lower().strip("?.,") for w in query.split()}
    scored: list[tuple[int, dict[str, str]]] = []
    for doc in KNOWLEDGE_BASE:
        text = doc["text"].lower()
        overlap = sum(1 for term in query_terms if term in text)
        if overlap:
            scored.append((overlap, doc))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    return [doc for _, doc in scored[:top_k]]


def fake_llm_answer(query: str, context: list[dict[str, str]]) -> str:
    """Mock answer so the demo runs without an API key.

    In production the OpenInference-instrumented ``openai`` call would
    replace this; here we just join the snippets back as the "answer".
    """
    snippets = " ".join(d["text"] for d in context)
    return f"Based on: {snippets}. Answer: {query.strip()} → see context."


def run_turn(
    conversation: list[dict[str, Any]],
    user_msg: str,
) -> None:
    """One turn of the RAG loop: retrieve → LLM → append to conversation."""
    docs = retrieve(user_msg)
    print(f"[tool] retrieve → {[d['id'] for d in docs]}")
    answer = fake_llm_answer(user_msg, docs)
    print(f"[assistant] {answer}\n")
    conversation.append({"role": "user", "content": user_msg})
    conversation.append({"role": "assistant", "content": answer})


def main() -> int:
    """Run the demo. Returns 0 on success."""
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

    conversation: list[dict[str, Any]] = []
    run_turn(conversation, "What is TimeTravel?")
    run_turn(conversation, "How do I branch a trace?")

    print("Trace shipped. Open http://127.0.0.1:8484/ui/ to inspect.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
