import os

# Clear proxy variables so loopback requests are not intercepted by sandbox proxies
for key in ["http_proxy", "https_proxy", "HTTP_PROXY", "HTTPS_PROXY"]:
    os.environ.pop(key, None)
os.environ["NO_PROXY"] = "localhost,127.0.0.1"

import pytest  # noqa: E402

from agent_timetravel.enums import SpanKind  # noqa: E402
from agent_timetravel.models import Branch, Span, Trace  # noqa: E402


@pytest.fixture
def sample_raw_attributes() -> dict[str, object]:
    """A minimal but realistic GenAI semconv attribute payload for an LLM span."""
    return {
        "gen_ai.system": "openai",
        "gen_ai.request.model": "qwen3:32b",
        "gen_ai.response.model": "qwen3:32b",
        "gen_ai.usage.prompt_tokens": 42,
        "gen_ai.usage.completion_tokens": 7,
        "gen_ai.usage.total_tokens": 49,
    }


@pytest.fixture
def llm_span(sample_raw_attributes: dict[str, object]) -> Span:
    return Span(
        trace_id="0" * 24 + "11111111",
        span_id="0" * 8 + "11111111"[:8],
        parent_span_id=None,
        name="chat.compt",
        kind=SpanKind.LLM,
        model_name="qwen3:32b",
        prompt_tokens=42,
        completion_tokens=7,
        total_tokens=49,
        start_time="2026-06-29T10:00:01+00:00",
        end_time="2026-06-29T10:00:02+00:00",
        raw_attributes=sample_raw_attributes,
    )


@pytest.fixture
def tool_span() -> Span:
    return Span(
        trace_id="0" * 24 + "11111111",
        span_id="0" * 8 + "22222222",
        parent_span_id="0" * 8 + "11111111",
        name="tool.search_products",
        kind=SpanKind.TOOL,
        start_time="2026-06-29T10:00:03+00:00",
        end_time="2026-06-29T10:00:04+00:00",
        raw_attributes={"tool.name": "search_products", "tool.output": "[]"},
    )


@pytest.fixture
def agent_span() -> Span:
    return Span(
        trace_id="0" * 24 + "11111111",
        span_id="0" * 8 + "33333333",
        parent_span_id=None,
        name="adk.agent.CustomerCareAgent",
        kind=SpanKind.AGENT,
        start_time="2026-06-29T10:00:00+00:00",
        end_time="2026-06-29T10:00:05+00:00",
        raw_attributes={"openinference.span.kind": "AGENT"},
    )


@pytest.fixture
def sample_trace(
    llm_span: Span, tool_span: Span, agent_span: Span
) -> Trace:
    """A 3-span trace: 1 agent + 1 LLM + 1 tool — the Phase 0 round-trip fixture."""
    return Trace(trace_id="0" * 24 + "11111111", spans=[agent_span, llm_span, tool_span])


@pytest.fixture
def sample_branch(sample_trace: Trace) -> Branch:
    return Branch(
        trace_id=sample_trace.trace_id,
        parent_branch_id=sample_trace.root_branch_id,
        branch_at_index=1,
        mode="branch",
        label="changed system prompt",
    )
