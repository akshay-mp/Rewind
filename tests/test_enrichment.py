"""Unit tests for the Phase 7 :mod:`agent_timetravel.enrichment` module.

Four families pin the Phase 7 exit criteria:

1. :func:`parse_quant` — covers every GGUF quant format Ollama / llama.cpp
   emits, plus positive "no quant" cases (cloud models, untagged local tags).
2. :func:`render_chat_template` — fallback path (no transformers installed)
   is deterministic and produces a readable concatenation. The
   ``transformers``-backed path is gated and not exercised here.
3. :func:`sample_vram` — returns ``VramSample(None, None)`` cleanly when no
   platform sampler is installed. We monkey-patch ``shutil.which`` to force
   every sampler OFF, so the test is hermetic on any CI host.
4. :func:`enrich_span` — top-level pass correctly stamps the quant attribute
   and is a no-op when no quant tag is present.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from agent_timetravel.enrichment import (
    GPU_PCT_ATTR,
    QUANT_ATTR,
    VRAM_MIB_ATTR,
    QuantInfo,
    enrich_span,
    parse_quant,
    quant_from_span,
    render_chat_template,
    sample_vram,
)
from agent_timetravel.enums import SpanKind
from agent_timetravel.models import Span

# ---------------------------------------------------------------------------
# 1. parse_quant
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("model_name", "expected"),
    [
        # GGUF K-quant variants (most common).
        ("qwen3:32b-q4_K_M", QuantInfo("q4_k_m", 4)),
        ("qwen3:32b-q4_K_S", QuantInfo("q4_k_s", 4)),
        ("qwen3:32b-q5_K_M", QuantInfo("q5_k_m", 5)),
        ("qwen3:32b-q6_K", QuantInfo("q6_k", 6)),
        # Legacy quant.
        ("llama3.1:8b-q8_0", QuantInfo("q8_0", 8)),
        ("mistral:7b-q2_K", QuantInfo("q2_k", 2)),
        ("mistral:7b-q3_K_M", QuantInfo("q3_k_m", 3)),
        # Float formats — no bits.
        ("phi3:mini-f16", QuantInfo("f16", None)),
        ("phi3:mini-fp16", QuantInfo("fp16", None)),
        ("phi3:mini-bf16", QuantInfo("bf16", None)),
        ("phi3:mini-f32", QuantInfo("f32", None)),
        # intN variants.
        ("test-model-i8", QuantInfo("i8", 8)),
        ("test-model-i4", QuantInfo("i4", 4)),
    ],
)
def test_parse_quant_matches_known_tags(
    model_name: str, expected: QuantInfo
) -> None:
    """Every Ollama / llama.cpp GGUF quant tag must parse correctly."""
    assert parse_quant(model_name) == expected


@pytest.mark.parametrize(
    "model_name",
    [
        "qwen3:32b",                 # local but no quant suffix
        "llama3.1:8b-instruct",      # local but no quant suffix
        "gpt-4o",                    # cloud
        "claude-3-5-sonnet-v2",      # cloud
        "gemini-1.5-pro",            # cloud
        "",                          # empty
    ],
)
def test_parse_quant_returns_none_when_no_tag(model_name: str) -> None:
    """Untagged / cloud model names must produce QuantInfo(None, None)."""
    assert parse_quant(model_name) == QuantInfo(None, None)


def test_parse_quant_handles_none() -> None:
    """None model_name must not blow up — spans with no model_name exist."""
    assert parse_quant(None) == QuantInfo(None, None)


# ---------------------------------------------------------------------------
# 2. quant_from_span (precedence: recorded attribute > on-the-fly parse)
# ---------------------------------------------------------------------------


def _make_span(model: str | None, *, quant_attr: str | None = None) -> Span:
    """Build a minimal LLM span with optional recorded quant attribute."""
    raw: dict[str, object] = {}
    if quant_attr is not None:
        raw[QUANT_ATTR] = quant_attr
    return Span(
        trace_id="0" * 32,
        span_id="0" * 16,
        name="chat.completions",
        kind=SpanKind.LLM,
        model_name=model,
        raw_attributes=raw,
    )


def test_quant_from_span_prefers_recorded_attribute() -> None:
    """A recorded timetravel.local.quant beats on-the-fly parsing."""
    span = _make_span("qwen3:32b-q4_K_M", quant_attr="q8_0")
    assert quant_from_span(span) == QuantInfo("q8_0", 8)


def test_quant_from_span_falls_back_to_parsing() -> None:
    """With no recorded attr, falls back to parsing model_name."""
    span = _make_span("qwen3:32b-q4_K_M")
    assert quant_from_span(span) == QuantInfo("q4_k_m", 4)


def test_quant_from_span_returns_none_when_no_signal() -> None:
    """Cloud / untagged spans with no recorded attr return QuantInfo(None, None)."""
    span = _make_span("gpt-4o")
    assert quant_from_span(span) == QuantInfo(None, None)


# ---------------------------------------------------------------------------
# 3. render_chat_template — fallback path only (transformers gated)
# ---------------------------------------------------------------------------


def test_render_template_empty_messages() -> None:
    """No messages → empty string."""
    assert render_chat_template([]) == ""


def test_render_template_fallback_is_deterministic() -> None:
    """Fallback render sorts by role and preserves intra-role order."""
    messages: Sequence[dict[str, object]] = [
        {"role": "user", "content": "first user msg"},
        {"role": "system", "content": "system prompt"},
        {"role": "assistant", "content": "assistant reply"},
        {"role": "user", "content": "second user msg"},
    ]
    rendered = render_chat_template(messages, model_name=None)
    # System comes first.
    assert rendered.startswith("[system] system prompt")
    # User messages follow in original order.
    assert rendered.index("first user msg") < rendered.index("second user msg")
    # Assistant comes last by role weight.
    assert rendered.endswith("[assistant] assistant reply")


def test_render_template_fallback_handles_non_dict_messages() -> None:
    """Non-dict messages are stringified rather than crashing."""
    messages: Sequence[object] = [
        "raw string",
        {"role": "user", "content": "real msg"},
    ]
    rendered = render_chat_template(
        messages,  # type: ignore[arg-type]
        model_name=None,
    )
    assert "[unknown] raw string" in rendered
    assert "[user] real msg" in rendered


def test_render_template_with_model_name_still_returns_string() -> None:
    """Even with a model_name, the function never raises — degrades to fallback.

    In the dev/test environment transformers isn't installed, so the HF
    tokenizer branch can't resolve. This test pins the contract that we get
    the fallback render back rather than an exception.
    """
    messages: Sequence[dict[str, object]] = [
        {"role": "user", "content": "hi"},
    ]
    rendered = render_chat_template(messages, model_name="qwen3:32b-q4_K_M")
    assert isinstance(rendered, str)
    assert "[user] hi" in rendered


# ---------------------------------------------------------------------------
# 4. sample_vram — hermetic, no real sampler on the host
# ---------------------------------------------------------------------------


def test_sample_vram_returns_none_when_no_sampler(monkeypatch: pytest.MonkeyPatch) -> None:
    """When no GPU probe tool is installed, sample_vram is a clean no-op."""
    monkeypatch.setattr("agent_timetravel.enrichment.shutil.which", lambda _: None)
    sample = sample_vram()
    assert sample.vram_mib is None
    assert sample.gpu_pct is None


def test_sample_vram_swallows_nvidia_smi_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """nvidia-smi present but exits non-zero → treat as unavailable."""
    monkeypatch.setattr(
        "agent_timetravel.enrichment.shutil.which",
        lambda cmd: "/usr/bin/nvidia-smi" if cmd == "nvidia-smi" else None,
    )

    def _fake_run(*_args: object, **_kwargs: object) -> object:
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "GPU not found"

        return _Proc()

    monkeypatch.setattr("agent_timetravel.enrichment.subprocess.run", _fake_run)
    sample = sample_vram()
    assert sample.vram_mib is None


# ---------------------------------------------------------------------------
# 5. enrich_span — top-level orchestration
# ---------------------------------------------------------------------------


def test_enrich_span_stamps_quant_attribute() -> None:
    """Default flags → a model tag with a quant suffix is detected + stamped."""
    span = _make_span("qwen3:32b-q4_K_M")
    enrich_span(span)
    assert span.raw_attributes[QUANT_ATTR] == "q4_k_m"


def test_enrich_span_no_op_when_model_has_no_quant() -> None:
    """Default flags + cloud/untagged model → no quant attribute written."""
    span = _make_span("gpt-4o")
    enrich_span(span)
    assert QUANT_ATTR not in span.raw_attributes


def test_enrich_span_respects_disable_quant_flag() -> None:
    """parse_model_quant=False suppresses the quant pass."""
    span = _make_span("qwen3:32b-q4_K_M")
    enrich_span(span, parse_model_quant=False)
    assert QUANT_ATTR not in span.raw_attributes


def test_enrich_span_vram_off_does_not_touch_raw_attrs() -> None:
    """With sample_gpu=False the sampler never runs and no VRAM key is written."""
    span = _make_span("qwen3:32b-q4_K_M")
    enrich_span(span, sample_gpu=False)
    assert VRAM_MIB_ATTR not in span.raw_attributes
    assert GPU_PCT_ATTR not in span.raw_attributes


def test_enrich_span_vram_on_writes_when_sampler_reports(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the sampler reports a value, enrich_span writes it to raw_attributes."""
    from agent_timetravel.enrichment import VramSample

    monkeypatch.setattr(
        "agent_timetravel.enrichment.sample_vram",
        lambda: VramSample(vram_mib=4096, gpu_pct=42.5),
    )
    span = _make_span("qwen3:32b-q4_K_M")
    enrich_span(span, sample_gpu=True)
    assert span.raw_attributes[VRAM_MIB_ATTR] == 4096
    assert span.raw_attributes[GPU_PCT_ATTR] == 42.5
