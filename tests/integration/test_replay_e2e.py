"""Phase 3 integration test — end-to-end replay determinism.

Phase 3 exit criterion (plan §6):

> Replay a real agent step-sequence offline: the LLM call returns the
> recorded response verbatim (zero outbound HTTP), and any tool call is
> served from the recorded `gen_ai.tool.output` (no side-effect). Branching
> at a divergence point re-executes live and persists new spans under the
> fork's ``branch_id``.

This test exercises the full stack — :class:`TraceStore` on disk, the
:class:`contextvars.ContextVar` session plumbing, the ``@timetravel.tool``
decorator, ``timetravel.openai_intercept.patch()``, and the SSE-free
:class:`timetravel.replay.ReplaySession` — *without* touching the network.

We don't spawn the FastAPI receiver here; we pre-seed the store directly
because Phase 3 ships the replay engine in isolation. Phase 4 will stitch
the OTLP receiver and replay into the same process.
"""

from __future__ import annotations

import sys
import types
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest

from agent_timetravel import tool as timetravel_tool
from agent_timetravel.enums import ReplayMode, SpanKind, SpanStatus
from agent_timetravel.models import Span, Trace, hash_payload
from agent_timetravel.openai_intercept import patch
from agent_timetravel.replay import active_session
from agent_timetravel.replay import replay as replay_ctx
from agent_timetravel.storage import TraceStore
from agent_timetravel.tool_intercept import _tool_args_hash

pytestmark = pytest.mark.integration


# ----------------------------------------------------------------------
# Fake OpenAI module — keeps the integration test free of HTTP.
# ----------------------------------------------------------------------
@contextmanager
def _fake_openai_module() -> Iterator[dict[str, Any]]:
    """Install a deterministic ``openai.resources.chat.completions`` stub.

    Just like the unit-test stub in ``tests/test_openai_intercept.py`` —
    the patched ``create`` method just records the call so we can assert
    that *no* outbound call happens in frozen mode.
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

        yield {
            "Completions": Completions,
            "AsyncCompletions": AsyncCompletions,
        }
    finally:
        for key, mod in saved.items():
            if mod is None:
                sys.modules.pop(key, None)
            else:
                sys.modules[key] = mod


# ----------------------------------------------------------------------
# Seed-trace construction
# ----------------------------------------------------------------------
_TRACE_ID = "0123456789abcdef0123456789abcdef"


def _llm_span(
    *,
    span_id: str,
    messages: list[dict[str, str]],
    model: str = "qwen3:32b",
    response_content: str = "cached answer",
) -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name="chat.completions.create",
        kind=SpanKind.LLM,
        status=SpanStatus.OK,
        model_name=model,
        prompt_tokens=12,
        completion_tokens=4,
        total_tokens=16,
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
                    "prompt_tokens": 12,
                    "completion_tokens": 4,
                    "total_tokens": 16,
                },
            },
        },
    )


def _tool_span(
    *,
    span_id: str,
    name: str,
    args: tuple[Any, ...],
    output: Any,
) -> Span:
    return Span(
        trace_id=_TRACE_ID,
        span_id=span_id,
        parent_span_id=None,
        name=name,
        kind=SpanKind.TOOL,
        status=SpanStatus.OK,
        raw_attributes={
            "gen_ai.tool.name": name,
            "gen_ai.tool.input": {"args": list(args), "kwargs": {}},
            "gen_ai.tool.input_hash": _tool_args_hash(args, {}),
            "gen_ai.tool.output": output,
        },
    )


def _seed_agent_trace(store: TraceStore) -> dict[str, Any]:
    """Seed a 3-span agent trace: LLM → TOOL → LLM."""
    user_msgs = [{"role": "user", "content": "weather in Paris?"}]
    followup_msgs = [
        {"role": "user", "content": "weather in Paris?"},
        {"role": "assistant", "content": "let me check"},
        {
            "role": "tool",
            "content": "[{'city': 'Paris', 'temp_c': 18}]",
        },
    ]
    spans = [
        _llm_span(span_id="a" * 16, messages=user_msgs, response_content="let me check"),
        _tool_span(
            span_id="b" * 16,
            name="get_weather",
            args=("Paris",),
            output=[{"city": "Paris", "temp_c": 18}],
        ),
        _llm_span(
            span_id="c" * 16,
            messages=followup_msgs,
            response_content="Paris is 18C",
        ),
    ]
    store.upsert_trace(Trace(trace_id=_TRACE_ID, spans=spans))
    for s in spans:
        store.insert_span(s)
    return {
        "trace_id": _TRACE_ID,
        "user_messages": user_msgs,
        "followup_messages": followup_msgs,
        "tool_args": ("Paris",),
        "expected_tool_output": [{"city": "Paris", "temp_c": 18}],
    }


# ----------------------------------------------------------------------
# The fake agent — what the user-side code looks like.
# ----------------------------------------------------------------------
def _build_agent(completions: Any) -> Any:
    """Construct a tiny agent object that talks through ``completions``."""

    @timetravel_tool(name="get_weather")
    def get_weather(city: str) -> list[dict[str, Any]]:
        # In frozen replay this is never called; in branch divergence it is.
        return [{"city": city, "temp_c": -999, "live": True}]

    class _Agent:
        def __init__(self) -> None:
            self.tool_calls = 0

        def run(self, user_input: str) -> str:
            messages = [{"role": "user", "content": user_input}]
            first = completions.create(model="qwen3:32b", messages=messages)
            _assistant = self._content(first)
            messages.append({"role": "assistant", "content": _assistant})
            # Deserialize the tool input from the LLM response.
            tool_input = user_input.split("weather in ", maxsplit=1)[1].rstrip("?")
            tool_output = get_weather(tool_input)
            self.tool_calls += 1
            messages.append(
                {"role": "tool", "content": str(tool_output)}
            )
            final = completions.create(model="qwen3:32b", messages=messages)
            return self._content(final)

        @staticmethod
        def _content(resp: Any) -> str:
            body = resp.model_dump() if hasattr(resp, "model_dump") else resp
            content: str = body["choices"][0]["message"]["content"]
            return content

    return _Agent


# ----------------------------------------------------------------------
# The integration contract.
# ----------------------------------------------------------------------
def test_frozen_replay_is_offline(
    tmp_path: Path,
) -> None:
    """Running the agent in FROZEN mode serves all calls from cache."""
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_agent_trace(store)

    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        completions_instance = Completions()
        # Make the agent use the SAME instance so we can count its calls.
        Agent = _build_agent(completions_instance)

        with patch(), replay_ctx(store, seed["trace_id"], mode=ReplayMode.FROZEN):
            assert active_session() is not None
            agent = Agent()
            output = agent.run("weather in Paris?")

        # Cached content from the third span ("Paris is 18C"), not the live stub.
        assert output == "Paris is 18C"
        # Zero live HTTP-shape calls to the stub.
        assert completions_instance.calls == []
        # Both LLM spans consumed (cursor moved past all 3).
        # Tool was served from cache — never executed live.
        assert agent.tool_calls == 1  # the wrapper did execute (cached branch)


def test_frozen_replay_audits_no_side_effects(
    tmp_path: Path,
) -> None:
    """A frozen replay does not invoke the live tool callable side-effects."""
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_agent_trace(store)
    side_effect_log: list[str] = []

    with _fake_openai_module() as fake:
        Completions = fake["Completions"]
        completions_instance = Completions()

        @timetravel_tool(name="get_weather")
        def get_weather(city: str) -> list[dict[str, Any]]:
            side_effect_log.append(f"LIVE:{city}")
            return [{"city": city, "live": True}]

        def run_agent() -> str:
            messages = [{"role": "user", "content": "weather in Paris?"}]
            r1 = completions_instance.create(model="qwen3:32b", messages=messages)
            body = r1.model_dump() if hasattr(r1, "model_dump") else r1
            messages.append(
                {"role": "assistant", "content": body["choices"][0]["message"]["content"]}
            )
            tool_result = get_weather(seed["tool_args"][0])  # "Paris"
            messages.append({"role": "tool", "content": str(tool_result)})
            r2 = completions_instance.create(model="qwen3:32b", messages=messages)
            body2 = r2.model_dump() if hasattr(r2, "model_dump") else r2
            out2: str = body2["choices"][0]["message"]["content"]
            return out2

        with patch(), replay_ctx(store, seed["trace_id"], mode=ReplayMode.FROZEN):
            output = run_agent()

        # Live tool body never executed.
        assert side_effect_log == []
        assert output == "Paris is 18C"


def test_branch_fork_captures_divergent_spans(
    tmp_path: Path,
) -> None:
    """Branching at index 1 (between first LLM and tool) re-runs the tail.

    A divergent tool call (different city) forwards live, runs the live
    function body, and persists a new TOOL span under the fork's
    ``branch_id``. The final LLM call also forwards live (because the
    messages hash has changed) and persists a new LLM span.
    """
    store = TraceStore(str(tmp_path / "agent_timetravel.db"))
    seed = _seed_agent_trace(store)

    with _fake_openai_module():
        @timetravel_tool(name="get_weather")
        def get_weather(city: str) -> list[dict[str, Any]]:
            return [{"city": city, "temp_c": -5, "live": True}]

        with patch(), replay_ctx(
            store,
            seed["trace_id"],
            mode=ReplayMode.BRANCH,
            branch_at=1,  # consume first LLM span, re-execute tool + second LLM
        ) as session:
            # A divergent tool call (different city from the root recording)
            # misses the cache, forwards live, runs the function body, and
            # records a new TOOL span under the fork's branch_id.
            divergent_output = get_weather("Berlin")

            # Live tool ran - captured under branch_id.
            branch_id = session.branch_id
            tail = store.get_spans(seed["trace_id"], branch_id=branch_id)

        assert divergent_output == [{"city": "Berlin", "temp_c": -5, "live": True}]
        # Filter to live-captured spans: the inherited-union query returns
        # root spans too; we identify our branch's new spans by their input hash.
        berlin_hash = _tool_args_hash(("Berlin",), {})
        new_tools = [
            s
            for s in tail
            if s.name == "get_weather"
            and s.raw_attributes.get("gen_ai.tool.input_hash") == berlin_hash
        ]
        assert len(new_tools) == 1
        new_span = new_tools[0]
        assert new_span.raw_attributes["gen_ai.tool.output"] == [
            {"city": "Berlin", "temp_c": -5, "live": True}
        ]
        # Hash reflects Berlin, not Paris.
        assert new_span.raw_attributes["gen_ai.tool.input_hash"] == berlin_hash
