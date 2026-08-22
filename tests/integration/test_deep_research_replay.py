"""Integration test — replaying an Open Deep Research-shaped agent trace.

Open Deep Research (``open_deep_research``) is the canonical LangGraph
deep-research agent: a multi-node ``StateGraph`` (clarify → write_brief →
research_supervisor[parallel researchers] → final_report). Every node calls
the same LangChain ``BaseChatModel`` via ``.ainvoke()``; under the hood that
becomes an ``openai...Completions.create`` call.

This test proves TimeTravel's capture → replay → branch → diff loop works against
that *shape* of agent — a multi-LLM-span trace with no tool spans (the
``SearchAPI.NONE`` offline configuration) — **without** requiring the real
``open-deep-research`` package, a model backend, or network access. It seeds a
synthetic 4-LLM-span trace in-process (mirroring ODR's clarify → brief →
researcher → final_report sequence) and drives it through the three replay
phases that ``examples/deep_research.py`` runs live:

1. **FROZEN** — every span served from cache, zero live calls.
2. **BRANCH** — fork at a researcher span, divergent call forwards live and is
   captured under a fresh ``branch_id``.
3. **span_diff / message_diff** — the branch's tail is diffed against the seed
   and the first divergence is flagged.

The pattern mirrors ``tests/integration/test_replay_e2e.py``: a fake
``openai.resources.chat.completions`` module is installed via ``sys.modules``
so the patched ``create`` consults the active ``ReplaySession`` with no HTTP.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_timetravel.diff import message_diff, span_diff
from agent_timetravel.enums import ReplayMode, SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.openai_intercept import patch
from agent_timetravel.replay import replay as replay_ctx
from agent_timetravel.storage import TraceStore

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Fake OpenAI module — keeps the integration test free of HTTP.
# ----------------------------------------------------------------------
@contextmanager
def _fake_openai_module() -> Iterator[dict[str, Any]]:
    """Install a deterministic ``openai.resources.chat.completions`` stub.

    Identical to the stub in ``test_replay_e2e.py``: the patched ``create``
    records every call so we can assert that *no* outbound call happens in
    frozen mode. A live forward returns the ``LIVE_FROM_STUB`` marker so we can
    distinguish served-from-cache vs forwarded-live in branch mode.
    """
    saved = {
        key: sys.modules.get(key)
        for key in (
            "openai",
            "openai.resources",
            "openai.resources.chat",
            "openai.resources.chat.completions",
            "openai.types",
            "openai.types.chat",
        )
    }
    try:
        class Completions:
            """Stubbed sync completions endpoint."""

            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            def create(self, *args: Any, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                return {
                    "id": "chatcmpl-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": kwargs.get("model", "stub"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": "LIVE_FROM_STUB",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": 1,
                        "completion_tokens": 1,
                        "total_tokens": 2,
                    },
                }

        class AsyncCompletions:
            """Stubbed async completions endpoint."""

            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def create(self, *args: Any, **kwargs: Any) -> Any:
                self.calls.append(kwargs)
                return Completions().create(*args, **kwargs)

        completions_mod = types.ModuleType("openai.resources.chat.completions")
        completions_mod.Completions = Completions  # type: ignore[attr-defined]
        completions_mod.AsyncCompletions = AsyncCompletions  # type: ignore[attr-defined]

        chat_mod = types.ModuleType("openai.resources.chat")
        chat_mod.completions = completions_mod  # type: ignore[attr-defined]
        resources_mod = types.ModuleType("openai.resources")
        resources_mod.chat = chat_mod  # type: ignore[attr-defined]
        openai_mod = types.ModuleType("openai")
        openai_mod.resources = resources_mod  # type: ignore[attr-defined]

        sys.modules["openai"] = openai_mod
        sys.modules["openai.resources"] = resources_mod
        sys.modules["openai.resources.chat"] = chat_mod
        sys.modules["openai.resources.chat.completions"] = completions_mod
        sys.modules.pop("openai.types", None)
        sys.modules.pop("openai.types.chat", None)

        yield {"Completions": Completions, "AsyncCompletions": AsyncCompletions}
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


# ----------------------------------------------------------------------
# Seed-trace construction — an Open-Deep-Research-shaped 4-LLM-span trace.
# ----------------------------------------------------------------------
_TRACE_ID = "fedcba9876543210fedcba9876543210"
_MODEL = "unsloth/Llama-3.1-8B"


def _llm_span(
    *,
    span_id: str,
    messages: list[dict[str, str]],
    model: str = _MODEL,
    response_content: str = "cached answer",
) -> Span:
    """Build an LLM span whose ``messages_hash`` matches the live call."""
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name=model,
        prompt_tokens=20,
        completion_tokens=8,
        total_tokens=28,
        messages_hash=hash_payload(messages),
        raw_attributes={
            "gen_ai.request.model": model,
            "gen_ai.response.model": model,
            "gen_ai.response": {
                "id": f"chatcmpl-{span_id}",
                "object": "chat.completion",
                "created": 0,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "message": {
                            "role": "assistant",
                            "content": response_content,
                        },
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": 20,
                    "completion_tokens": 8,
                    "total_tokens": 28,
                },
            },
        },
    )


def _seed_deep_research_trace(store: TraceStore) -> dict[str, Any]:
    """Seed a 4-span ODR-shaped trace.

    The span sequence mirrors Open Deep Research's node order when run with
    ``SearchAPI.NONE`` (no tool/search spans, pure LLM nodes):

        span 0 — clarify_with_user     (LLM, structured output: no clarification)
        span 1 — write_research_brief  (LLM, structured output: research brief)
        span 2 — researcher            (LLM, the actual research step)
        span 3 — final_report_generation (LLM, synthesised report)
    """
    clarify_msgs = [{"role": "user", "content": "Research time-travel debugging."}]
    brief_msgs = [*clarify_msgs, {"role": "assistant", "content": "no clarification needed"}]
    researcher_msgs = [
        *brief_msgs,
        {"role": "assistant", "content": "brief: survey OTel-based agent debuggers"},
    ]
    report_msgs = [
        *researcher_msgs,
        {"role": "assistant", "content": "found 3 relevant projects"},
    ]

    spans = [
        _llm_span(
            span_id="1" * 16,
            messages=clarify_msgs,
            response_content="no clarification needed",
        ),
        _llm_span(
            span_id="2" * 16,
            messages=brief_msgs,
            response_content="brief: survey OTel-based agent debuggers",
        ),
        _llm_span(
            span_id="3" * 16,
            messages=researcher_msgs,
            response_content="found 3 relevant projects",
        ),
        _llm_span(
            span_id="4" * 16,
            messages=report_msgs,
            response_content="TimeTravel, AgentLens, and Chronos Agent are the leading tools.",
        ),
    ]
    store.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for s in spans:
        store.insert_span(s)
    return {
        "trace_id": _TRACE_ID,
        "clarify_messages": clarify_msgs,
        "brief_messages": brief_msgs,
        "researcher_messages": researcher_msgs,
        "report_messages": report_msgs,
        "expected_report": "TimeTravel, AgentLens, and Chronos Agent are the leading tools.",
    }


def _content(resp: Any) -> str:
    """Extract the assistant text from a stub or materialised response."""
    body = resp.model_dump() if hasattr(resp, "model_dump") else resp
    return body["choices"][0]["message"]["content"]


# ----------------------------------------------------------------------
# A fake "ODR agent" — calls completions.create for each of its 4 nodes.
# ----------------------------------------------------------------------
def _run_deep_research_agent(completions: Any, seed: dict[str, Any]) -> str:
    """Walk the 4 ODR nodes in order, calling ``completions.create`` each time.

    The messages lists are built to match the recorded ``messages_hash`` so
    frozen replay serves the cached response; a divergent topic in branch mode
    changes the hash and forces a live forward.
    """
    completions.create(model=_MODEL, messages=seed["clarify_messages"])
    completions.create(model=_MODEL, messages=seed["brief_messages"])
    completions.create(model=_MODEL, messages=seed["researcher_messages"])
    r3 = completions.create(model=_MODEL, messages=seed["report_messages"])
    return _content(r3)


# ----------------------------------------------------------------------
# The integration contracts.
# ----------------------------------------------------------------------
def test_frozen_replay_serves_all_odr_nodes_offline(tmp_path: Path) -> None:
    """FROZEN replay of the 4-node ODR trace makes zero live calls.

    Every node (clarify → brief → researcher → final_report) is served from
    the recorded fixture; the final report matches the seed verbatim.
    """
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_deep_research_trace(store)

    with _fake_openai_module() as fake:
        completions = fake["Completions"]()
        with patch(), replay_ctx(store, seed["trace_id"], mode=ReplayMode.FROZEN):
            report = _run_deep_research_agent(completions, seed)

        assert report == seed["expected_report"]
        # Zero live calls — the whole multi-node graph replayed offline.
        assert completions.calls == []


def test_branch_at_researcher_node_forwards_tail_live(tmp_path: Path) -> None:
    """Branching at the researcher node (index 2) re-runs the tail live.

    ``branch_at=2`` positions the cursor at span 2: the inherited prefix
    (clarify, brief) is *already consumed*, so the agent resumes execution at
    the researcher node. A divergent researcher call (changed messages) misses
    the fixture at the cursor, forwards live (returns ``LIVE_FROM_STUB``), and
    a new LLM span is captured under the fork's ``branch_id``.
    """
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_deep_research_trace(store)

    with _fake_openai_module() as fake:
        completions = fake["Completions"]()

        # Divergent researcher messages — different content → different hash.
        divergent_researcher = [
            {"role": "user", "content": "Research local-model eval harnesses."},
            {"role": "assistant", "content": "no clarification needed"},
            {"role": "assistant", "content": "brief: survey local-first agent evaluators"},
        ]
        divergent_report = [
            *divergent_researcher,
            {"role": "assistant", "content": "found 2 relevant projects"},
        ]

        with patch(), replay_ctx(
            store,
            seed["trace_id"],
            mode=ReplayMode.BRANCH,
            branch_at=2,  # prefix [clarify, brief] consumed; resume at researcher
        ) as session:
            # Resume at the researcher node — two divergent live calls.
            r2 = completions.create(model=_MODEL, messages=divergent_researcher)
            r3 = completions.create(model=_MODEL, messages=divergent_report)
            branch_id = session.branch_id

        # Both divergent tail calls forwarded live.
        assert _content(r2) == "LIVE_FROM_STUB"
        assert _content(r3) == "LIVE_FROM_STUB"
        assert len(completions.calls) == 2

        # New spans persisted under the fork's branch_id. The union query
        # (root + branch) returns the inherited prefix too, so filter by the
        # divergent messages hashes to isolate the branch's own new spans.
        branch_spans = store.get_spans(seed["trace_id"], branch_id=branch_id)
        divergent_hashes = {
            hash_payload(divergent_researcher),
            hash_payload(divergent_report),
        }
        captured = [
            s
            for s in branch_spans
            if s.kind == SpanKind.LLM and s.messages_hash in divergent_hashes
        ]
        assert len(captured) == 2


def test_span_diff_flags_researcher_divergence(tmp_path: Path) -> None:
    """``span_diff`` marks the researcher node (index 2) as first divergence.

    The branch's *own* timeline = inherited prefix [clarify, brief] + the
    live-captured tail [divergent researcher, divergent report]. We diff the
    seed timeline against that reconstructed branch timeline (not the union
    query, which would repeat the shared prefix).
    """
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_deep_research_trace(store)
    seed_spans = store.get_spans(seed["trace_id"])

    with _fake_openai_module() as fake:
        completions = fake["Completions"]()
        divergent_researcher = [
            {"role": "user", "content": "Research local-model eval harnesses."},
            {"role": "assistant", "content": "no clarification needed"},
            {"role": "assistant", "content": "brief: survey local-first agent evaluators"},
        ]
        divergent_report = [
            *divergent_researcher,
            {"role": "assistant", "content": "found 2 relevant projects"},
        ]

        with patch(), replay_ctx(
            store,
            seed["trace_id"],
            mode=ReplayMode.BRANCH,
            branch_at=2,
        ) as session:
            completions.create(model=_MODEL, messages=divergent_researcher)
            completions.create(model=_MODEL, messages=divergent_report)
            branch_id = session.branch_id

        # Reconstruct the branch timeline: inherited prefix + new tail.
        branch_spans = store.get_spans(seed["trace_id"], branch_id=branch_id)
        divergent_hashes = {
            hash_payload(divergent_researcher),
            hash_payload(divergent_report),
        }
        new_tail = [
            s for s in branch_spans
            if s.kind == SpanKind.LLM and s.messages_hash in divergent_hashes
        ]
        branch_timeline = seed_spans[:2] + new_tail

        diff = span_diff(seed_spans, branch_timeline)
        # Clarify (idx 0) and brief (idx 1) match; researcher (idx 2) diverges.
        assert diff.first_divergence_index == 2
        assert not diff.identical


def test_message_diff_highlights_diverged_report(tmp_path: Path) -> None:
    """``message_diff`` flags added/removed tokens in the diverged report."""
    left = "TimeTravel, AgentLens, and Chronos Agent are the leading tools."
    right = "bench-loop and tma1 are the local-first eval options."
    md = message_diff(left, right)
    assert md.added_tokens > 0
    assert md.removed_tokens > 0
    # At least one fragment is the common prefix up to "TimeTravel" vs "bench-loop".
    kinds = {f.kind for f in md.fragments}
    assert "removed" in kinds or "changed" in kinds
    assert "added" in kinds or "changed" in kinds
